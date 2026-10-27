"""M8 — point-of-no-return ("leave-by") warning.

M3 answers "is this route safe if I leave at time T?". The point-of-no-return
asks the follow-up a driver actually needs: *how long can I wait?*

For a route (or the remaining part of an active trip) the evaluator is run for a
sequence of delayed start times. The first delay at which the route stops being
feasible gives the deadline: the last start (or, for a trip, the longest total
delay) that still keeps the route safe for the vehicle. The boundary is refined
by bisection to ``RESOLUTION_S``.

Three outcomes:

* ``clear``      – feasible for every delay inside the forecast window;
* ``countdown``  – feasible now, cut off after ``seconds_left``;
* ``blocked``    – already infeasible now (``reopens_in_s`` says when, if ever,
                   it becomes feasible again inside the window).

Feasibility is not monotone in the start time (a flood can recede), so the whole
window is scanned and the *first* cut-off is reported; a later reopening is
returned separately and never hides the cut-off.

Only forecast data the engine already holds is used. Delays whose arrival would
fall beyond the hazard forecast horizon are not evaluated and the window is
reported as ``forecast_limited``. Statements are rendered from the pre-translated
templates, never from free text.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .assistant import templates as TPL
from .vehicles import get_profile

IST = timezone(timedelta(hours=5, minutes=30))
STEP_S = 300          # coarse scan step
RESOLUTION_S = 60     # bisection resolution of the reported deadline
MAX_DELAY_S = 7200    # never look further ahead than the model horizon (120 min)
URGENT_S = 600        # <= 10 min left: red
WARN_S = 1800         # <= 30 min left: amber


def hhmm(t: float) -> str:
    return datetime.fromtimestamp(t, IST).strftime("%H:%M")


def scan(svc, route, vehicle: str, start_t: float, *, step_s: int = STEP_S, resolution_s: int = RESOLUTION_S,
         max_delay_s: int = MAX_DELAY_S) -> dict:
    """Find the latest safe delay for ``route`` starting at ``start_t`` (engine clock seconds)."""
    s = svc.settings
    veh = get_profile(vehicle)
    now = svc.now()
    stale = (now - svc.store.updated_at) > s.hazard_ttl_s
    arrive_span = route.duration * (1 + s.eta_rel_uncertainty) + s.eta_abs_uncertainty_s
    horizon_end = now + max(svc.horizons)
    limit = int(max(0.0, min(float(max_delay_s), horizon_end - start_t - arrive_span)))
    forecast_limited = limit < max_delay_s

    cache: dict[int, object] = {}

    def assess(d: int):
        if d not in cache:
            cache[d] = svc.evaluator.assess(route, start_t + d, veh, svc.store.index, stale=stale)
        return cache[d]

    grid = list(range(0, limit + 1, step_s)) or [0]
    if grid[-1] < limit:
        grid.append(limit)

    out = {"status": "clear", "seconds_left": None, "leave_by_t": None, "window_s": limit, "reason": "",
           "hazard_classes": [], "reopens_in_s": None, "urgency": "green", "forecast_limited": forecast_limited,
           "stale": stale, "start_t": start_t, "step_s": step_s, "resolution_s": resolution_s}

    first = assess(0)
    if not first.feasible:
        out.update(status="blocked", urgency="red", reason=first.reason,
                   hazard_classes=sorted({b["class"] for b in first.blocked}) or ["risk"])
        lo = 0
        for d in grid[1:]:
            if assess(d).feasible:
                hi = d
                while hi - lo > resolution_s:
                    mid = (lo + hi) // 2
                    if assess(mid).feasible:
                        hi = mid
                    else:
                        lo = mid
                out["reopens_in_s"] = hi
                break
            lo = d
    else:
        bad = next((d for d in grid[1:] if not assess(d).feasible), None)
        if bad is not None:
            lo, hi = grid[grid.index(bad) - 1], bad
            while hi - lo > resolution_s:
                mid = (lo + hi) // 2
                if assess(mid).feasible:
                    lo = mid
                else:
                    hi = mid
            cut = assess(hi)
            left = int(lo)
            out.update(status="countdown", seconds_left=left, leave_by_t=start_t + left, reason=cut.reason,
                       hazard_classes=sorted({b["class"] for b in cut.blocked}) or ["risk"],
                       urgency="red" if left <= URGENT_S else "amber" if left <= WARN_S else "green")
            for d in grid:
                if d > hi and assess(d).feasible:
                    out["reopens_in_s"] = d
                    break
    out["assessments"] = len(cache)
    return out


def _hazard_text(classes: list, lang: str) -> str:
    return " / ".join(TPL.name(TPL.HAZARD_NAMES, c, lang) for c in (classes or ["risk"]))


def message(res: dict, vehicle: str, lang: str = "en", *, trip: bool = False) -> str:
    """Safety statement rendered from templates only."""
    lang = lang if lang in ("en", "hi", "ta") else "en"
    veh = TPL.name(TPL.VEHICLE_NAMES, vehicle, lang)
    st = res["status"]
    if st == "blocked":
        text = TPL.render("ponr_blocked", lang, vehicle=veh)
        if res.get("reopens_in_s"):
            text += " " + TPL.render("ponr_reopens", lang, minutes=max(1, round(res["reopens_in_s"] / 60)))
        return text
    if st == "clear":
        return TPL.render("ponr_clear", lang, minutes=max(1, round(res["window_s"] / 60)))
    minutes = max(1, round(res["seconds_left"] / 60))
    hazard = _hazard_text(res["hazard_classes"], lang)
    if trip:
        key = "ponr_trip_urgent" if res["urgency"] == "red" else "ponr_trip"
        return TPL.render(key, lang, minutes=minutes, hazard=hazard)
    if res["urgency"] == "red":
        return TPL.render("ponr_now", lang, minutes=minutes, hazard=hazard)
    return TPL.render("ponr_countdown", lang, minutes=minutes, leave_by=hhmm(res["leave_by_t"]), vehicle=veh, hazard=hazard)
