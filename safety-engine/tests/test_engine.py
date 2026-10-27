"""Unit and integration tests for the safety engine (run: pytest -q)."""
from __future__ import annotations

import math

import httpx
import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from safe_engine.assistant.gate import verify
from safe_engine.assistant.tools import Ledger, Tools
from safe_engine.config import Settings
from safe_engine.feasibility import FeasibilityEvaluator, chunk_route
from safe_engine.fusion import EvidenceFusion, apply_evidence
from safe_engine.geography import PlaceLookup, parse_distance_question, parse_place_question
from safe_engine.geo import LocalFrame
from safe_engine.offline import decode_points, encode_points
from safe_engine.routing.base import Route
from safe_engine.slices import HazardSlice, HazardStore, SpatioTemporalIndex, brute_force_query, new_id
from safe_engine.terrain import TerrainGrid
from safe_engine.trips import Trip, TripRegistry
from safe_engine.vehicles import get_profile

FRAME = LocalFrame(80.15, 12.95)


# ----------------------------------------------------------------------------- terrain
def test_priority_flood_fills_closed_depression():
    z = np.full((7, 7), 5.0)
    z[3, 3] = 3.0                      # 2 m pit
    z[2:5, 2:5] = np.minimum(z[2:5, 2:5], 4.0)
    z[3, 3] = 3.0
    t = TerrainGrid(FRAME, 10.0, z).derive(river_acc_cells=1000)
    assert t.fill_depth[3, 3] == pytest.approx(2.0)
    assert t.fill_depth[0, 0] == 0.0
    assert t.acc.max() >= 1


# ----------------------------------------------------------------------------- index
def _slice(geom, t0, t1, cls="flood", p=0.9, inten=0.6):
    return HazardSlice(new_id(), cls, geom, t0, t1, p, inten)


def test_index_matches_brute_force():
    rng = np.random.default_rng(0)
    sl = [_slice(box(x, y, x + 200, y + 200), t, t + 900) for x, y, t in
          zip(rng.uniform(0, 5000, 300), rng.uniform(0, 5000, 300), rng.choice([0, 900, 1800, 2700], 300))]
    idx = SpatioTemporalIndex(sl, 900)
    for _ in range(50):
        g = LineString([(rng.uniform(0, 5000), rng.uniform(0, 5000)), (rng.uniform(0, 5000), rng.uniform(0, 5000))])
        t0 = rng.uniform(0, 3000)
        a = {s.id for s in idx.query(g, t0, t0 + 300)}
        b = {s.id for s in brute_force_query(sl, g, t0, t0 + 300, {})}
        assert a == b
    assert idx.stats["exact_tests"] < 50 * 300  # the index prunes most candidates


def test_hazard_geojson_includes_forecasts_over_requested_window():
    from safe_engine.service import EngineService

    svc = object.__new__(EngineService)
    svc.frame = FRAME
    svc._now = 1000
    current = _slice(box(0, 0, 100, 100), 900, 1100)
    future = _slice(box(200, 200, 300, 300), 1300, 1600)
    later = _slice(box(400, 400, 500, 500), 1700, 1900)
    svc.store = type("Store", (), {"version": 1, "slices": {s.id: s for s in (current, future, later)}})()
    svc.scenario = None

    geojson = svc.hazards_geojson(1000, t_end=1600)

    assert {f["properties"]["id"] for f in geojson["features"]} == {current.id, future.id}


# ----------------------------------------------------------------------------- feasibility (M3)
def _straight_route(length=3000, speed=10.0, elevated=None):
    xs = np.linspace(0, length, 31)
    coords = [(x, 0.0) for x in xs]
    durs = [(xs[1] - xs[0]) / speed] * 30
    return Route(coords=coords, seg_dur=durs, elevated=elevated)


def test_time_aware_ignores_hazard_that_ends_before_arrival_and_catches_one_that_starts():
    s = Settings()
    ev = FeasibilityEvaluator(s)
    r = _straight_route()                           # 300 s long, hazard at x=2500 reached at ~250 s
    ends_early = _slice(box(2400, -50, 2600, 50), 0, 60)
    starts_later = _slice(box(2400, -50, 2600, 50), 200, 4000)
    car = get_profile("car")
    a = ev.assess(r, 0, car, SpatioTemporalIndex([ends_early], 900))
    assert a.feasible
    b = ev.assess(r, 0, car, SpatioTemporalIndex([starts_later], 900))
    assert not b.feasible
    # the present-tense baseline gets both wrong
    assert not ev.assess(r, 0, car, SpatioTemporalIndex([ends_early], 900), time_aware=False).feasible
    assert ev.assess(r, 0, car, SpatioTemporalIndex([starts_later], 900), time_aware=False).feasible


def test_vehicle_thresholds_and_margin():
    ev = FeasibilityEvaluator(Settings())
    r = _straight_route()
    shallow = _slice(box(1400, -50, 1600, 50), 0, 4000, inten=0.30)
    idx = SpatioTemporalIndex([shallow], 900)
    assert not ev.assess(r, 0, get_profile("two_wheeler"), idx).feasible
    assert not ev.assess(r, 0, get_profile("car"), idx).feasible
    assert ev.assess(r, 0, get_profile("truck"), idx).feasible
    later = _slice(box(1400, -50, 1600, 50), 3000, 6000, inten=0.9)
    a = ev.assess(r, 0, get_profile("car"), SpatioTemporalIndex([later], 900))
    assert a.feasible and a.margin_s is not None and 2500 < a.margin_s < 3000


def test_bridge_segments_are_exempt_from_ponding_only():
    ev = FeasibilityEvaluator(Settings())
    elev = [10 <= i <= 20 for i in range(30)]
    r = _straight_route(elevated=elev)
    flood = _slice(box(1300, -50, 1700, 50), 0, 4000, inten=0.9)
    wind = _slice(box(1300, -50, 1700, 50), 0, 4000, cls="storm", inten=130)
    assert ev.assess(r, 0, get_profile("car"), SpatioTemporalIndex([flood], 900)).feasible
    assert not ev.assess(r, 0, get_profile("car"), SpatioTemporalIndex([wind], 900)).feasible


def test_soft_risk_chance_constraint():
    ev = FeasibilityEvaluator(Settings())
    r = _straight_route()
    soft = [_slice(box(x, -50, x + 100, 50), 0, 4000, p=0.2, inten=0.9) for x in (500, 1500)]
    a = ev.assess(r, 0, get_profile("car"), SpatioTemporalIndex(soft, 900))
    assert not a.feasible and not a.blocked and a.p_safe == pytest.approx(0.64, abs=0.02)


# ----------------------------------------------------------------------------- store / trips (M5)
def test_store_versions_and_trip_lookup_is_local():
    store = HazardStore(900)
    reg = TripRegistry(250)
    trips = []
    for k in range(20):
        y = k * 1000.0
        r = Route(coords=[(0, y), (5000, y)], seg_dur=[500.0])
        t = Trip(route=r, depart_t=0, vehicle="car", origin=r.coords[0], dest=r.coords[-1])
        reg.register(t, 0)
        trips.append(t)
    closure = HazardSlice(new_id("A"), "closure", box(2400, 2900, 2600, 3100), 0, 3600, 1.0, 1.0, source="admin")
    diff = store.add_manual(closure, 0, "admin@test")
    hit = reg.affected(diff.parts)
    assert hit == {trips[3].id}
    # a closure that starts after every trip has passed affects nobody
    late = HazardSlice(new_id("A"), "closure", box(2400, 2900, 2600, 3100), 5000, 9000, 1.0, 1.0, source="admin")
    assert reg.affected(store.add_manual(late, 0, "a").parts) == set()


def test_pending_slices_do_not_affect_routing_until_approved():
    store = HazardStore(900)
    s = HazardSlice(new_id("C"), "flood", box(0, 0, 100, 100), 0, 3600, 0.6, 0.45, source="crowd", status="pending")
    store.add_manual(s, 0, "u")
    assert store.index.query(Point(50, 50), 0, 10) == []
    store.set_status(s.id, "approved", 1, "admin")
    assert len(store.index.query(Point(50, 50), 0, 10)) == 1


# ----------------------------------------------------------------------------- fusion
def test_crowd_dedup_reputation_and_noisy_or():
    f = EvidenceFusion(FRAME)
    e1, s1, dup1 = f.add_crowd(100, 100, "flood", 0, "u1", "knee")
    e2, s2, dup2 = f.add_crowd(120, 110, "flood", 60, "u1", "knee")
    assert not dup1 and dup2 and s2 is None
    f.add_crowd(130, 90, "flood", 60, "u2", "knee")
    f.add_crowd(90, 130, "flood", 60, "u3", "knee")
    assert f.corroboration(e1, 60) == 3
    before = f.rep("u1")
    f.feedback("u1", True)
    assert f.rep("u1") > before
    p = np.array([0.1])
    assert apply_evidence(p, np.array([0.8]), 0)[0] > 0.4
    assert apply_evidence(p, np.array([0.8]), 7200)[0] < apply_evidence(p, np.array([0.8]), 0)[0]


CAP = """<?xml version="1.0"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>T1</identifier><info>
<event>Heavy rain and flooding</event><severity>Severe</severity><certainty>Likely</certainty>
<area><areaDesc>test</areaDesc><polygon>12.96,80.16 12.96,80.20 13.00,80.20 13.00,80.16 12.96,80.16</polygon>
<circle>13.05,80.25 1.5</circle></area></info></alert>"""


def test_cap_parsing():
    f = EvidenceFusion(FRAME)
    items = f.add_cap_xml(CAP, 0)
    assert len(items) == 2 and all(i.hclass == "flood" for i in items)
    assert items[0].confidence == pytest.approx(0.85 * 0.8)
    assert items[1].geom.area == pytest.approx(math.pi * 1500 ** 2, rel=0.02)


# ----------------------------------------------------------------------------- offline codec
def test_route_code_roundtrip_and_checksum():
    pts = [(80.27071, 13.08268), (80.2501, 13.0402), (80.21234, 13.01111)]
    code = encode_points(pts, 3)
    back, v = decode_points(code)
    assert v == 3 and all(abs(a[0] - b[0]) < 1e-5 and abs(a[1] - b[1]) < 1e-5 for a, b in zip(pts, back))
    assert len(code) < 40
    bad = code[:-3] + ("A" if code[-3] != "A" else "B") + code[-2:]
    with pytest.raises(ValueError):
        decode_points(bad)


# ----------------------------------------------------------------------------- full service + gate (M6)
@pytest.fixture(scope="module")
def svc():
    from safe_engine.service import EngineService
    s = EngineService(Settings())
    s.advance(75 * 60)
    return s


def _ll(s, fx, fy):
    return s.frame.to_lonlat(s.width * fx, s.height * fy)


def test_plan_returns_assessed_routes(svc):
    p = svc.plan(_ll(svc, 0.6, 0.4), _ll(svc, 0.9, 0.6), "truck")
    assert p["routes"] and all("feasible" in r for r in p["routes"])
    assert p["synthetic"] is True
    if p["chosen"]:
        dr = p["decision_record"]
        assert dr and dr["route_id"] == p["chosen"] and "narrative" in dr and "counterfactual_departures" in dr


def test_explain_and_point_forecast(svc):
    loc = _ll(svc, 0.55, 0.45)
    fc = svc.forecast_point(loc[0], loc[1], 60)
    assert fc and fc["probabilities"]["flood_depth_ge_15cm"] >= 0
    assert "flood" in fc["attribution"] and "landslide" in fc["attribution"]
    p = svc.plan(loc, _ll(svc, 0.85, 0.55), "car")
    rid = p["chosen"] or p["routes"][0]["id"]
    ex = svc.explain_route(rid, "en")
    assert ex["route_id"] == rid and ex["method"]["time_aware"] is True


def test_gate_rejects_fabricated_and_stale_claims(svc):
    loc, dest = _ll(svc, 0.6, 0.4), _ll(svc, 0.9, 0.6)
    ledger = Ledger()
    tools = Tools(svc, ledger, "truck", "en")
    res = tools.get_route(loc, dest)
    tools.get_sop("flood")
    now = svc.now()
    fake = verify({"claims": [{"type": "route_recommendation", "ids": ["R999999"]}]}, ledger, svc, now, "en", "truck", loc)
    assert not fake.ok and "not returned by any tool" in fake.failures[0]
    bad_sop = verify({"claims": [{"type": "instruction", "ids": ["SOP-MADE-UP"]}]}, ledger, svc, now, "en", "truck", loc)
    assert not bad_sop.ok
    infeasible = [r["id"] for r in res["routes"] if not r["feasible"]]
    if infeasible:
        g = verify({"claims": [{"type": "route_recommendation", "ids": [infeasible[0]]}]}, ledger, svc, now, "en", "truck", loc)
        assert not g.ok
    if res["chosen"]:
        g = verify({"claims": [{"type": "route_recommendation", "ids": [res["chosen"]]}, {"type": "instruction", "ids": ["SOP-FLOOD-01"]}],
                    "explanation": "Take the bridge road. Rain is easing."}, ledger, svc, now, "en", "truck", loc)
        assert g.ok and g.explanation == "Rain is easing."
        late = verify({"claims": [{"type": "route_recommendation", "ids": [res["chosen"]]}]}, ledger, svc,
                      now + svc.settings.hazard_ttl_s + 60, "en", "truck", loc)
        assert not late.ok


def test_assistant_end_to_end_multilingual(svc):
    loc = _ll(svc, 0.6, 0.4)
    for lang in ("en", "hi", "ta"):
        a = svc.assistant.ask("Any hazards near me?", lang, loc, "car")
        assert a["blocks"] and all(b["verified"] for b in a["blocks"])


def test_assistant_general_questions_without_location(svc):
    general = svc.assistant.ask("What is a flood?", location=None)
    assert general["gate_passed"] and not general["verified"]
    assert general["blocks"][0]["type"] == "general_information"
    assert not any(word in general["blocks"][0]["text"].lower() for word in ("avoid", "stay", "drive"))

    local = svc.assistant.ask("Any hazards near me?", location=None)
    assert local["gate_passed"] and local["verified"]
    assert local["blocks"][0]["type"] == "needs_location"


def test_place_distance_question_uses_geocoder_and_road_router(svc):
    places = {
        "Mumbai": {"place_id": 1, "name": "Mumbai", "display_name": "Mumbai, Maharashtra, India",
                   "lat": "19.076", "lon": "72.8777", "address": {"country": "India"}},
        "Lucknow": {"place_id": 2, "name": "Lucknow", "display_name": "Lucknow, Uttar Pradesh, India",
                    "lat": "26.8467", "lon": "80.9462", "address": {"country": "India"}},
    }

    def handler(request):
        if request.url.path.endswith("/search"):
            place = places.get(request.url.params["q"])
            return httpx.Response(200, json=[place] if place else [])
        if "/route/v1/driving/" in request.url.path:
            return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 1_438_993.3, "duration": 62_447.7}]})
        return httpx.Response(404)

    svc.place_lookup = PlaceLookup(
        "https://geo.test", "https://route.test", "test-agent",
        client=httpx.Client(transport=httpx.MockTransport(handler)), min_geocode_interval_s=0,
    )
    assert parse_distance_question("whats distance between mumbai and lucknow") == ("mumbai", "lucknow")
    assert parse_place_question("where is Mumbai located") == "Mumbai"
    answer = svc.assistant.ask("whats distance between Mumbai and Lucknow", location=None)
    assert answer["gate_passed"] and answer["verified"]
    text = answer["blocks"][0]["text"]
    assert "Mumbai, Maharashtra, India to Lucknow, Uttar Pradesh, India" in text
    assert "1,439 km by road" in text
    assert "not live traffic" in text
    assert "SOP-" not in text
    place_answer = svc.assistant.ask("Where is Mumbai located?", location=None)
    assert place_answer["gate_passed"] and place_answer["verified"]
    assert "Mumbai, Maharashtra, India" in place_answer["blocks"][0]["text"]
    assert "19.0760° N, 72.8777° E" in place_answer["blocks"][0]["text"]


def test_report_creates_pending_slice_and_admin_approval_revalidates(svc):
    loc = _ll(svc, 0.5, 0.5)
    r = svc.report(loc[0], loc[1], "flood", "knee", "tester", "test")
    assert svc.store.get(r["slice_id"]).status == "pending"
    out = svc.admin_set(r["slice_id"], "approved", "admin@test")
    assert out["status"] == "approved" and out["revalidation"] is not None


def test_api_smoke():
    from fastapi.testclient import TestClient
    from safe_engine import api
    c = TestClient(api.app)
    assert c.get("/health").status_code == 200
    assert c.get("/vehicles").json()[0]["key"]
    assert c.get("/hazards").json()["type"] == "FeatureCollection"
    assert "Flood Prediction Lab" in c.get("/predictions.html").text
    assert c.get("/predictions.js").status_code == 200
    st = c.get("/status").json()
    bb = st["region"]["bbox"]
    assert c.get(f"/forecast/point?lon={(bb[0]+bb[2])/2}&lat={(bb[1]+bb[3])/2}").status_code == 200
