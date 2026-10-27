"""M6 — deterministic verification gate for language-model output.

The model's answer is a list of *claims*, each citing ledger object ids. The
gate releases the answer only if every claim verifies against the CURRENT
engine state:

  (i)   every cited id exists in the ledger (was returned by a tool) and in the
        live system (route cache / hazard store / shelter list / SOP corpus);
  (ii)  a recommended route is re-assessed now and must still be feasible; a
        recommended shelter's route must be feasible and the shelter must not
        lie in an active hazard slice at the predicted arrival time;
        a "no safe route" claim must be true (all cited routes infeasible);
  (iii) cited objects are younger than OBJECT_TTL and the hazard data younger
        than HAZARD_TTL;
  (iv)  safety-critical text is rendered from templates with verified values;
        free text is confined to a clearly labelled explanation from which
        imperative route advice is stripped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..sop import SOP
from . import templates as TPL

IST = timezone(timedelta(hours=5, minutes=30))
CRITICAL_PATTERNS = re.compile(
    r"\b(take|go via|use the|drive|ride|cross|safe to|is safe|head to|turn (left|right)|"
    r"evacuate|avoid|stay|move|call|follow|contact|seek|do not|don't|never)\b", re.I)
KNOWN = {"route_recommendation", "shelter_recommendation", "hazard_warning", "instruction", "no_safe_route",
         "hazard_report", "general_information", "needs_location", "place_distance", "place_lookup"}


@dataclass
class GateResult:
    ok: bool
    blocks: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    checks: int = 0
    explanation: str | None = None


def _fmt_t(t: float) -> str:
    return datetime.fromtimestamp(t, IST).strftime("%H:%M")


def verify(answer: dict, ledger, svc, now: float, lang: str, vehicle: str, location=None) -> GateResult:
    res = GateResult(ok=True)
    claims = answer.get("claims") if isinstance(answer, dict) else None
    if not isinstance(claims, list) or not claims:
        res.ok = False
        res.failures.append("answer has no structured claims")
        return res
    s = svc.settings
    hazard_age = now - svc.store.updated_at
    stale = hazard_age > s.hazard_ttl_s

    def obj(oid, kind):
        o = ledger.get(oid)
        res.checks += 1
        if o is None or o["kind"] != kind:
            res.failures.append(f"{oid}: not returned by any tool in this session")
            return None
        if now - o["ts"] > s.object_ttl_s:
            res.failures.append(f"{oid}: older than {int(s.object_ttl_s // 60)} min")
            return None
        return o

    for c in claims:
        ctype = c.get("type")
        ids = [str(i) for i in c.get("ids", [])]
        if ctype not in KNOWN:
            res.failures.append(f"unknown claim type {ctype!r}")
            continue
        if ctype == "general_information":
            explanation = answer.get("explanation")
            if ids or not isinstance(explanation, str) or not explanation.strip():
                res.failures.append("general_information must contain an explanation and no ids")
                continue
            kept = [x for x in re.split(r"(?<=[.!?।])\s+", explanation.strip())
                    if x and not CRITICAL_PATTERNS.search(x)]
            text = " ".join(kept)[:600]
            if not text:
                res.failures.append("general_information contained no safe educational text")
                continue
            res.blocks.append({"type": ctype, "ids": [], "verified": False, "text": text})
            continue
        if ctype == "needs_location":
            res.blocks.append({"type": ctype, "ids": [], "verified": True,
                               "text": "Select your location on the map for local hazard or shelter questions. "
                                       "For route questions, select both a start and destination."})
            continue
        if ctype == "place_distance":
            pids = [i for i in ids if i.startswith("P")]
            if len(pids) != 1:
                res.failures.append("place_distance must cite exactly one place lookup id")
                continue
            o = obj(pids[0], "place_distance")
            if not o:
                continue
            data = o["data"]
            if data.get("error"):
                res.blocks.append({"type": ctype, "ids": pids, "verified": False,
                                   "text": data["error"]})
                continue
            a, b = data["origin"], data["destination"]
            text = (f'{a["display_name"]} to {b["display_name"]}: about '
                    f'{data["straight_distance_km"]:,} km straight-line.')
            if data.get("road_distance_km") is not None:
                mins = data.get("road_duration_min")
                duration = (f' (about {mins // 60} h {mins % 60} min)' if mins is not None else "")
                text += (f' About {data["road_distance_km"]:,} km by road{duration}; '
                         "approximate OSRM routing, not live traffic.")
            elif data.get("road_unavailable"):
                text += " Driving distance is unavailable from the road-routing service."
            text += " Place data: OpenStreetMap contributors."
            res.blocks.append({"type": ctype, "ids": pids, "verified": True, "text": text})
            continue
        if ctype == "place_lookup":
            pids = [i for i in ids if i.startswith("P")]
            if len(pids) != 1:
                res.failures.append("place_lookup must cite exactly one place lookup id")
                continue
            o = obj(pids[0], "place_lookup")
            if not o:
                continue
            place = o["data"]
            if place.get("error"):
                res.blocks.append({"type": ctype, "ids": pids, "verified": False,
                                   "text": place["error"]})
                continue
            lat, lon = place["lat"], place["lon"]
            text = (f'OpenStreetMap locates {place["display_name"]}. Approximate mapped point: '
                    f'{abs(lat):.4f}° {"N" if lat >= 0 else "S"}, '
                    f'{abs(lon):.4f}° {"E" if lon >= 0 else "W"}.')
            res.blocks.append({"type": ctype, "ids": pids, "verified": True, "text": text})
            continue
        if ctype in ("route_recommendation", "shelter_recommendation") and stale:
            res.failures.append(f"hazard data is {int(hazard_age // 60)} min old (> TTL)")
            continue

        if ctype == "route_recommendation":
            rids = [i for i in ids if i.startswith("R")]
            if len(rids) != 1:
                res.failures.append("route_recommendation must cite exactly one route id")
                continue
            o = obj(rids[0], "route")
            if not o:
                continue
            a = svc.reassess(rids[0], now)
            res.checks += 1
            if a is None or not a["feasible"]:
                res.failures.append(f"{rids[0]}: not feasible under current hazard state")
                continue
            r = svc.route_info(rids[0])
            res.blocks.append({"type": ctype, "ids": rids, "verified": True,
                               "text": TPL.render("route_ok", lang, route_id=rids[0], distance_km=f"{r['distance_m'] / 1000:.1f}",
                                                  duration_min=round(r["duration_s"] / 60), p_safe=round(a["p_safe"] * 100),
                                                  margin_text=_margin(a, lang))})

        elif ctype == "shelter_recommendation":
            sid = next((i for i in ids if i.startswith("S")), None)
            rid = next((i for i in ids if i.startswith("R")), None)
            so, ro = (obj(sid, "shelter") if sid else None), (obj(rid, "route") if rid else None)
            if not so or not ro:
                if not sid or not rid:
                    res.failures.append("shelter_recommendation must cite a shelter id and a route id")
                continue
            if ro["data"].get("shelter_id") != sid:
                res.failures.append(f"{rid} does not lead to {sid}")
                continue
            a = svc.reassess(rid, now)
            res.checks += 1
            if a is None or not a["feasible"]:
                res.failures.append(f"{rid}: route to shelter not feasible now")
                continue
            arrival = now + svc.route_info(rid)["duration_s"]
            res.checks += 1
            if svc.point_hazarded(so["data"]["lon"], so["data"]["lat"], arrival - 300, arrival + 3600, vehicle):
                res.failures.append(f"{sid}: shelter inside a predicted hazard at arrival")
                continue
            r = svc.route_info(rid)
            res.blocks.append({"type": ctype, "ids": [sid, rid], "verified": True,
                               "text": TPL.render("shelter", lang, name=so["data"]["name"], shelter_id=sid,
                                                  distance_km=f"{r['distance_m'] / 1000:.1f}", duration_min=round(r["duration_s"] / 60), route_id=rid)})

        elif ctype == "hazard_warning":
            for hid in ids:
                o = obj(hid, "hazard")
                if not o:
                    continue
                h = svc.store.get(hid)
                res.checks += 1
                if h is None or h.status != "approved" or h.t_end < now:
                    res.failures.append(f"{hid}: hazard no longer active")
                    continue
                d = o["data"].get("distance_m") or 0
                key = "here" if d < 150 else "near"
                where = TPL.WHERE[key].get(lang, TPL.WHERE[key]["en"]).format(d=f"{d / 1000:.1f}")
                if h.hclass == "flood":
                    m = re.search(r"depth > ([0-9.]+)", h.note or "")
                    it = f", > {int(round(float(m.group(1)) * 100))} cm" if m else ""
                elif h.hclass == "storm":
                    it = f", ~{int(h.intensity)} km/h"
                else:
                    it = ""
                res.blocks.append({"type": ctype, "ids": [hid], "verified": True,
                                   "text": TPL.render("hazard", lang, hazard_id=hid, hazard=TPL.name(TPL.HAZARD_NAMES, h.hclass, lang),
                                                      where=where, start=_fmt_t(max(now, h.t_start)), end=_fmt_t(h.t_end),
                                                      p=round(h.probability * 100), intensity_text=it)})

        elif ctype == "instruction":
            for sid in ids:
                if sid not in SOP:
                    res.failures.append(f"{sid}: not an SOP clause")
                    continue
                if not obj(sid, "sop"):
                    continue
                res.blocks.append({"type": ctype, "ids": [sid], "verified": True, "text": SOP[sid].get(lang) or SOP[sid]["en"]})

        elif ctype == "no_safe_route":
            rids = [i for i in ids if i.startswith("R")]
            if not rids:
                res.failures.append("no_safe_route must cite the infeasible route ids")
                continue
            okc = True
            for rid in rids:
                if not obj(rid, "route"):
                    okc = False
                    break
                a = svc.reassess(rid, now)
                res.checks += 1
                if a is not None and a["feasible"]:
                    res.failures.append(f"{rid}: is feasible, 'no safe route' would be wrong")
                    okc = False
                    break
            if okc:
                res.blocks.append({"type": ctype, "ids": rids, "verified": True,
                                   "text": TPL.render("no_route", lang, vehicle=TPL.name(TPL.VEHICLE_NAMES, vehicle, lang))})

        elif ctype == "hazard_report":
            if location is None:
                res.failures.append("hazard_report needs the user's location")
                continue
            ev = svc.report(location[0], location[1], c.get("hazard", "flood"), c.get("depth", "unknown"),
                            c.get("reporter", "assistant-user"), c.get("text", ""), via="llm")
            res.blocks.append({"type": ctype, "ids": [ev["evidence_id"]], "verified": True,
                               "text": TPL.render("report_ack", lang, evidence_id=ev["evidence_id"])})

    expl = answer.get("explanation") if isinstance(answer, dict) else None
    if isinstance(expl, str) and expl.strip():
        kept = [x for x in re.split(r"(?<=[.!?।])\s+", expl.strip()) if x and not CRITICAL_PATTERNS.search(x)]
        if not any(c.get("type") == "general_information" for c in claims if isinstance(c, dict)):
            res.explanation = " ".join(kept)[:600] or None
    if stale and not res.blocks:
        res.failures.append("stale hazard data")
    res.ok = not res.failures and bool(res.blocks)
    return res


def _margin(a: dict, lang: str) -> str:
    if a.get("margin_s") is None:
        return TPL.render("margin_none", lang)
    return TPL.render("margin", lang, margin_min=max(0, round(a["margin_s"] / 60)))
