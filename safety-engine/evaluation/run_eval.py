"""Evaluation harness for the IDF Item 8 / patent specification.

    python -m evaluation.run_eval            # writes evaluation/results/RESULTS.md and results.json

Everything here runs on SYNTHETIC storms (scenario.py) with the test storm
seed (never used to train or calibrate the hazard model). Ground truth comes
from the hidden truth simulator. Numbers demonstrate the mechanisms and their
relative effect; they are not field results.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import time

import numpy as np

from safe_engine.assistant import Assistant
from safe_engine.assistant.planner import RulePlanner
from safe_engine import ponr
from safe_engine.config import Settings
from safe_engine.feasibility import FeasibilityEvaluator, chunk_route
from safe_engine.geometry import _polys_for_mask
from safe_engine.routing.base import Route
from safe_engine.service import EngineService
from safe_engine.slices import HazardSlice, SpatioTemporalIndex, new_id
from safe_engine.trips import Trip
from safe_engine.vehicles import get_profile

OUT = os.path.join(os.path.dirname(__file__), "results")
TEST_SEED = 19
VEHICLES = ["two_wheeler", "car", "truck"]


def pct(xs, q):
    return float(np.percentile(xs, q)) if xs else float("nan")


# ============================================================================ ground truth
def truth_unsafe(svc, route: Route, depart: float, vehicle: str) -> bool:
    veh = get_profile(vehicle)
    T, tr = svc.terrain, svc.truth
    t = depart
    for i, (a, b) in enumerate(zip(route.coords, route.coords[1:])):
        d = route.seg_dur[i] if i < len(route.seg_dur) else 0
        elevated = bool(route.elevated and i < len(route.elevated) and route.elevated[i])
        for f in (0.0, 0.5, 1.0):
            x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            rc = T.rc(x, y)
            if rc is None:
                continue
            tt = t + d * f
            if not elevated and tr.depth_at(tt)[rc] > veh.max_water_m:
                return True
            if tr.landslide_at(tt)[rc]:
                return True
            if tr.wind_at(tt)[rc] > veh.max_wind_kmh:
                return True
        t += d
    return False


def present_tense_index(svc, t: float) -> SpatioTemporalIndex:
    """B1 = original design: hazard polygons as they are *now* (perfectly observed), vehicle-agnostic."""
    T, tr = svc.terrain, svc.truth
    sl = []
    for cls, mask in (("flood", tr.depth_at(t) >= 0.15), ("landslide", tr.landslide_at(t)), ("storm", tr.wind_at(t) >= 50)):
        for g in _polys_for_mask(mask, T.cell, T.cell * 0.25):
            sl.append(HazardSlice(new_id("B"), cls, g, t - 1, t + 1, 1.0, 1.0))
    return SpatioTemporalIndex(sl, 900)


def random_od(rng, svc, min_km=4.0):
    W, H = svc.width, svc.height
    while True:
        o = (rng.uniform(0.05, 0.95) * W, rng.uniform(0.05, 0.95) * H)
        d = (rng.uniform(0.05, 0.95) * W, rng.uniform(0.05, 0.95) * H)
        if math.dist(o, d) >= min_km * 1000:
            return o, d


# ============================================================================ E2 + E3: routing
def exp_routing(svc, n_od=14, times_min=range(60, 361, 30), seed=1):
    rng = random.Random(seed)
    ev: FeasibilityEvaluator = svc.evaluator
    methods = ["B0_fastest", "B1_present_tense", "M3_time_aware_vehicle_agnostic", "M3_full", "M3_full_plus_M4"]
    rec = {m: {"requests": 0, "recommended": 0, "unsafe_recommended": 0, "false_refusal": 0, "extra_time_s": []} for m in methods}
    cand = {m: {"unsafe": 0, "unsafe_rejected": 0, "safe": 0, "safe_rejected": 0} for m in methods[1:4]}
    lat_ms, idx_tests, naive_tests = [], [], []
    detour = {"needed": 0, "found": 0, "found_truly_safe": 0, "ms": [], "queries": [], "oracle_safe_exists": 0}
    svc.reset(TEST_SEED)
    svc.auto_reroute = False
    clock0 = svc.now()
    for tm in times_min:
        svc.advance(clock0 + tm * 60 - svc.now())
        now = svc.now()
        b1 = present_tense_index(svc, now)
        n_slices = len([s for s in svc.store.slices.values() if s.status == "approved"])
        for _ in range(n_od):
            o, d = random_od(rng, svc)
            routes = svc.engine.routes(o, d, 3)
            if not routes:
                continue
            for veh in VEHICLES:
                vp = get_profile(veh)
                truth = [truth_unsafe(svc, r, now, veh) for r in routes]
                fastest_safe = min((r.duration for r, u in zip(routes, truth) if not u), default=None)
                t0 = time.perf_counter()
                s0 = dict(svc.store.index.stats)
                full = [ev.assess(r, now, vp, svc.store.index) for r in routes]
                lat_ms.append((time.perf_counter() - t0) * 1000)
                idx_tests.append(svc.store.index.stats["exact_tests"] - s0["exact_tests"])
                naive_tests.append(sum(len(chunk_route(r, svc.settings.chunk_m)) for r in routes) * n_slices * 2)
                agn = [ev.assess(r, now, vp, svc.store.index, vehicle_aware=False) for r in routes]
                base = [ev.assess(Route(coords=r.coords, seg_dur=r.seg_dur, id=r.id + "b"), now, vp, b1,
                                  time_aware=False, vehicle_aware=False, hard_only=True) for r in routes]
                decisions = {"B0_fastest": [True] * len(routes), "B1_present_tense": [a.feasible for a in base],
                             "M3_time_aware_vehicle_agnostic": [a.feasible for a in agn], "M3_full": [a.feasible for a in full]}
                for m, ok in decisions.items():
                    if m in cand:
                        for u, f in zip(truth, ok):
                            if u:
                                cand[m]["unsafe"] += 1
                                cand[m]["unsafe_rejected"] += (not f)
                            else:
                                cand[m]["safe"] += 1
                                cand[m]["safe_rejected"] += (not f)
                    _score(rec[m], routes, ok, truth, fastest_safe)
                # M3 + M4: detour when every candidate fails
                ok_full = decisions["M3_full"]
                if any(ok_full):
                    _score(rec["M3_full_plus_M4"], routes, ok_full, truth, fastest_safe)
                else:
                    detour["needed"] += 1
                    worst = sorted(zip(routes, full), key=lambda x: (len(x[1].blocked), -x[1].p_safe))[0]
                    res = svc.detour.synthesize(o, d, worst[0], worst[1], now, vp, svc.store)
                    detour["ms"].append(res["ms"]); detour["queries"].append(res["queries"])
                    r2 = res["route"]
                    rec_m = rec["M3_full_plus_M4"]
                    rec_m["requests"] += 1
                    if r2 is not None:
                        detour["found"] += 1
                        u2 = truth_unsafe(svc, r2, now, veh)
                        detour["found_truly_safe"] += (not u2)
                        rec_m["recommended"] += 1
                        rec_m["unsafe_recommended"] += u2
                        if fastest_safe:
                            rec_m["extra_time_s"].append(r2.duration - fastest_safe)
                    elif fastest_safe is not None:
                        rec_m["false_refusal"] += 1
    out = {"methods": {}, "candidates": {}, "latency_ms_k3": {"p50": pct(lat_ms, 50), "p95": pct(lat_ms, 95)},
           "geometry_tests_per_request": {"indexed_mean": statistics.mean(idx_tests), "brute_force_mean": statistics.mean(naive_tests),
                                          "reduction_factor": statistics.mean(naive_tests) / max(1, statistics.mean(idx_tests))}}
    for m, r in rec.items():
        n = max(1, r["requests"])
        out["methods"][m] = {"requests": r["requests"], "recommended": r["recommended"],
                             "unsafe_recommendation_rate": r["unsafe_recommended"] / n,
                             "unsafe_among_recommended": r["unsafe_recommended"] / max(1, r["recommended"]),
                             "false_refusal_rate": r["false_refusal"] / n,
                             "mean_extra_time_vs_fastest_truly_safe_s": statistics.mean(r["extra_time_s"]) if r["extra_time_s"] else 0.0}
    for m, c in cand.items():
        out["candidates"][m] = {"safety_recall": c["unsafe_rejected"] / max(1, c["unsafe"]),
                                "false_rejection_rate": c["safe_rejected"] / max(1, c["safe"]),
                                "unsafe_candidates": c["unsafe"], "safe_candidates": c["safe"]}
    out["M4_detour"] = {"requests_with_all_candidates_infeasible": detour["needed"], "detour_found": detour["found"],
                        "detour_found_and_truly_safe": detour["found_truly_safe"],
                        "latency_ms_p50": pct(detour["ms"], 50), "latency_ms_p95": pct(detour["ms"], 95),
                        "engine_requeries_mean": statistics.mean(detour["queries"]) if detour["queries"] else 0}
    return out


def _score(r, routes, ok, truth, fastest_safe):
    r["requests"] += 1
    picks = [(rt, u) for rt, f, u in zip(routes, ok, truth) if f]
    if picks:
        rt, u = min(picks, key=lambda x: x[0].duration)
        r["recommended"] += 1
        r["unsafe_recommended"] += u
        if fastest_safe is not None and not u:
            r["extra_time_s"].append(rt.duration - fastest_safe)
    elif fastest_safe is not None:
        r["false_refusal"] += 1


# ============================================================================ E4: trip re-validation (M5)
def exp_trips(per_step=300, steps=8, seed=2):
    """Keeps a rolling population of active trips; measures each 15-min model refresh, a single
    admin road closure, and the naive alternative (re-evaluate every active trip)."""
    svc = EngineService(Settings(), scenario_seed=TEST_SEED)
    svc.auto_reroute = False
    svc.advance(30 * 60)
    rng = random.Random(seed)
    model_rows, local, naive = [], [], []
    for _ in range(steps):
        for _ in range(per_step):
            o, d = random_od(rng, svc, 6.0)
            r = svc.engine.routes(o, d, 1)
            if r:
                svc.registry.register(Trip(route=r[0], depart_t=svc.now(), vehicle=rng.choice(VEHICLES), origin=o, dest=d), svc.now())
        x, y = rng.uniform(0.2, 0.8) * svc.width, rng.uniform(0.2, 0.8) * svc.height
        lon, lat = svc.frame.to_lonlat(x, y)
        local.append(svc.admin_add_closure({"type": "Point", "coordinates": [lon, lat]}, "closure", 3, "eval")["revalidation"])
        t0 = time.perf_counter()
        act = svc.registry.active()
        for t in act:
            rem, _ = t.remaining(svc.now())
            svc.evaluator.assess(rem, svc.now(), get_profile(t.vehicle), svc.store.index)
        naive.append({"trips": len(act), "ms": (time.perf_counter() - t0) * 1000})
        svc.advance(15 * 60)
        model_rows.append(dict(svc.reval_stats[-1]))
    frac = [r["re_evaluated"] / max(1, r["active_trips"]) for r in model_rows if r["active_trips"]]
    return {"new_trips_per_step": per_step,
            "model_refresh": {"mean_active": statistics.mean(r["active_trips"] for r in model_rows),
                              "mean_re_evaluated": statistics.mean(r["re_evaluated"] for r in model_rows),
                              "mean_fraction_re_evaluated": statistics.mean(frac) if frac else 0,
                              "mean_ms": statistics.mean(r["ms"] for r in model_rows), "rows": model_rows},
            "naive_all_trips": {"trips": statistics.mean(n["trips"] for n in naive), "ms": statistics.mean(n["ms"] for n in naive)},
            "local_closure": {"mean_active": statistics.mean(r["active_trips"] for r in local),
                              "mean_re_evaluated": statistics.mean(r["re_evaluated"] for r in local),
                              "mean_ms": statistics.mean(r["ms"] for r in local)}}


# ============================================================================ E5: anti-herding
def exp_capacity(n=900, seed=3):
    res = {}
    for gamma in (0.0, 1.5):
        s = Settings()
        s.capacity_gamma = gamma
        s.capacity_veh_per_hr = 150
        svc = EngineService(s, scenario_seed=TEST_SEED)
        svc.auto_reroute = False
        rng = random.Random(seed)
        W, H = svc.width, svc.height
        durs = []
        for _ in range(n):
            o = (W * (0.15 + rng.uniform(-0.03, 0.03)), H * (0.5 + rng.uniform(-0.03, 0.03)))
            d = (W * (0.85 + rng.uniform(-0.03, 0.03)), H * (0.55 + rng.uniform(-0.03, 0.03)))
            rows, chosen, _, _ = svc._plan_xy(o, d, "car", svc.now())
            if chosen:
                t = Trip(route=chosen["route"], depart_t=svc.now(), vehicle="car", origin=o, dest=d)
                svc.registry.register(t, svc.now())
                durs.append(chosen["route"].duration)
        loads = list(svc.registry.load.values())
        res[f"gamma_{gamma}"] = {"assigned": len(durs), "peak_segment_load": max(loads), "peak_load_factor": max(loads) / s.capacity_veh_per_hr,
                                 "segments_over_capacity": sum(1 for v in loads if v > s.capacity_veh_per_hr),
                                 "mean_nominal_duration_s": statistics.mean(durs)}
    return res


# ============================================================================ E6: verification gate (M6)
class NoisyPlanner:
    """Simulates a hallucinating LLM: corrupts safety-critical claims with probability q."""
    name = "noisy-llm-simulator"

    def __init__(self, q=0.5, seed=4):
        self.base, self.q, self.rng, self.corrupted = RulePlanner(), q, random.Random(seed), []

    def propose(self, question, tools, location, destination=None, feedback=None):
        ans = self.base.propose(question, tools, location, destination, feedback)
        self.corrupted = []
        for c in ans["claims"]:
            if c["type"] in ("route_recommendation", "shelter_recommendation", "hazard_warning", "instruction", "no_safe_route") \
                    and self.rng.random() < self.q:
                kind = self.rng.choice(["fabricate", "swap_infeasible", "fabricate_sop"])
                if c["type"] == "instruction" or kind == "fabricate_sop":
                    c["ids"] = ["SOP-INVENTED-9"] if c["type"] == "instruction" else [i + "9" for i in c["ids"]]
                elif kind == "swap_infeasible":
                    bad = [k for k, v in tools.ledger.objects.items() if v["kind"] == "route" and not v["data"]["feasible"]]
                    c["ids"] = [bad[0]] + [i for i in c["ids"] if not i.startswith("R")] if bad and c["type"] != "no_safe_route" else ["R" + "9" * 6]
                    if c["type"] == "no_safe_route":
                        ok = [k for k, v in tools.ledger.objects.items() if v["kind"] == "route" and v["data"]["feasible"]]
                        c["ids"] = ok[:1] or ["R" + "9" * 6]
                else:
                    c["ids"] = [("R" if i.startswith("R") else "H" if i.startswith("H") else "S") + "8" * 6 for i in c["ids"]]
                self.corrupted.append(c)
        return ans


def exp_gate(n_loc=40, seed=5):
    svc = EngineService(Settings(), scenario_seed=TEST_SEED)
    svc.advance(90 * 60)
    rng = random.Random(seed)
    qs = ["Is it safe to go to my destination?", "Where is the nearest shelter?", "Any hazards near me?"]
    langs = ["en", "hi", "ta"]
    noisy = NoisyPlanner(0.5)
    out = {"questions": 0, "corrupted_answers": 0, "ungated_wrong_released": 0, "gated_wrong_released": 0,
           "gated_refusals": 0, "gated_recovered_after_retry": 0, "gate_ms": []}
    for i in range(n_loc):
        o, d = random_od(rng, svc, 3.0)
        loc, dest = list(svc.frame.to_lonlat(*o)), list(svc.frame.to_lonlat(*d))
        for q in qs:
            lang = langs[(i + len(q)) % 3]
            out["questions"] += 1
            asst = Assistant(svc, generator=noisy)
            t0 = time.perf_counter()
            res = asst.ask(q, lang, loc, rng.choice(VEHICLES), dest)
            out["gate_ms"].append((time.perf_counter() - t0) * 1000)
            # which corrupted claims (from ANY attempt) reached the user?
            released_ids = {tuple(b["ids"]) for b in res["blocks"]}
            if res.get("gate") and any(not g["ok"] for g in res["gate"]):
                out["corrupted_answers"] += 1
                out["ungated_wrong_released"] += 1     # without the gate the first (corrupted) answer is shown
            if res.get("refused"):
                out["gated_refusals"] += 1
            elif res["attempts"] > 1:
                out["gated_recovered_after_retry"] += 1
            for c in noisy.corrupted:
                if tuple(c["ids"]) in released_ids:
                    out["gated_wrong_released"] += 1
    # stale data: every route / shelter claim must be refused
    svc.store.updated_at -= svc.settings.hazard_ttl_s + 120
    stale_total, stale_refused = 0, 0
    for i in range(15):
        o, d = random_od(rng, svc, 3.0)
        res = Assistant(svc, generator=RulePlanner()).ask("Is it safe to go to my destination?", "en",
                                                           list(svc.frame.to_lonlat(*o)), "car", list(svc.frame.to_lonlat(*d)))
        stale_total += 1
        stale_refused += not any(b["type"] == "route_recommendation" for b in res["blocks"])
    out["stale_data_route_claims_blocked"] = f"{stale_refused}/{stale_total}"
    out["gate_ms_p50"], out["gate_ms_p95"] = pct(out["gate_ms"], 50), pct(out["gate_ms"], 95)
    del out["gate_ms"]
    return out


# ============================================================================ E7: offline
def exp_offline():
    svc = EngineService(Settings(), scenario_seed=TEST_SEED)
    svc.advance(120 * 60)
    lon, lat = svc.frame.to_lonlat(svc.width / 2, svc.height / 2)
    pk = svc.offline_pack(lon, lat, 5, 3)
    raw = json.dumps(pk).encode()
    import gzip
    p = svc.plan(svc.frame.to_lonlat(svc.width * 0.2, svc.height * 0.3), svc.frame.to_lonlat(svc.width * 0.8, svc.height * 0.7), "truck")
    codes = [r["route_code"] for r in p["routes"]]
    return {"pack_radius_km": 5, "pack_hours": 3, "slices": len(pk["slices"]), "graph_nodes": len(pk.get("graph", {}).get("nodes", {})),
            "pack_kb": round(len(raw) / 1024, 1), "pack_gzip_kb": round(len(gzip.compress(raw)) / 1024, 1),
            "route_code_chars": [len(c) for c in codes]}


# ============================================================================ E8: point of no return (M8)
def exp_ponr(n_od=10, times_min=range(30, 211, 30), seed=6, grace_min=15):
    """Replays the storm. For every recommended route the leave-by deadline is computed, then checked against the
    hidden ground truth: is it really safe to leave exactly at the deadline, and really unsafe a little after it?"""
    svc = EngineService(Settings(), scenario_seed=TEST_SEED)
    svc.auto_reroute = False
    rng = random.Random(seed)
    clock0 = svc.now()
    c = {"requests": 0, "no_safe_route": 0, "clear": 0, "countdown": 0}
    at_deadline_unsafe, after_grace_unsafe, clear_unsafe, errs, ms, n_assess = [], [], [], [], [], []
    now_unsafe, dl_given_safe_now, grace_given_safe_now = [], [], []
    grid = list(range(0, 7200, 300))
    for tm in times_min:
        svc.advance(clock0 + tm * 60 - svc.now())
        now = svc.now()
        for _ in range(n_od):
            o, d = random_od(rng, svc)
            for veh in VEHICLES:
                c["requests"] += 1
                _, chosen, _, _ = svc._plan_xy(o, d, veh, now)
                if chosen is None:
                    c["no_safe_route"] += 1
                    continue
                route = chosen["route"]
                t0 = time.perf_counter()
                lb = ponr.scan(svc, route, veh, now)
                ms.append((time.perf_counter() - t0) * 1000)
                n_assess.append(lb["assessments"])
                window = [g for g in grid if g <= lb["window_s"]]
                truth = [truth_unsafe(svc, route, now + g, veh) for g in window]
                if lb["status"] == "clear":
                    c["clear"] += 1
                    clear_unsafe.append(any(truth))
                    continue
                c["countdown"] += 1
                left = lb["seconds_left"]
                u_now = truth_unsafe(svc, route, now, veh)
                u_dl = truth_unsafe(svc, route, now + left, veh)
                u_gr = truth_unsafe(svc, route, now + left + grace_min * 60, veh)
                now_unsafe.append(u_now)
                at_deadline_unsafe.append(u_dl)
                after_grace_unsafe.append(u_gr)
                if not u_now:                       # the recommendation itself was right: does the deadline hold from here?
                    dl_given_safe_now.append(u_dl)
                    grace_given_safe_now.append(u_gr)
                first_bad = next((g for g, u in zip(window, truth) if u), None)
                if first_bad is not None and first_bad > 0:
                    errs.append(left - (first_bad - 300))      # predicted deadline minus the last truly safe grid delay

    def rate(xs):
        return sum(xs) / len(xs) if xs else float("nan")
    return {"requests": c["requests"], "no_safe_route": c["no_safe_route"], "clear": c["clear"], "countdown": c["countdown"],
            "grace_min": grace_min,
            "unsafe_when_leaving_now": rate(now_unsafe),
            "unsafe_when_leaving_exactly_at_deadline": rate(at_deadline_unsafe),
            "unsafe_when_leaving_grace_after_deadline": rate(after_grace_unsafe),
            "given_safe_now": {"n": len(dl_given_safe_now), "unsafe_at_deadline": rate(dl_given_safe_now),
                               "unsafe_grace_after_deadline": rate(grace_given_safe_now)},
            "clear_but_truly_unsafe_inside_window": rate(clear_unsafe),
            "deadline_vs_truth": {"n": len(errs), "conservative_share": rate([e <= 300 for e in errs]),
                                  "median_error_min": statistics.median(errs) / 60 if errs else float("nan"),
                                  "mean_error_min": statistics.mean(errs) / 60 if errs else float("nan")},
            "scan_ms_p50": pct(ms, 50), "scan_ms_p95": pct(ms, 95), "assessments_mean": statistics.mean(n_assess) if n_assess else 0}


# ============================================================================ main
def main():
    import sys
    os.makedirs(OUT, exist_ok=True)
    if "--only-e8" in sys.argv:
        # add / refresh E8 without re-running E1-E7 (their numbers in results.json are kept)
        with open(os.path.join(OUT, "results.json")) as fh:
            results = json.load(fh)
        print("E8 point of no return…", flush=True)
        results["E8_point_of_no_return"] = exp_ponr()
        with open(os.path.join(OUT, "results.json"), "w") as fh:
            json.dump(results, fh, indent=2, default=float)
        from evaluation.report import write_markdown
        write_markdown(results, os.path.join(OUT, "RESULTS.md"))
        print(json.dumps(results["E8_point_of_no_return"], indent=2, default=float))
        return
    t0 = time.time()
    results = {"note": "SYNTHETIC scenario (test storm seed %d, unseen in training/calibration)." % TEST_SEED}
    with open(os.path.join(os.path.dirname(__file__), "..", "models", "hazard_model_metrics.json")) as fh:
        results["E1_hazard_model"] = json.load(fh)
    print("E2/E3 routing…", flush=True)
    svc = EngineService(Settings(), scenario_seed=TEST_SEED)
    results["E2_E3_routing"] = exp_routing(svc)
    print("E2b operating points…", flush=True)
    import safe_engine.geometry as G
    sweep = {}
    for name, (pmin, phard, alpha) in {"balanced": (0.15, 0.5, 0.90), "default (conservative)": (0.10, 0.30, 0.97)}.items():
        G.P_MIN = pmin
        s = Settings(); s.p_hard_block = phard; s.alpha = alpha
        rr = exp_routing(EngineService(s, scenario_seed=TEST_SEED), n_od=8, times_min=range(60, 361, 60), seed=9)
        sweep[name] = {"p_min": pmin, "p_hard": phard, "alpha": alpha,
                       "M3_unsafe": rr["methods"]["M3_full"]["unsafe_recommendation_rate"],
                       "M3_false_refusal": rr["methods"]["M3_full"]["false_refusal_rate"],
                       "B1_unsafe": rr["methods"]["B1_present_tense"]["unsafe_recommendation_rate"],
                       "B1_false_refusal": rr["methods"]["B1_present_tense"]["false_refusal_rate"]}
    G.P_MIN = 0.10
    results["E2b_operating_points"] = sweep
    print("E4 trips…", flush=True)
    results["E4_trip_revalidation"] = exp_trips()
    print("E5 capacity…", flush=True)
    results["E5_anti_herding"] = exp_capacity()
    print("E6 gate…", flush=True)
    results["E6_verification_gate"] = exp_gate()
    print("E7 offline…", flush=True)
    results["E7_offline"] = exp_offline()
    print("E8 point of no return…", flush=True)
    results["E8_point_of_no_return"] = exp_ponr()
    results["runtime_s"] = round(time.time() - t0)
    with open(os.path.join(OUT, "results.json"), "w") as fh:
        json.dump(results, fh, indent=2, default=float)
    from evaluation.report import write_markdown
    write_markdown(results, os.path.join(OUT, "RESULTS.md"))
    print(json.dumps({k: v for k, v in results.items() if k != "E1_hazard_model"}, indent=2, default=float)[:6000])


if __name__ == "__main__":
    main()
