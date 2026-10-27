"""EngineService: wires M1–M6, evidence fusion, trips and the clock together."""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict

import numpy as np
from shapely.geometry import LineString, Point

from .assistant import Assistant
from .config import Settings
from .detour import DetourSynthesizer
from .feasibility import FeasibilityEvaluator
from .fusion import EvidenceFusion, apply_evidence
from .geo import LocalFrame
from .geography import PlaceLookup
from .geometry import generate_slices
from .heatmap import risk_grid
from .hazard_model import HydroState, build_inputs
from . import ponr
from .offline import decode_points, encode_points
from .rainfall import LiveWeather, ScenarioWeather
from .routing.base import Route
from .routing.local_graph import LocalGraphEngine
from .routing.osrm import OsrmEngine
from .scenario import DT_S, StormScenario, TruthSimulator
from .slices import HazardSlice, new_id
from .sms import SmsGateway
from .sop import load_shelters, sop_for, synthetic_shelters
from .terrain import load_esri_ascii, synthetic_terrain
from .explain import build_decision_record, local_forecast_at
from .train import START, load_or_train
from .trips import Trip, TripRegistry
from .vehicles import PROFILES, get_profile

log = logging.getLogger("safe_engine")
VEHICLE_CODES = {k: i for i, k in enumerate(PROFILES)}


class EngineService:
    def __init__(self, settings: Settings | None = None, scenario_seed: int | None = None, model=None, engine=None):
        self.settings = s = settings or Settings()
        self.frame = LocalFrame(s.region_lon0, s.region_lat0)
        self.lock = threading.RLock()
        import os
        if s.mode == "live" and s.dem_path:
            self.terrain = load_esri_ascii(s.dem_path, self.frame)
        else:
            self.terrain = synthetic_terrain(self.frame, s.region_width_m, s.region_height_m, s.cell_m, seed=s.seed)
        self.width = self.terrain.nx * self.terrain.cell
        self.height = self.terrain.ny * self.terrain.cell
        self.model = model or load_or_train()
        self.horizons = [h * 60 for h in s.horizons_min]
        self.place_lookup = PlaceLookup(s.geocoder_url, s.osrm_url, s.geocoder_user_agent)
        if engine is not None:
            self.engine = engine
        elif s.routing_engine == "osrm":
            self.engine = OsrmEngine(s.osrm_url, self.frame)
        else:
            rv = self.terrain.river_mask
            cell = self.terrain.cell

            def is_channel(x, y):
                r, c = int(y // cell), int(x // cell)
                return 0 <= r < rv.shape[0] and 0 <= c < rv.shape[1] and bool(rv[r, c])
            self.engine = LocalGraphEngine(self.width, self.height, seed=s.seed, is_channel=is_channel)
        self.evaluator = FeasibilityEvaluator(s)
        self.detour = DetourSynthesizer(self.engine, self.evaluator, s)
        shelters_path = os.environ.get("SHELTERS_PATH", "")
        self.shelters = load_shelters(shelters_path, self.frame) if shelters_path else synthetic_shelters(self.terrain)
        self.assistant = Assistant(self)
        self.timings: dict = {}
        self.reval_stats: list = []
        self.auto_reroute = True   # evaluation can switch off re-planning to time re-validation alone
        self.sms = SmsGateway(self)
        self.reset(scenario_seed if scenario_seed is not None else s.seed)

    # ================================================================== clock / scenario
    def reset(self, seed: int | None = None):
        from .fusion import EvidenceFusion
        from .slices import HazardStore
        s = self.settings
        with self.lock:
            self.seed = seed if seed is not None else s.seed
            self.store = HazardStore(s.index_bucket_s)
            self.fusion = EvidenceFusion(self.frame)
            self.registry = TripRegistry(s.trip_cell_m, s.eta_rel_uncertainty, s.capacity_veh_per_hr)
            self.routes: OrderedDict = OrderedDict()
            self.trip_archive: OrderedDict = OrderedDict()
            self.sms.clear()
            if s.mode == "live":
                self.scenario = None
                self.truth = None
                self.weather = LiveWeather(self.terrain, s.rainfall_api_url)
                self._now = None
                self.state = HydroState(self.terrain, time.time() - 3 * 3600)
            else:
                self.scenario = StormScenario(self.terrain, self.seed, START)
                self.truth = TruthSimulator(self.terrain, self.scenario)
                self.weather = ScenarioWeather(self.scenario)
                self._now = START
                self.state = HydroState(self.terrain, START)
                rng = np.random.default_rng(self.seed + 5)
                cand = np.argwhere(self.terrain.fill_depth > 0.3)
                pick = cand[rng.choice(len(cand), size=min(8, len(cand)), replace=False)] if len(cand) else []
                self.sensors = [{"id": f"WL{i + 1:02d}", "rc": (int(r), int(c))} for i, (r, c) in enumerate(pick)]
            self.refresh()

    def now(self) -> float:
        return self._now if self._now is not None else time.time()

    @property
    def synthetic(self) -> bool:
        return self.scenario is not None

    def advance(self, seconds: float) -> dict:
        """Scenario mode: move the clock forward, refreshing hazards every 15 minutes."""
        if self._now is None:
            raise RuntimeError("clock is real time in live mode")
        target = self._now + seconds
        with self.lock:
            while self._now < target - 1e-6:
                nxt = START + (math.floor((self._now - START) / 900 + 1e-9) + 1) * 900
                self._now = min(nxt, target)
                if abs(self._now - nxt) < 1e-6:
                    self.refresh()
        return self.status()

    # ================================================================== hazard refresh (M1 -> M2)
    def refresh(self) -> dict:
        t0 = time.perf_counter()
        now = self.now()
        with self.lock:
            self.state.advance_to(now, self.weather)
            if self.truth is not None:
                for sd in self.sensors:
                    d = float(self.truth.depth_at(now)[sd["rc"]])
                    x, y = self.terrain.cell_center(*sd["rc"])
                    sd["depth_m"] = round(d, 3)
                    self.fusion.add_sensor(x, y, d, now, sd["id"])
                self.fusion.items = [e for e in self.fusion.items if not (e.kind == "sensor" and now - e.t > 3600)]
            inputs = build_inputs(self.state, self.weather, now, self.horizons)
            t1 = time.perf_counter()
            preds = [self.model.predict(hi) for hi in inputs]
            for cls in ("flood", "landslide", "storm"):
                e, floor = self.fusion.field(self.terrain, cls, now)
                if not np.any(e > 0):
                    continue
                for k, hi in enumerate(inputs):
                    preds[k][cls] = apply_evidence(preds[k][cls], e.ravel(), hi.tau_s)
                    if cls == "flood":
                        fl = floor.ravel()
                        # reported depth raises the exceedance probability of the levels it reaches
                        preds[k]["flood_levels"] = [(lv, apply_evidence(p, np.where(fl >= lv, e.ravel(), 0.3 * e.ravel()), hi.tau_s))
                                                    for lv, p in preds[k].get("flood_levels", [])]
                        if hi.tau_s <= 3600:
                            hi.est_depth = np.maximum(hi.est_depth, floor)
            t2 = time.perf_counter()
            slices, raster = generate_slices(self.terrain, now, self.horizons, preds, inputs)
            t3 = time.perf_counter()
            diff = self.store.publish_model(slices, now, raster, self.terrain.cell)
            t4 = time.perf_counter()
            reval = self.on_new_version(diff, now)
            self.timings = {"inputs_ms": round((t1 - t0) * 1000), "model_ms": round((t2 - t1) * 1000),
                            "geometry_ms": round((t3 - t2) * 1000), "publish_ms": round((t4 - t3) * 1000),
                            "revalidation_ms": reval["ms"], "slices": len(slices), "version": self.store.version}
            return self.timings

    # ================================================================== planning (M3 + M4 + capacity)
    def _plan_xy(self, o, d, vehicle: str, depart_t: float, k: int | None = None):
        s = self.settings
        veh = get_profile(vehicle)
        stale = (self.now() - self.store.updated_at) > s.hazard_ttl_s
        cands = self.engine.routes(o, d, k or s.k_alternatives)
        rows = []
        for r in cands:
            a = self.evaluator.assess(r, depart_t, veh, self.store.index, stale=stale)
            rows.append({"route": r, "a": a, "lf": self.registry.load_factor(r, depart_t), "source": "engine"})
        detour_info = None
        if rows and not any(x["a"].feasible for x in rows):
            worst_first = sorted(rows, key=lambda x: (len(x["a"].blocked), -x["a"].p_safe, x["route"].duration))
            det = self.detour.synthesize(o, d, worst_first[0]["route"], worst_first[0]["a"], depart_t, veh, self.store)
            detour_info = {k2: v for k2, v in det.items() if k2 not in ("route", "assessment")}
            if det["route"] is not None:
                rows.append({"route": det["route"], "a": det["assessment"], "lf": self.registry.load_factor(det["route"], depart_t), "source": "detour"})
        feas = [x for x in rows if x["a"].feasible]
        chosen = None
        if feas:
            g = s.capacity_gamma
            chosen = min(feas, key=lambda x: x["route"].duration * (1 + g * max(0.0, x["lf"] - 0.8)))
        for x in rows:
            self.routes[x["route"].id] = {"route": x["route"], "vehicle": vehicle, "depart_t": depart_t, "created": self.now(),
                                          "assessment": x["a"]}
            self.routes.move_to_end(x["route"].id)
        while len(self.routes) > 5000:
            self.routes.popitem(last=False)
        return rows, chosen, detour_info, stale

    def plan(self, origin, destination, vehicle="car", depart_t=None, k=None, with_baseline=True) -> dict:
        depart_t = depart_t or self.now()
        o, d = self.frame.to_xy(*origin), self.frame.to_xy(*destination)
        with self.lock:
            rows, chosen, detour, stale = self._plan_xy(o, d, vehicle, depart_t, k)
            baseline = None
            if with_baseline:
                b_ok = []
                for x in rows:
                    if x["source"] != "engine":
                        continue
                    b = self.evaluator.assess(x["route"], depart_t, get_profile(vehicle), self.store.index,
                                              time_aware=False, vehicle_aware=False, hard_only=True)
                    if b.feasible:
                        b_ok.append(x)
                if b_ok:
                    pick = min(b_ok, key=lambda x: x["route"].duration)
                    baseline = {"route_id": pick["route"].id, "m3_feasible": pick["a"].feasible,
                                "note": "Present-tense polygon filter (original design) would choose this route."}
                else:
                    baseline = {"route_id": None, "note": "Present-tense polygon filter (original design) finds no route."}
            out = []
            for x in rows:
                r, a = x["route"], x["a"]
                out.append({"id": r.id, "source": x["source"], "chosen": chosen is not None and r.id == chosen["route"].id,
                            "feasible": a.feasible, "p_safe": round(a.p_safe, 4), "margin_s": None if a.margin_s is None else round(a.margin_s),
                            "reason": a.reason, "duration_s": round(r.duration), "distance_m": round(r.distance),
                            "load_factor": round(x["lf"], 3), "stats": a.stats,
                            "blocked": [{**b, "at": self.frame.to_lonlat(*b["at"])} for b in a.blocked[:10]],
                            "geometry": {"type": "LineString", "coordinates": [list(map(lambda v: round(v, 6), self.frame.to_lonlat(*c))) for c in r.coords]},
                            "via": [self.frame.to_lonlat(*v) for v in r.via],
                            "route_code": self.route_code(r.id, vehicle)})
            rec = build_decision_record(self, chosen["route"].id, "en") if chosen else None
            leave_by = self.route_leave_by(chosen["route"].id) if chosen else None
            return {"routes": out, "chosen": chosen["route"].id if chosen else None, "detour": detour, "baseline": baseline,
                    "decision_record": rec, "leave_by": leave_by, "vehicle": get_profile(vehicle).to_dict(), "depart_t": depart_t, "stale": stale,
                    "hazard_data_age_s": round(self.now() - self.store.updated_at), "hazard_version": self.store.version,
                    "synthetic": self.synthetic}

    def explain_route(self, route_id: str, lang: str = "en") -> dict | None:
        with self.lock:
            return build_decision_record(self, route_id, lang)

    def forecast_point(self, lon: float, lat: float, horizon_min: int = 60) -> dict | None:
        with self.lock:
            return local_forecast_at(self, lon, lat, horizon_min)

    def reassess(self, route_id: str, now: float) -> dict | None:
        c = self.routes.get(route_id)
        if c is None:
            return None
        stale = (now - self.store.updated_at) > self.settings.hazard_ttl_s
        a = self.evaluator.assess(c["route"], now, get_profile(c["vehicle"]), self.store.index, stale=stale)
        return a.to_dict()

    def route_info(self, route_id: str) -> dict:
        r = self.routes[route_id]["route"]
        return {"distance_m": r.distance, "duration_s": r.duration}

    def route_code(self, route_id: str, vehicle: str) -> str:
        r = self.routes[route_id]["route"] if route_id in self.routes else None
        if r is None:
            return ""
        pts = [r.coords[0], *r.via, r.coords[-1]]
        return encode_points([self.frame.to_lonlat(*p) for p in pts], VEHICLE_CODES.get(vehicle, 0))

    def decode_route(self, code: str) -> dict:
        pts, vcode = decode_points(code)
        vehicle = next((k for k, v in VEHICLE_CODES.items() if v == vcode), "car")
        xy = [self.frame.to_xy(*p) for p in pts]
        r = self.engine.route_via(xy[0], xy[1:-1], xy[-1]) if len(xy) > 2 else self.engine.routes(xy[0], xy[-1], 1)[0]
        a = self.evaluator.assess(r, self.now(), get_profile(vehicle), self.store.index)
        self.routes[r.id] = {"route": r, "vehicle": vehicle, "depart_t": self.now(), "created": self.now(), "assessment": a}
        return {"route_id": r.id, "vehicle": vehicle, "points": pts, "assessment": a.to_dict(),
                "geometry": {"type": "LineString", "coordinates": [self.frame.to_lonlat(*c) for c in r.coords]}}

    # ================================================================== trips (M5)
    def start_trip(self, route_id: str, user: str = "anon") -> dict:
        c = self.routes.get(route_id)
        if c is None:
            raise KeyError(route_id)
        now = self.now()
        r = c["route"]
        trip = Trip(route=r, depart_t=now, vehicle=c["vehicle"], origin=r.coords[0], dest=r.coords[-1])
        a = self.evaluator.assess(r, now, get_profile(c["vehicle"]), self.store.index)
        trip.last_p_safe, trip.last_margin = a.p_safe, a.margin_s
        trip.events.append({"seq": 1, "t": now, "type": "started", "route_id": r.id, "p_safe": round(a.p_safe, 3), "margin_s": a.margin_s})
        self.registry.register(trip, now)
        return self.trip_status(trip.id)

    def _archive(self, t):
        if t is not None:
            self.trip_archive[t.id] = t
            while len(self.trip_archive) > 2000:
                self.trip_archive.popitem(last=False)

    def trip_status(self, tid: str, since: int = 0) -> dict:
        t = self.registry.trips.get(tid) or self.trip_archive.get(tid)
        if t is None:
            raise KeyError(tid)
        now = self.now()
        _, pos = t.position(now)
        return {"id": t.id, "status": t.status, "route_id": t.route.id, "vehicle": t.vehicle, "reroutes": t.reroutes,
                "position": self.frame.to_lonlat(*pos), "eta": t.depart_t + t.route.duration,
                "p_safe": round(t.last_p_safe, 3), "margin_s": t.last_margin,
                "geometry": {"type": "LineString", "coordinates": [self.frame.to_lonlat(*c) for c in t.route.coords]},
                "events": [e for e in t.events if e["seq"] > since]}

    def cancel_trip(self, tid: str):
        t = self.registry.remove(tid)
        if t:
            t.status = "cancelled"
            self._archive(t)

    def on_new_version(self, diff, now: float) -> dict:
        t0 = time.perf_counter()
        s = self.settings
        for t in list(self.registry.active()):
            if t.arrived(now):
                t.status = "arrived"
                t.events.append({"seq": len(t.events) + 1, "t": now, "type": "arrived"})
                self._archive(self.registry.remove(t.id))
        total = len(self.registry.active())
        affected = self.registry.affected(diff.parts) if diff.parts and total else set()
        rerouted = 0
        for tid in affected:
            t = self.registry.trips.get(tid)
            if t is None or t.status != "active":
                continue
            veh = get_profile(t.vehicle)
            rem, _ = t.remaining(now)
            a = self.evaluator.assess(rem, now, veh, self.store.index)
            need = (not a.feasible) or (a.margin_s is not None and a.margin_s < s.reroute_margin_min_s)
            if need and not self.auto_reroute:
                t.last_p_safe, t.last_margin = a.p_safe, a.margin_s
            elif need:
                _, pos = t.position(now)
                rows, chosen, _, _ = self._plan_xy(pos, t.dest, t.vehicle, now)
                better = chosen is not None and (not a.feasible or chosen["a"].p_safe - a.p_safe > s.reroute_hysteresis
                                                 or (chosen["a"].margin_s is None or (a.margin_s is not None and chosen["a"].margin_s > a.margin_s + 300)))
                if better:
                    self.registry.replace_route(t, chosen["route"], now)
                    t.reroutes += 1
                    rerouted += 1
                    t.last_p_safe, t.last_margin = chosen["a"].p_safe, chosen["a"].margin_s
                    t.events.append({"seq": len(t.events) + 1, "t": now, "type": "reroute", "route_id": chosen["route"].id,
                                     "reason": a.reason or f"safety margin {int((a.margin_s or 0) // 60)} min",
                                     "p_safe": round(chosen["a"].p_safe, 3), "margin_s": chosen["a"].margin_s,
                                     "geometry": {"type": "LineString", "coordinates": [self.frame.to_lonlat(*c) for c in chosen["route"].coords]}})
                elif chosen is None and not a.feasible:
                    t.events.append({"seq": len(t.events) + 1, "t": now, "type": "no_safe_route", "reason": a.reason,
                                     "advice": "Stop in a safe place on higher ground; do not drive into water."})
            else:
                if abs(a.p_safe - t.last_p_safe) > 0.05:
                    t.events.append({"seq": len(t.events) + 1, "t": now, "type": "update", "p_safe": round(a.p_safe, 3), "margin_s": a.margin_s})
                t.last_p_safe, t.last_margin = a.p_safe, a.margin_s
            self.registry._index(t, now)
        st = {"t": now, "version": diff.version, "active_trips": total, "re_evaluated": len(affected), "rerouted": rerouted,
              "ms": round((time.perf_counter() - t0) * 1000, 1)}
        self.reval_stats.append(st)
        self.reval_stats = self.reval_stats[-200:]
        return st

    # ================================================================== point of no return (M8), heatmap
    def route_leave_by(self, route_id: str, lang: str = "en") -> dict | None:
        """Latest safe start for a planned route (see ponr.py). None for an unknown route."""
        c = self.routes.get(route_id)
        if c is None:
            return None
        with self.lock:
            res = ponr.scan(self, c["route"], c["vehicle"], max(c["depart_t"], self.now()))
        res["route_id"] = route_id
        res["message"] = ponr.message(res, c["vehicle"], lang)
        return res

    def trip_leave_by(self, trip_id: str, lang: str = "en") -> dict:
        """Delay budget of an active trip: how long it can be held up before the road ahead is cut off."""
        t = self.registry.trips.get(trip_id) or self.trip_archive.get(trip_id)
        if t is None:
            raise KeyError(trip_id)
        if t.status != "active":
            return {"status": "inactive", "trip_id": trip_id, "trip_status": t.status, "message": "", "urgency": "none"}
        with self.lock:
            now = self.now()
            rem, _ = t.remaining(now)
            res = ponr.scan(self, rem, t.vehicle, now)
        res["trip_id"] = trip_id
        res["trip_status"] = t.status
        res["message"] = ponr.message(res, t.vehicle, lang, trip=True)
        return res

    def heatmap(self, offset_min: float = 0, hclass: str | None = None, vehicle: str | None = None) -> dict:
        with self.lock:
            return risk_grid(self, self.now() + offset_min * 60, hclass, vehicle)

    # ================================================================== evidence / admin
    def report(self, lon, lat, hclass="flood", depth_level="unknown", reporter="anon", text="", via="app") -> dict:
        now = self.now()
        x, y = self.frame.to_xy(lon, lat)
        with self.lock:
            ev, sl, dup = self.fusion.add_crowd(x, y, hclass, now, reporter, depth_level, text)
            auto = False
            if sl is not None:
                sl.note += f" (via {via})"
                self.store.add_manual(sl, now, reporter or "anon")
                n = self.fusion.corroboration(ev, now)
                if n >= 3 and self.fusion.auto:
                    for e in self.fusion.items:
                        if e.kind == "crowd" and e.slice_id and e.hclass == ev.hclass and e.geom.distance(ev.geom) <= 250 \
                                and self.store.get(e.slice_id) and self.store.get(e.slice_id).status == "pending":
                            diff = self.store.set_status(e.slice_id, "approved", now, f"auto-corroborated({n})")
                            self.on_new_version(diff, now)
                    auto = True
            return {"evidence_id": ev.id, "slice_id": ev.slice_id, "duplicate": dup, "auto_approved": auto,
                    "reporter_reputation": round(self.fusion.rep(reporter), 3)}

    def admin_set(self, slice_id: str, status: str, actor: str) -> dict:
        now = self.now()
        with self.lock:
            diff = self.store.set_status(slice_id, status, now, actor)
            for e in self.fusion.items:
                if e.slice_id == slice_id:
                    if e.kind == "crowd":
                        self.fusion.feedback(e.reporter, status == "approved")
                    e.status = "active" if status == "approved" else "rejected"
            reval = self.on_new_version(diff, now) if diff else None
            return {"slice_id": slice_id, "status": status, "version": self.store.version, "revalidation": reval}

    def admin_add_closure(self, geojson: dict, hclass: str, hours: float, actor: str, note: str = "") -> dict:
        now = self.now()
        g = self.frame.geojson_to_xy(geojson)
        if g.geom_type in ("Point", "LineString"):
            g = g.buffer(40)
        intensity = {"flood": 0.6, "storm": 100.0}.get(hclass, 1.0)
        s = HazardSlice(new_id("A"), hclass, g, now, now + hours * 3600, 1.0, intensity, source="admin",
                        status="approved", approved_by=actor, note=note)
        with self.lock:
            diff = self.store.add_manual(s, now, actor)
            reval = self.on_new_version(diff, now)
        return {"slice_id": s.id, "version": self.store.version, "revalidation": reval}

    def ingest_cap(self, xml_text: str) -> dict:
        now = self.now()
        with self.lock:
            items = self.fusion.add_cap_xml(xml_text, now)
            timings = self.refresh() if items else None
        return {"ingested": len(items), "items": [i.id for i in items], "refresh": timings}

    # ================================================================== queries
    def hazards_geojson(self, t: float | None = None, include_pending=True, classes=None, t_end: float | None = None) -> dict:
        t = self.now() if t is None else t
        t_end = max(t + 1, t_end or t + 1)
        feats = []
        for s in self.store.slices.values():
            if s.status == "rejected" or (s.status == "pending" and not include_pending):
                continue
            if s.status == "approved" and not s.overlaps(t, t_end):
                continue
            if s.status == "pending" and not s.overlaps(t, t_end):
                continue
            if classes and s.hclass not in classes:
                continue
            d = s.to_dict(self.frame)
            g = d.pop("geometry")
            feats.append({"type": "Feature", "geometry": g, "properties": d})
        return {"type": "FeatureCollection", "features": feats, "t": t, "version": self.store.version, "synthetic": self.synthetic}

    def hazards_near(self, lon, lat, radius_m, t0, t1) -> list[dict]:
        x, y = self.frame.to_xy(lon, lat)
        p = Point(x, y)
        hits = self.store.index.query(p.buffer(radius_m), t0, t1)
        out = []
        for h in hits:
            d = h.to_dict(self.frame)
            d.pop("geometry")
            d["distance_m"] = round(h.geom.distance(p))
            out.append(d)
        return sorted(out, key=lambda h: (h["distance_m"], -h["probability"]))

    def point_hazarded(self, lon, lat, t0, t1, vehicle="car") -> bool:
        x, y = self.frame.to_xy(lon, lat)
        veh = get_profile(vehicle)
        for h in self.store.index.query(Point(x, y), t0, t1):
            if (h.intensity > veh.threshold(h.hclass) or h.hclass in ("landslide", "closure")) and h.probability >= self.settings.p_hard_block:
                return True
        return False

    def nearest_shelters(self, lon, lat, n=3) -> list[dict]:
        x, y = self.frame.to_xy(lon, lat)
        out = []
        for s in self.shelters:
            lo, la = self.frame.to_lonlat(s["x"], s["y"])
            out.append({"id": s["id"], "name": s["name"], "lon": lo, "lat": la, "capacity": s["capacity"],
                        "distance_m": math.dist((x, y), (s["x"], s["y"])), "synthetic": s.get("synthetic", False)})
        return sorted(out, key=lambda s: s["distance_m"])[:n]

    def truth_geojson(self, t: float | None = None) -> dict:
        """Scenario only: true flood extent (> 0.15 m) for the demo's 'ground truth' toggle."""
        if self.truth is None:
            return {"type": "FeatureCollection", "features": []}
        from .geometry import _polys_for_mask
        t = t or self.now()
        mask = self.truth.depth_at(t) >= 0.15
        feats = [{"type": "Feature", "properties": {"kind": "truth_flood"}, "geometry": self.frame.xy_to_geojson(g, 5)}
                 for g in _polys_for_mask(mask, self.terrain.cell, self.terrain.cell * 0.25)]
        return {"type": "FeatureCollection", "features": feats, "t": t}

    def offline_pack(self, lon, lat, radius_km=5, hours=3) -> dict:
        now = self.now()
        x, y = self.frame.to_xy(lon, lat)
        area = Point(x, y).buffer(radius_km * 1000)
        sl = []
        for s in self.store.slices.values():
            if s.status == "approved" and s.probability >= 0.3 and s.t_end >= now and s.t_start <= now + hours * 3600 \
                    and s.geom.intersects(area):
                d = s.to_dict(self.frame)
                d["geometry"] = self.frame.xy_to_geojson(s.geom.intersection(area).simplify(30), 5)
                for k in ("created_at", "approved_by", "note", "version", "status"):
                    d.pop(k, None)
                sl.append(d)
        pack = {"generated_at": now, "valid_until": now + hours * 3600, "ttl_s": self.settings.hazard_ttl_s,
                "hazard_version": self.store.version, "center": [lon, lat], "radius_km": radius_km, "slices": sl,
                "shelters": [s for s in self.nearest_shelters(lon, lat, 20) if s["distance_m"] <= radius_km * 1000],
                "sop": {lang: sop_for("any", lang) + sop_for("flood", lang) for lang in ("en", "hi", "ta")},
                "vehicles": {k: v.to_dict() for k, v in PROFILES.items()}, "synthetic": self.synthetic}
        if isinstance(self.engine, LocalGraphEngine):
            pack["graph"] = self.engine.export((x - radius_km * 1000, y - radius_km * 1000, x + radius_km * 1000, y + radius_km * 1000))
        return pack

    def status(self) -> dict:
        now = self.now()
        sc = self.scenario
        return {"mode": self.settings.mode, "synthetic": self.synthetic, "now": now,
                "scenario": None if sc is None else {"seed": self.seed, "start": sc.start, "end": sc.start + sc.duration_s,
                                                     "elapsed_min": round((now - sc.start) / 60), "peak_rain_mmh": round(sc.params.peak_mmh)},
                "hazard_version": self.store.version, "hazard_updated_at": self.store.updated_at,
                "hazard_age_s": round(now - self.store.updated_at), "slices": len(self.store.slices),
                "pending": len(self.store.pending()), "active_trips": len(self.registry.active()),
                "timings": self.timings, "last_revalidation": self.reval_stats[-1] if self.reval_stats else None,
                "engine": self.engine.name, "assistant": getattr(self.assistant.gen, "name", "custom"),
                "region": {"lon0": self.frame.lon0, "lat0": self.frame.lat0, "width_m": self.width, "height_m": self.height,
                           "bbox": [*self.frame.to_lonlat(0, 0), *self.frame.to_lonlat(self.width, self.height)]},
                "sensors": [{"id": s["id"], "lonlat": self.frame.to_lonlat(*self.terrain.cell_center(*s["rc"])), "depth_m": s.get("depth_m")}
                            for s in getattr(self, "sensors", [])] if self.synthetic else []}
