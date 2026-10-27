"""Tools the language model may call. Every object returned carries an id and a
timestamp and is recorded in a per-question ledger; the verification gate only
accepts claims that cite ledger objects."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from ..sop import SOP, sop_for

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_route", "description": "Plan and safety-check routes between two points for a vehicle. Returns route ids with feasibility, safety probability and margin.",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "array", "items": {"type": "number"}, "description": "[lon, lat]"},
            "destination": {"type": "array", "items": {"type": "number"}, "description": "[lon, lat]"},
            "vehicle": {"type": "string", "enum": ["two_wheeler", "car", "suv", "ambulance", "bus", "truck"]}},
            "required": ["origin", "destination"]}}},
    {"type": "function", "function": {
        "name": "get_place", "description": "Look up a real place name and return its mapped location and approximate coordinates.",
        "parameters": {"type": "object", "properties": {"place": {"type": "string"}},
            "required": ["place"]}}},
    {"type": "function", "function": {
        "name": "get_place_distance", "description": "Look up two real place names and return straight-line and approximate driving distances.",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string"}, "destination": {"type": "string"}},
            "required": ["origin", "destination"]}}},
    {"type": "function", "function": {
        "name": "get_hazards", "description": "Active and predicted hazard slices near a point.",
        "parameters": {"type": "object", "properties": {
            "lon": {"type": "number"}, "lat": {"type": "number"}, "radius_km": {"type": "number"},
            "minutes_ahead": {"type": "number"}}, "required": ["lon", "lat"]}}},
    {"type": "function", "function": {
        "name": "get_shelters", "description": "Nearest relief shelters with a safety-checked route to each.",
        "parameters": {"type": "object", "properties": {
            "lon": {"type": "number"}, "lat": {"type": "number"}, "vehicle": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["lon", "lat"]}}},
    {"type": "function", "function": {
        "name": "get_route_explanation", "description": "Auditable decision record for a route id (why safe/unsafe, blockers, counterfactual departures).",
        "parameters": {"type": "object", "properties": {
            "route_id": {"type": "string"}, "lang": {"type": "string", "enum": ["en", "hi", "ta"]}},
            "required": ["route_id"]}}},
    {"type": "function", "function": {
        "name": "get_point_forecast", "description": "Cell-level hazard probabilities and feature attribution at a lon/lat for a horizon in minutes.",
        "parameters": {"type": "object", "properties": {
            "lon": {"type": "number"}, "lat": {"type": "number"}, "horizon_min": {"type": "integer"}},
            "required": ["lon", "lat"]}}},
    {"type": "function", "function": {
        "name": "get_sop", "description": "Official-guidance clauses for a hazard type in a language.",
        "parameters": {"type": "object", "properties": {
            "hazard": {"type": "string", "enum": ["flood", "landslide", "storm", "any"]},
            "lang": {"type": "string", "enum": ["en", "hi", "ta"]}}, "required": ["hazard"]}}},
]


@dataclass
class Ledger:
    objects: dict = field(default_factory=dict)

    def put(self, oid: str, kind: str, ts: float, data: dict, version: int | None = None):
        self.objects[oid] = {"kind": kind, "ts": ts, "version": version, "data": data}

    def get(self, oid: str):
        return self.objects.get(oid)


class Tools:
    def __init__(self, service, ledger: Ledger, default_vehicle: str = "car", lang: str = "en",
                 hazard_horizon_min: int = 120):
        self.svc, self.ledger, self.vehicle, self.lang = service, ledger, default_vehicle, lang
        self.hazard_horizon_min = hazard_horizon_min

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name == "call":
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except Exception as e:  # tool errors are returned to the model, not raised
            return {"error": str(e)}

    def get_route(self, origin, destination, vehicle=None):
        now = self.svc.now()
        res = self.svc.plan(origin, destination, vehicle or self.vehicle, now)
        out = []
        for r in res["routes"]:
            self.ledger.put(r["id"], "route", now, {**r, "vehicle": vehicle or self.vehicle, "destination": destination}, self.svc.store.version)
            out.append({k: r[k] for k in ("id", "feasible", "p_safe", "margin_s", "duration_s", "distance_m", "reason", "chosen")})
        qid = f"Q{len(self.ledger.objects) + 1:05d}"
        self.ledger.put(qid, "route_query", now, {"route_ids": [r["id"] for r in res["routes"]], "any_feasible": res["chosen"] is not None})
        return {"query_id": qid, "routes": out, "chosen": res["chosen"], "hazard_data_age_s": res["hazard_data_age_s"]}

    def get_place_distance(self, origin: str, destination: str):
        now = self.svc.now()
        result = self.svc.place_lookup.distance_between(origin, destination)
        pid = f"P{len(self.ledger.objects) + 1:05d}"
        self.ledger.put(pid, "place_distance", now, result)
        return {"place_distance_id": pid, **result}

    def get_place(self, place: str):
        now = self.svc.now()
        result = self.svc.place_lookup.find_place(place)
        pid = f"P{len(self.ledger.objects) + 1:05d}"
        self.ledger.put(pid, "place_lookup", now, result)
        return {"place_id": pid, **result}

    def get_hazards(self, lon, lat, radius_km=3, minutes_ahead=None):
        now = self.svc.now()
        minutes_ahead = self.hazard_horizon_min if minutes_ahead is None else min(
            max(minutes_ahead, 0), self.hazard_horizon_min
        )
        items = self.svc.hazards_near(lon, lat, radius_km * 1000, now, now + minutes_ahead * 60)
        for h in items:
            self.ledger.put(h["id"], "hazard", now, h, self.svc.store.version)
        slim = [{k: h[k] for k in ("id", "class", "probability", "intensity", "unit", "t_start", "t_end", "distance_m")} for h in items[:15]]
        return {"hazards": slim, "count": len(items)}

    def get_shelters(self, lon, lat, vehicle=None, n=3):
        now = self.svc.now()
        out = []
        for s in self.svc.nearest_shelters(lon, lat, n):
            res = self.svc.plan([lon, lat], [s["lon"], s["lat"]], vehicle or self.vehicle, now)
            ch = res["chosen"]
            self.ledger.put(s["id"], "shelter", now, s)
            for r in res["routes"]:
                self.ledger.put(r["id"], "route", now, {**r, "vehicle": vehicle or self.vehicle, "destination": [s["lon"], s["lat"]], "shelter_id": s["id"]}, self.svc.store.version)
            out.append({"shelter_id": s["id"], "name": s["name"], "straight_km": round(s["distance_m"] / 1000, 2),
                        "route_id": ch, "reachable": ch is not None})
        return {"shelters": out}

    def get_route_explanation(self, route_id: str, lang=None):
        now = self.svc.now()
        rec = self.svc.explain_route(route_id, lang or self.lang)
        if rec is None:
            return {"error": "unknown route id"}
        qid = f"Q{len(self.ledger.objects) + 1:05d}"
        self.ledger.put(qid, "explanation", now, rec, self.svc.store.version)
        slim = {k: rec[k] for k in ("route_id", "feasible", "p_safe", "margin_s", "reason", "narrative", "counterfactual_departures", "baseline_present_tense")}
        slim["blockers"] = [{"class": b["class"], "p": b["p"], "arrival_time": b["arrival_time"], "summary": b["summary"]} for b in rec.get("blockers", [])]
        return {"explanation_id": qid, **slim}

    def get_point_forecast(self, lon, lat, horizon_min=60):
        now = self.svc.now()
        fc = self.svc.forecast_point(lon, lat, horizon_min)
        if fc is None:
            return {"error": "outside forecast region"}
        fid = f"F{len(self.ledger.objects) + 1:05d}"
        self.ledger.put(fid, "forecast", now, fc, self.svc.store.version)
        return {"forecast_id": fid, **fc}

    def get_sop(self, hazard="any", lang=None):
        now = self.svc.now()
        items = sop_for(hazard, lang or self.lang)
        for c in items:
            self.ledger.put(c["id"], "sop", now, c)
        return {"clauses": items}
