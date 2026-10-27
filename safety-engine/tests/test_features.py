"""Tests for the point-of-no-return warning (M8), the SMS fallback and the risk heatmap.

    pytest -q tests/test_features.py
"""
from __future__ import annotations

import string
import types

import numpy as np
import pytest
from shapely.geometry import Point, box

from safe_engine import ponr, sms
from safe_engine.assistant import templates as TPL
from safe_engine.config import Settings
from safe_engine.feasibility import FeasibilityEvaluator
from safe_engine.routing.base import Route
from safe_engine.slices import HazardSlice, SpatioTemporalIndex, new_id
from safe_engine.vehicles import get_profile

LANGS = ("en", "hi", "ta")


# ============================================================================ helpers
def _route(length=3000.0, speed=10.0):
    xs = np.linspace(0, length, 31)
    return Route(coords=[(x, 0.0) for x in xs], seg_dur=[(xs[1] - xs[0]) / speed] * 30)


def _slice(t0, t1, x0=2400, x1=2600, cls="flood", p=0.9, inten=0.6):
    return HazardSlice(new_id(), cls, box(x0, -50, x1, 50), t0, t1, p, inten)


class _Svc:
    """The few members ponr.scan needs from EngineService."""

    def __init__(self, slices, now=0.0, horizons=(7200,)):
        self.settings = Settings()
        self._now = now
        self.horizons = list(horizons)
        self.store = types.SimpleNamespace(updated_at=now, index=SpatioTemporalIndex(slices, 900))
        self.evaluator = FeasibilityEvaluator(self.settings)

    def now(self):
        return self._now


def _oracle_deadline(svc, route, vehicle, limit=6000):
    """Brute force, one-second steps: last delay before the first infeasible one."""
    veh, last = get_profile(vehicle), None
    for d in range(0, limit):
        if not svc.evaluator.assess(route, float(d), veh, svc.store.index).feasible:
            return last
        last = d
    return None


# ============================================================================ M8 point of no return
def test_deadline_matches_one_second_brute_force():
    svc = _Svc([_slice(1800, 4000)])
    r = _route()
    res = ponr.scan(svc, r, "car", 0.0)
    oracle = _oracle_deadline(svc, r, "car")
    assert res["status"] == "countdown" and oracle is not None
    assert oracle - ponr.RESOLUTION_S <= res["seconds_left"] <= oracle        # never later than the true deadline
    assert res["leave_by_t"] == pytest.approx(res["seconds_left"])
    assert res["hazard_classes"] == ["flood"]


def test_route_that_takes_longer_must_leave_earlier():
    svc = _Svc([_slice(3000, 6000)])
    fast = ponr.scan(svc, _route(speed=10.0), "car", 0.0)
    slow = ponr.scan(svc, _route(speed=5.0), "car", 0.0)
    assert fast["status"] == slow["status"] == "countdown"
    assert slow["seconds_left"] < fast["seconds_left"] - 120


def test_vehicle_decides_whether_there_is_a_deadline():
    svc = _Svc([_slice(1800, 4000, inten=0.30)])           # 30 cm of water
    assert ponr.scan(svc, _route(), "truck", 0.0)["status"] == "clear"
    assert ponr.scan(svc, _route(), "car", 0.0)["status"] == "countdown"
    two = ponr.scan(svc, _route(), "two_wheeler", 0.0)
    car = ponr.scan(svc, _route(), "car", 0.0)
    assert two["status"] == "countdown" and two["seconds_left"] == car["seconds_left"]


def test_clear_when_nothing_is_forecast():
    res = ponr.scan(_Svc([]), _route(), "car", 0.0)
    assert res["status"] == "clear" and res["seconds_left"] is None and res["leave_by_t"] is None
    assert res["urgency"] == "green" and res["window_s"] > 3600


def test_blocked_now_reports_when_it_reopens():
    svc = _Svc([_slice(0, 2400)])                          # water on the road right now, receding at 40 min
    res = ponr.scan(svc, _route(), "car", 0.0)
    assert res["status"] == "blocked" and res["urgency"] == "red" and res["seconds_left"] is None
    assert res["reopens_in_s"] is not None and 2400 - 600 < res["reopens_in_s"] < 2400 + 600
    # a hazard that outlasts the forecast never reopens
    forever = ponr.scan(_Svc([_slice(0, 9000)]), _route(), "car", 0.0)
    assert forever["status"] == "blocked" and forever["reopens_in_s"] is None


def test_first_cutoff_is_reported_even_if_the_road_reopens_later():
    res = ponr.scan(_Svc([_slice(1800, 3000)]), _route(), "car", 0.0)
    assert res["status"] == "countdown" and res["reopens_in_s"] is not None
    assert res["reopens_in_s"] > res["seconds_left"]


def test_urgency_bands():
    def at(t0):
        return ponr.scan(_Svc([_slice(t0, t0 + 3000)]), _route(), "car", 0.0)
    assert at(7000)["urgency"] == "green"
    assert at(1500)["urgency"] == "amber"
    assert at(900)["urgency"] == "red"


def test_window_is_limited_by_the_forecast_horizon():
    svc = _Svc([], now=0.0, horizons=(900,))               # forecast reaches only 15 min ahead
    res = ponr.scan(svc, _route(), "car", 0.0)
    assert res["forecast_limited"] and res["window_s"] < 900
    late = ponr.scan(svc, _route(), "car", 800.0)          # start so late that arrival is beyond the forecast
    assert late["window_s"] == 0 and late["forecast_limited"]


def test_stale_hazard_data_is_stricter_not_looser():
    svc = _Svc([_slice(0, 9000, p=0.2)])                   # 20% soft risk only: feasible at alpha 0.97? no, 0.8 < 0.97
    assert ponr.scan(svc, _route(), "car", 0.0)["status"] == "blocked"
    soft = _Svc([_slice(0, 9000, p=0.02)])                 # 2%: feasible when fresh (0.98 >= 0.97) ...
    assert ponr.scan(soft, _route(), "car", 0.0)["status"] == "clear"
    soft.store.updated_at = -soft.settings.hazard_ttl_s - 10   # ... but not when the data are stale
    stale = ponr.scan(soft, _route(), "car", 0.0)
    assert stale["stale"] and stale["status"] == "blocked"
    assert stale["hazard_classes"] == ["risk"]


def _fields(tpl: str) -> set:
    return {f for _, f, _, _ in string.Formatter().parse(tpl) if f}


def test_ponr_templates_complete_and_consistent_across_languages():
    keys = [k for k in TPL.T if k.startswith("ponr_")]
    assert len(keys) == 7
    for k in keys:
        assert set(TPL.T[k]) == set(LANGS)
        assert len({frozenset(_fields(TPL.T[k][lg])) for lg in LANGS}) == 1, k       # same placeholders in en / hi / ta
    for h in ("flood", "landslide", "storm", "closure", "risk"):
        assert set(TPL.HAZARD_NAMES[h]) == set(LANGS)


@pytest.mark.parametrize("lang", LANGS)
def test_messages_render_in_every_language(lang):
    cases = [
        ponr.scan(_Svc([_slice(7000, 9000)]), _route(), "car", 0.0),     # green countdown
        ponr.scan(_Svc([_slice(1500, 5000)]), _route(), "car", 0.0),     # amber countdown
        ponr.scan(_Svc([_slice(900, 5000)]), _route(), "car", 0.0),      # red -> "leave now"
        ponr.scan(_Svc([]), _route(), "car", 0.0),                       # clear
        ponr.scan(_Svc([_slice(0, 2400)]), _route(), "car", 0.0),        # blocked + reopens
    ]
    for res in cases:
        for trip in (False, True):
            text = ponr.message(res, "ambulance", lang, trip=trip)
            assert text and "{" not in text and "None" not in text
            if lang != "en":
                assert any(ord(ch) > 0x900 for ch in text)               # really translated
    red = ponr.message(cases[2], "car", "en")
    assert red.startswith("Leave now") and "min" in red
    assert "by" in ponr.message(cases[1], "car", "en") and "min" in ponr.message(cases[1], "car", "en")


# ============================================================================ SMS: encoding, parsing, templates
def test_sms_segment_arithmetic():
    assert sms.sms_info("a" * 160) == {"encoding": "GSM-7", "units": 160, "segments": 1}
    assert sms.sms_info("a" * 161)["segments"] == 2
    assert sms.sms_info("a" * 306)["segments"] == 2 and sms.sms_info("a" * 307)["segments"] == 3
    assert sms.sms_info("{" * 80)["segments"] == 1 and sms.sms_info("{" * 81)["segments"] == 2      # extension chars cost 2
    hi = "क" * 70
    assert sms.sms_info(hi) == {"encoding": "UCS-2", "units": 70, "segments": 1}
    assert sms.sms_info("क" * 71)["segments"] == 2
    assert sms.sms_info("த" * 134)["segments"] == 2 and sms.sms_info("த" * 135)["segments"] == 3
    assert sms.sms_info("hello 😀")["encoding"] == "UCS-2" and sms.sms_info("hello 😀")["units"] == 8   # emoji = surrogate pair


@pytest.mark.parametrize("text,expected", [
    ("help", ("help", ())), ("  Help ", ("help", ())), ("?", ("help", ())),
    ("SAFE", ("safe", ())), ("safe", ("safe", ())), ("STOP", ("stop", ())), ("unsubscribe", ("stop", ())),
    ("LANG hi", ("lang", ("hi",))), ("lang TA", ("lang", ("ta",))), ("LANG FR", ("unknown", ())),
    ("ROUTE AbC123_-xyz", ("route", ("AbC123_-xyz",))),          # route codes are case-sensitive
    ("route abc", ("unknown", ())),                              # too short to be a code
    ("SHELTER 12.95,80.15", ("shelter", (12.95, 80.15))), ("shelter 12.95 80.15", ("shelter", (12.95, 80.15))),
    ("SHELTER -12.5, 80", ("shelter", (-12.5, 80.0))), ("SHELTER", ("shelter_help", ())),
    ("hello there", ("unknown", ())), ("", ("unknown", ())),
])
def test_parse_inbound(text, expected):
    assert sms.parse_inbound(text) == expected


def test_phone_normalisation():
    assert sms.normalise_phone("+91 98765-43210") == "+919876543210"
    assert sms.normalise_phone("(044) 2345 6789") == "04423456789"
    for bad in ("", "abc", "12345", "+1234567890123456", "98765 4321x"):
        with pytest.raises(ValueError):
            sms.normalise_phone(bad)


def test_sms_templates_complete_consistent_and_short_enough():
    sample = dict(vehicle="SUV / jeep", name="Anna Nagar Community Hall"[:30], km="12.3", min=45, minutes=120, hhmm="14:35",
                  leave="Leave by 14:35.", code="AQ3xYz9pQrStUvWx", hazard="strong wind / debris", age_min=45)
    for key, by_lang in sms.S.items():
        assert set(by_lang) == set(LANGS), key
        assert len({frozenset(_fields(by_lang[lg])) for lg in LANGS}) == 1, f"{key}: placeholders differ between languages"
        for lg in LANGS:
            body = sms.render(key, lg, **sample)
            assert "{" not in body
            assert sms.sms_info(body)["segments"] <= sms.MAX_SEGMENTS, f"{key}/{lg}: {sms.sms_info(body)}"
    assert sms.sms_info(sms.render("route_ok", "en", **sample))["segments"] <= 2
    for key in ("no_safe", "stale", "no_shelter", "trip_blocked"):          # every refusal tells people to call 112
        assert all("112" in sms.S[key][lg] for lg in LANGS), key


# ============================================================================ engine-level fixtures
@pytest.fixture(scope="module")
def svc():
    from safe_engine.service import EngineService
    s = EngineService(Settings())
    s.advance(45 * 60)
    return s


def _ll(s, fx, fy):
    return s.frame.to_lonlat(s.width * fx, s.height * fy)


@pytest.fixture(scope="module")
def planned(svc):
    """A plan with a chosen route; also keeps one infeasible route to test refusals."""
    for o, d, v in [((0.15, 0.3), (0.85, 0.7), "car"), ((0.2, 0.5), (0.8, 0.55), "two_wheeler"), ((0.6, 0.4), (0.9, 0.6), "truck")]:
        p = svc.plan(_ll(svc, *o), _ll(svc, *d), v)
        if p["chosen"]:
            return p, v
    pytest.skip("no safe route in this scenario")


def test_plan_carries_leave_by_for_the_chosen_route(svc, planned):
    p, vehicle = planned
    lb = p["leave_by"]
    assert lb and lb["route_id"] == p["chosen"] and lb["status"] in ("clear", "countdown")
    assert lb["message"] and lb["assessments"] >= 3
    again = svc.route_leave_by(p["chosen"], "hi")
    assert again["status"] == lb["status"] and again["seconds_left"] == lb["seconds_left"]
    assert any(ord(ch) > 0x900 for ch in again["message"])
    assert svc.route_leave_by("R-does-not-exist") is None


def test_leave_by_deadline_is_consistent_with_m3(svc, planned):
    """Leaving exactly at the deadline is feasible; leaving a step later than the first cut-off is not."""
    p, vehicle = planned
    lb = p["leave_by"]
    if lb["status"] != "countdown":
        pytest.skip("this scenario has no cut-off for the chosen route")
    c = svc.routes[p["chosen"]]
    veh = get_profile(vehicle)
    start = max(c["depart_t"], svc.now())
    assert svc.evaluator.assess(c["route"], start + lb["seconds_left"], veh, svc.store.index).feasible
    assert not svc.evaluator.assess(c["route"], start + lb["seconds_left"] + 2 * ponr.RESOLUTION_S, veh, svc.store.index).feasible


def test_trip_delay_budget_lifecycle(svc, planned):
    p, vehicle = planned
    t = svc.start_trip(p["chosen"])
    lb = svc.trip_leave_by(t["id"], "en")
    assert lb["trip_id"] == t["id"] and lb["status"] in ("clear", "countdown", "blocked") and lb["message"]
    svc.cancel_trip(t["id"])
    done = svc.trip_leave_by(t["id"])
    assert done["status"] == "inactive" and done["trip_status"] == "cancelled"
    with pytest.raises(KeyError):
        svc.trip_leave_by("T-nope")


# ============================================================================ SMS gateway (verified guidance)
def test_route_sms_is_verified_and_carries_a_working_code(svc, planned):
    p, vehicle = planned
    for lang in LANGS:
        out = svc.sms.send("+919876543210", lang, "route", route_id=p["chosen"], vehicle=vehicle)
        m = out["message"]
        assert out["sent"] and m["verified"] and m["direction"] == "out" and m["segments"] <= sms.MAX_SEGMENTS
        code = next(r for r in p["routes"] if r["id"] == p["chosen"])["route_code"]
        assert code in m["body"]
        assert out["delivery"] == {"provider": "simulated", "delivered": False, "simulated": True}
    assert svc.sms.send("+919876543210", "hi", "route", route_id=p["chosen"], vehicle=vehicle)["message"]["encoding"] == "UCS-2"
    assert svc.sms.send("+919876543210", "en", "route", route_id=p["chosen"], vehicle=vehicle)["message"]["encoding"] == "GSM-7"


def test_sms_refuses_to_confirm_an_infeasible_route(svc):
    bad = None
    for o, d, v in [((0.6, 0.4), (0.9, 0.6), "two_wheeler"), ((0.2, 0.5), (0.8, 0.55), "two_wheeler"), ((0.15, 0.3), (0.85, 0.7), "two_wheeler")]:
        bad = next((r for r in svc.plan(_ll(svc, *o), _ll(svc, *d), v)["routes"] if not r["feasible"]), None)
        if bad:
            break
    if bad is None:
        pytest.skip("every candidate was feasible in this scenario")
    for lang in LANGS:
        m = svc.sms.send("+919000000001", lang, "route", route_id=bad["id"], vehicle="two_wheeler")["message"]
        assert m["verified"] is False and m["reason"] == "infeasible"
        assert "112" in m["body"] and bad["route_code"] not in m["body"]
        assert "SAFE ROUTE" not in m["body"]


def test_sms_refuses_stale_hazard_data_and_old_route_objects(svc, planned):
    p, vehicle = planned
    rid = p["chosen"]
    old = svc.store.updated_at
    svc.store.updated_at = svc.now() - svc.settings.hazard_ttl_s - 300
    try:
        m = svc.sms.send("+919000000002", "en", "route", route_id=rid, vehicle=vehicle)["message"]
        assert m["verified"] is False and m["reason"] == "stale" and "min old" in m["body"]
        assert svc.sms.send("+919000000002", "en", "shelter", lon=_ll(svc, .5, .5)[0], lat=_ll(svc, .5, .5)[1])["message"]["reason"] == "stale"
    finally:
        svc.store.updated_at = old
    created = svc.routes[rid]["created"]
    svc.routes[rid]["created"] = svc.now() - svc.settings.object_ttl_s - 60
    try:
        m = svc.sms.send("+919000000002", "en", "route", route_id=rid, vehicle=vehicle)["message"]
        assert m["verified"] is False and m["reason"] == "route_too_old"
    finally:
        svc.routes[rid]["created"] = created
    m = svc.sms.send("+919000000002", "en", "route", route_id="R999999", vehicle="car")["message"]
    assert m["verified"] is False and m["reason"] == "unknown_route"


def test_trip_and_shelter_sms(svc, planned):
    p, vehicle = planned
    t = svc.start_trip(p["chosen"])
    m = svc.sms.send("+919000000003", "ta", "trip", trip_id=t["id"])["message"]
    assert m["verified"] and m["trip_id"] == t["id"] and m["body"]
    svc.cancel_trip(t["id"])
    assert svc.sms.send("+919000000003", "en", "trip", trip_id=t["id"])["message"]["body"] == sms.render("trip_done", "en")
    with pytest.raises(KeyError):
        svc.sms.send("+919000000003", "en", "trip", trip_id="T-nope")
    lon, lat = _ll(svc, 0.4, 0.4)
    sh = svc.sms.send("+919000000003", "en", "shelter", lon=lon, lat=lat, vehicle="truck")["message"]
    assert sh["reason"] in ("ok", "no_safe_shelter")
    if sh["reason"] == "ok":
        assert sh["verified"] and sh["shelter_id"] and sh["route_id"] and "Route code" in sh["body"]
        # the shelter must not lie inside a predicted hazard when we get there
        s_ = next(x for x in svc.shelters if x["id"] == sh["shelter_id"])
        lo, la = svc.frame.to_lonlat(s_["x"], s_["y"])
        arrival = svc.now() + svc.route_info(sh["route_id"])["duration_s"]
        assert not svc.point_hazarded(lo, la, arrival - 300, arrival + 3600, "truck")
    assert svc.sms.send("+919000000003", "en", "shelter", lon=77.2, lat=28.6)["message"]["reason"] == "out_of_area"
    for kw in ({"kind": "route"}, {"kind": "trip"}, {"kind": "shelter"}, {"kind": "voice"}):
        with pytest.raises(ValueError):
            svc.sms.send("+919000000003", "en", **kw)
    with pytest.raises(ValueError):
        svc.sms.send("not a number", "en", "route", route_id="R1")


def test_inbound_commands(svc, planned):
    p, vehicle = planned
    g, ph = svc.sms, "+919000000004"
    r = g.receive(ph, "HELP")
    assert r["in"]["direction"] == "in" and r["reply"]["body"] == sms.render("help", "en")
    assert g.receive(ph, "SAFE")["reply"]["body"] == sms.render("ack_safe", "en") and ph in g.safe_checkins
    hi = g.receive(ph, "LANG HI")["reply"]["body"]
    assert hi == sms.render("lang_set", "hi")
    assert g.receive(ph, "HELP")["reply"]["body"] == sms.render("help", "hi")              # language sticks per number
    g.receive(ph, "LANG EN")
    assert g.receive(ph, "what is this")["reply"]["body"] == sms.render("unknown", "en")
    assert g.receive(ph, "SHELTER")["reply"]["body"] == sms.render("need_coords", "en")
    assert g.receive(ph, "SHELTER 28.6,77.2")["reply"]["reason"] == "out_of_area"
    assert g.receive(ph, "ROUTE AAAAAAAAAAAA")["reply"]["reason"] == "bad_code"            # checksum fails
    code = next(r for r in p["routes"] if r["id"] == p["chosen"])["route_code"]
    rc = g.receive(ph, f"ROUTE {code}")["reply"]
    assert rc["command"] == "route" and rc["body"] and rc["reason"] in ("ok", "infeasible")
    if rc["reason"] == "ok":
        assert rc["verified"] and "SAFE ROUTE" in rc["body"]
    assert [m["direction"] for m in g.outbox(ph)][:2] == ["in", "out"]


def test_stop_suppresses_further_messages_and_is_per_number(svc, planned):
    p, vehicle = planned
    g = svc.sms
    stop = g.receive("+919000000005", "STOP")["reply"]
    assert stop["body"] == sms.render("stop", "en")
    out = g.send("+919000000005", "en", "route", route_id=p["chosen"], vehicle=vehicle)
    assert out == {"sent": False, "suppressed": True, "reason": "recipient sent STOP"}
    assert g.receive("+919000000005", "HELP")["reply"] is None
    assert g.send("+919000000006", "en", "route", route_id=p["chosen"], vehicle=vehicle)["sent"]


def test_reset_clears_the_sms_log(svc):
    svc.sms.receive("+919000000007", "HELP")
    assert svc.sms.outbox("+919000000007")
    seed = svc.seed
    svc.reset(seed)
    assert svc.sms.outbox() == [] and svc.sms.stopped == set()
    svc.advance(45 * 60)


# ============================================================================ heatmap
def test_heatmap_shape_range_and_geography(svc):
    h = svc.heatmap(30, None, "car")
    a = np.array(h["values"])
    assert a.shape == (h["ny"], h["nx"]) and h["nx"] <= sms.MAX_SEGMENTS * 0 + 90 and h["ny"] <= 90
    assert a.min() >= 0.0 and a.max() <= 1.0 and h["max"] == pytest.approx(a.max())
    lon0, lat0, lon1, lat1 = h["bbox"]
    assert lon0 < lon1 and lat0 < lat1
    bb = svc.status()["region"]["bbox"]
    assert lon0 == pytest.approx(bb[0]) and lat0 == pytest.approx(bb[1])
    assert lon1 >= bb[2] - 1e-6 and lat1 >= bb[3] - 1e-6                 # grid covers the whole region
    assert h["offset_min"] == 30 and h["synthetic"] is True


def test_heatmap_cells_are_explained_by_real_slices(svc):
    t = svc.now() + 30 * 60
    h = svc.heatmap(30, None, None)
    a = np.array(h["values"])
    assert a.max() > 0.3, "the simulated storm should produce risk by now"
    size = h["cell_m"]
    live = [s for s in svc.store.slices.values() if s.status == "approved" and s.overlaps(t, t + 1)]
    rng = np.random.default_rng(1)
    for r, c in zip(*np.nonzero(a > 0)):
        if rng.random() > 0.05:
            continue
        pt = Point((c + 0.5) * size, (r + 0.5) * size)
        cover = [s.probability for s in live if s.geom.contains(pt)]
        assert cover and max(cover) == pytest.approx(a[r, c], abs=1e-3)
    zeros = np.argwhere(a == 0)
    for r, c in zeros[rng.choice(len(zeros), size=min(40, len(zeros)), replace=False)]:
        pt = Point((c + 0.5) * size, (r + 0.5) * size)
        assert not any(s.geom.contains(pt) for s in live)


def test_heatmap_moves_with_the_slider(svc):
    base = np.array(svc.heatmap(0)["values"])
    assert any(not np.array_equal(base, np.array(svc.heatmap(m)["values"])) for m in (15, 30, 60, 90, 120))


def test_heatmap_is_vehicle_and_class_specific(svc):
    def grid(cls, veh):
        return np.array(svc.heatmap(30, cls, veh)["values"])
    # flood limits (m): truck .60 > bus .50 > suv .40 > ambulance .30 > car .25 > two-wheeler .15  -> tougher vehicle, less flood risk
    chain = [grid("flood", v) for v in ("truck", "bus", "suv", "ambulance", "car", "two_wheeler")] + [grid("flood", None)]
    for tougher, weaker in zip(chain, chain[1:]):
        assert (tougher <= weaker + 1e-9).all()
    assert chain[0].sum() < chain[-2].sum()
    # wind limits (km/h): suv 90 > car = ambulance 80 > bus = truck 70 > two-wheeler 50 (a truck is MORE exposed to wind than a car)
    wind = [grid("storm", v) for v in ("suv", "car", "bus", "two_wheeler")] + [grid("storm", None)]
    for tougher, weaker in zip(wind, wind[1:]):
        assert (tougher <= weaker + 1e-9).all()
    assert np.array_equal(grid("storm", "car"), grid("storm", "ambulance")) and np.array_equal(grid("storm", "bus"), grid("storm", "truck"))
    # classes partition the all-hazards picture
    anyveh = grid(None, None)
    parts = [grid(c, None) for c in ("flood", "landslide", "storm", "closure")]
    assert np.array_equal(np.maximum.reduce(parts), anyveh)
    assert all((p <= anyveh + 1e-9).all() for p in parts)
    # landslides and closures are impassable for every vehicle, so the vehicle filter never removes them
    assert np.array_equal(grid("landslide", "truck"), grid("landslide", "two_wheeler"))


def test_heatmap_includes_admin_closures_and_ignores_pending_reports(svc):
    lon, lat = _ll(svc, 0.12, 0.88)
    before = np.array(svc.heatmap(0, "closure")["values"])
    assert before.max() == 0.0
    r = svc.report(lon, lat, "closure", "unknown", "tester", "pending only")
    assert np.array(svc.heatmap(0, "closure")["values"]).max() == 0.0     # pending: the router ignores it, so does the map
    svc.admin_add_closure({"type": "Point", "coordinates": [lon, lat]}, "closure", 3, "tester")
    after = np.array(svc.heatmap(0, "closure")["values"])
    assert after.max() == pytest.approx(1.0) and (after > 0).sum() >= 1
    # the cell containing the closure lights up
    h = svc.heatmap(0, "closure")
    x, y = svc.frame.to_xy(lon, lat)
    assert after[int(y // h["cell_m"]), int(x // h["cell_m"])] == pytest.approx(1.0)


def test_what_if_departure_matches_m3_for_that_time(svc):
    o, d = _ll(svc, 0.15, 0.3), _ll(svc, 0.85, 0.7)
    for minutes in (0, 30, 60):
        depart = svc.now() + minutes * 60
        p = svc.plan(o, d, "car", depart_t=depart)
        assert p["depart_t"] == depart
        for r in p["routes"]:
            rt = svc.routes[r["id"]]["route"]
            a = svc.evaluator.assess(rt, depart, get_profile("car"), svc.store.index, stale=p["stale"])
            assert a.feasible == r["feasible"]
        if p["leave_by"]:
            assert p["leave_by"]["start_t"] == depart


# ============================================================================ HTTP API
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from safe_engine import api
    c = TestClient(api.app)
    c.post("/scenario/reset?seed=7")
    c.post("/scenario/advance?minutes=45")
    return c


def _api_plan(client, vehicle="car"):
    bb = client.get("/status").json()["region"]["bbox"]
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    o = [bb[0] + 0.15 * w, bb[1] + 0.3 * h]
    d = [bb[0] + 0.85 * w, bb[1] + 0.7 * h]
    return client.post("/plan", json={"origin": o, "destination": d, "vehicle": vehicle}).json()


def test_api_heatmap(client):
    r = client.get("/hazards/heatmap?offset_min=30&vehicle=car")
    assert r.status_code == 200
    j = r.json()
    assert len(j["values"]) == j["ny"] and len(j["values"][0]) == j["nx"] and j["vehicle"] == "car"
    assert client.get("/hazards/heatmap?hazard_class=flood").json()["class"] == "flood"
    assert client.get("/hazards/heatmap?offset_min=121").status_code == 422
    assert client.get("/hazards/heatmap?offset_min=-1").status_code == 422
    assert client.get("/hazards/heatmap?hazard_class=lava").status_code == 422
    assert client.get("/hazards/heatmap?vehicle=hovercraft").status_code == 400


def test_api_leave_by_and_trip_budget(client):
    p = _api_plan(client)
    assert "leave_by" in p
    if not p["chosen"]:
        pytest.skip("no safe route in this scenario")
    rid = p["chosen"]
    lb = client.get(f"/routes/{rid}/leave-by?lang=ta").json()
    assert lb["route_id"] == rid and lb["status"] in ("clear", "countdown") and lb["message"]
    assert client.get(f"/routes/{rid}/leave-by?lang=fr").status_code == 422
    assert client.get("/routes/R-nope/leave-by").status_code == 404
    t = client.post("/trips", json={"route_id": rid}).json()
    tb = client.get(f"/trips/{t['id']}/leave-by").json()
    assert tb["trip_id"] == t["id"] and tb["status"] in ("clear", "countdown", "blocked")
    client.delete(f"/trips/{t['id']}")
    assert client.get(f"/trips/{t['id']}/leave-by").json()["status"] == "inactive"
    assert client.get("/trips/T-nope/leave-by").status_code == 404


def test_api_sms_roundtrip_and_validation(client):
    p = _api_plan(client)
    if not p["chosen"]:
        pytest.skip("no safe route in this scenario")
    rid = p["chosen"]
    ok = client.post("/sms/send", json={"to": "+91 98765 43210", "lang": "hi", "kind": "route", "route_id": rid})
    assert ok.status_code == 200 and ok.json()["message"]["phone"] == "+919876543210" and ok.json()["delivery"]["simulated"]
    box = client.get("/sms/outbox", params={"phone": "+919876543210"}).json()
    assert box["provider"] == "simulated" and box["messages"][-1]["body"] == ok.json()["message"]["body"]
    rep = client.post("/sms/inbound", json={"from": "+919876543210", "text": "HELP"}).json()
    assert rep["in"]["direction"] == "in" and rep["reply"]["command"] == "help"
    assert client.post("/sms/send", json={"to": "+919876543210", "kind": "route"}).status_code == 400          # missing route_id
    assert client.post("/sms/send", json={"to": "12", "kind": "route", "route_id": rid}).status_code == 400    # bad number
    assert client.post("/sms/send", json={"to": "+919876543210", "kind": "carrier-pigeon"}).status_code == 422
    assert client.post("/sms/send", json={"to": "+919876543210", "kind": "trip", "trip_id": "T-nope"}).status_code == 404
    assert client.post("/sms/send", json={"to": "+919876543210", "kind": "route", "route_id": rid, "lang": "xx"}).status_code == 422
    assert client.post("/sms/inbound", json={"from": "bad", "text": "HELP"}).status_code == 400
    assert client.post("/sms/inbound", json={"from": "+919876543210", "text": "x" * 400}).status_code == 422
    assert client.get("/sms/outbox", params={"phone": "bad"}).status_code == 400


def test_console_assets_expose_the_new_controls(client):
    html = client.get("/").text
    for needle in ('id="depart"', 'id="heatOn"', 'id="tab-sms"', 'id="advBtn"', 'id="phoneMsgs"'):
        assert needle in html
    js = client.get("/console.js").text
    for needle in ("/hazards/heatmap", "/leave-by", "/sms/send", "/sms/inbound", "/scenario/advance"):
        assert needle in js
    css = client.get("/console.css").text
    assert ".leaveby" in css and ".bubble" in css


# ============================================================================ regression: Trip.remaining() must keep bridge flags
def test_remaining_route_keeps_bridge_flags_so_ponding_is_still_exempt():
    """Found while building M8: Trip.remaining() dropped Route.elevated, so a trip on a bridge was re-assessed as if it
    drove through the water below it (shown as 'blocked' straight after a feasible plan)."""
    from safe_engine.trips import Trip
    elev = [10 <= i <= 20 for i in range(30)]
    r = _route()
    r.elevated = elev
    flood = _slice(0, 9000, x0=1300, x1=1700, inten=0.9)
    svc = _Svc([flood])
    assert ponr.scan(svc, r, "car", 0.0)["status"] == "clear"                       # the planned route is fine (bridge)
    trip = Trip(route=r, depart_t=0.0, vehicle="car", origin=r.coords[0], dest=r.coords[-1])
    for t in (0.0, 40.0, 120.0, 200.0, 290.0):
        rem, _ = trip.remaining(t)
        assert rem.elevated is not None and len(rem.elevated) == len(rem.seg_dur) == len(rem.coords) - 1
        assert svc.evaluator.assess(rem, t, get_profile("car"), svc.store.index).feasible, t
        assert ponr.scan(svc, rem, "car", t)["status"] != "blocked", t
    # without the bridge the same flood does block the remaining route
    plain = Trip(route=_route(), depart_t=0.0, vehicle="car", origin=(0, 0), dest=(3000, 0))
    assert not svc.evaluator.assess(plain.remaining(0.0)[0], 0.0, get_profile("car"), svc.store.index).feasible


def test_a_trip_started_on_a_recommended_route_is_not_reported_blocked(svc, planned):
    _, vehicle = planned
    # plan afresh: earlier tests may have advanced the clock and expired the shared fixture's routes
    p = svc.plan(_ll(svc, 0.15, 0.3), _ll(svc, 0.85, 0.7), vehicle)
    if not p["chosen"]:
        pytest.skip("no safe route right now")
    t = svc.start_trip(p["chosen"])
    lb = svc.trip_leave_by(t["id"])
    assert lb["status"] != "blocked", lb["message"]
    svc.cancel_trip(t["id"])


def test_shelter_name_shortening():
    assert sms._short_name("Relief shelter 8 (College camp, Block B)") == "Relief shelter 8"
    assert sms._short_name("Anna Nagar Hall") == "Anna Nagar Hall"
    assert len(sms._short_name("A" * 80)) == 30
