"""SMS / WhatsApp fallback: the same verified guidance as the app, as short text.

When the data connection is down a person can still receive, and ask for, safety
guidance by SMS. This module does three things:

* composes compact messages (English / Hindi / Tamil) **from templates and verified
  values only** — a route or shelter is mentioned only if it passes the same checks
  as the M6 gate: hazard data fresh, route object fresh, route re-assessed feasible
  now, shelter outside any predicted hazard at arrival. If a check fails the message
  says that nothing can be confirmed; it never carries an unverified claim;
* works out the encoding and the number of SMS segments (GSM-7 vs UCS-2) so the
  cost of a message is known before it is sent;
* parses inbound keyword replies (ROUTE, SHELTER, SAFE, LANG, STOP, HELP).

Delivery goes through a provider interface. ``SimulatedProvider`` only records
messages in memory (no network, no keys), which is what the demo and the tests use.
A real gateway is another class with the same ``send`` method.
"""
from __future__ import annotations

import logging
import math
import re
from collections import deque

from .assistant import templates as TPL
from . import ponr

log = logging.getLogger("safe_engine.sms")

LANGS = ("en", "hi", "ta")
MAX_SEGMENTS = 4

# ----------------------------------------------------------------------------- encoding
_GSM7_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
                  "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM7_EXT = set("\f^{}\\[~]|€")


def sms_info(text: str) -> dict:
    """Encoding, length units and segment count of a message."""
    if all(ch in _GSM7_BASIC or ch in _GSM7_EXT for ch in text):
        units = sum(2 if ch in _GSM7_EXT else 1 for ch in text)
        single, multi, enc = 160, 153, "GSM-7"
    else:
        units = len(text.encode("utf-16-le")) // 2
        single, multi, enc = 70, 67, "UCS-2"
    segments = 1 if units <= single else math.ceil(units / multi)
    return {"encoding": enc, "units": units, "segments": segments}


# ----------------------------------------------------------------------------- templates
S = {
    "route_ok": {
        "en": "SAFE ROUTE for {vehicle}: {km} km, {min} min. {leave} Code {code}. Reply ROUTE {code} to recheck.",
        "hi": "सुरक्षित मार्ग ({vehicle}): {km} किमी, {min} मिनट। {leave} कोड {code}। दोबारा जाँचने के लिए ROUTE {code} भेजें।",
        "ta": "பாதுகாப்பான வழி ({vehicle}): {km} கி.மீ., {min} நிமிடம். {leave} குறியீடு {code}. மீண்டும் சரிபார்க்க ROUTE {code} அனுப்பவும்.",
    },
    "leave_by": {"en": "Leave by {hhmm}.", "hi": "{hhmm} तक निकलें।", "ta": "{hhmm} க்குள் புறப்படுங்கள்."},
    "leave_now": {"en": "Leave NOW.", "hi": "अभी निकलें।", "ta": "இப்போதே புறப்படுங்கள்."},
    "leave_clear": {"en": "No cut-off forecast.", "hi": "कोई अवरोध अनुमानित नहीं।", "ta": "துண்டிப்பு கணிக்கப்படவில்லை."},
    "trip_countdown": {
        "en": "TRIP: at most {minutes} min of delay before the road ahead is cut off ({hazard}). Reply SAFE when you arrive.",
        "hi": "यात्रा: आगे की सड़क ({hazard}) से कटने से पहले अधिकतम {minutes} मिनट की देरी संभव। पहुँचने पर SAFE भेजें।",
        "ta": "பயணம்: முன்னால் உள்ள சாலை ({hazard}) காரணமாக துண்டிக்கப்படும் முன் அதிகபட்சம் {minutes} நிமிட தாமதம். சேர்ந்ததும் SAFE அனுப்பவும்.",
    },
    "trip_urgent": {
        "en": "URGENT: road ahead predicted cut off by {hazard} in about {minutes} min.",
        "hi": "तत्काल: {hazard} से आगे की सड़क लगभग {minutes} मिनट में कटने का अनुमान।",
        "ta": "அவசரம்: {hazard} காரணமாக முன்னால் உள்ள சாலை சுமார் {minutes} நிமிடத்தில் துண்டிக்கப்படும் என கணிப்பு.",
    },
    "trip_clear": {
        "en": "TRIP: no cut-off predicted for the next {minutes} min.",
        "hi": "यात्रा: अगले {minutes} मिनट कोई अवरोध अनुमानित नहीं।",
        "ta": "பயணம்: அடுத்த {minutes} நிமிடங்களுக்கு துண்டிப்பு கணிக்கப்படவில்லை.",
    },
    "trip_blocked": {
        "en": "ALERT: road ahead not safe for your {vehicle}. Do not drive through water. Move to higher ground or a shelter. Emergency: 112.",
        "hi": "चेतावनी: आगे की सड़क आपके {vehicle} के लिए सुरक्षित नहीं। पानी से वाहन न चलाएँ। ऊँचे स्थान या आश्रय पर जाएँ। आपातकाल: 112।",
        "ta": "எச்சரிக்கை: முன்னால் உள்ள சாலை உங்கள் {vehicle} க்குப் பாதுகாப்பானது அல்ல. தண்ணீரில் வாகனம் ஓட்ட வேண்டாம். உயரமான இடம் அல்லது தங்குமிடம் செல்லுங்கள். அவசரம்: 112.",
    },
    "trip_done": {"en": "This trip has ended.", "hi": "यह यात्रा समाप्त हो चुकी है।", "ta": "இந்தப் பயணம் முடிந்துவிட்டது."},
    "no_safe": {
        "en": "Cannot confirm a safe route for your {vehicle} now. Do not drive through water. Follow official instructions. Emergency: 112.",
        "hi": "अभी आपके {vehicle} के लिए सुरक्षित मार्ग की पुष्टि नहीं हो सकी। पानी से वाहन न चलाएँ। आधिकारिक निर्देशों का पालन करें। आपातकाल: 112।",
        "ta": "இப்போது உங்கள் {vehicle} க்குப் பாதுகாப்பான வழியை உறுதிப்படுத்த முடியவில்லை. தண்ணீரில் வாகனம் ஓட்ட வேண்டாம். அதிகாரப்பூர்வ வழிகாட்டுதல்களைப் பின்பற்றுங்கள். அவசரம்: 112.",
    },
    "stale": {
        "en": "Hazard data is {age_min} min old; no route can be confirmed. Follow official instructions. Emergency: 112.",
        "hi": "खतरे का डेटा {age_min} मिनट पुराना है; किसी मार्ग की पुष्टि नहीं हो सकती। आधिकारिक निर्देशों का पालन करें। आपातकाल: 112।",
        "ta": "ஆபத்துத் தரவு {age_min} நிமிடம் பழையது; எந்த வழியையும் உறுதிப்படுத்த முடியவில்லை. அதிகாரப்பூர்வ வழிகாட்டுதல்களைப் பின்பற்றுங்கள். அவசரம்: 112.",
    },
    "shelter": {
        "en": "Nearest reachable shelter: {name}, {km} km, {min} min. Route code {code}.",
        "hi": "निकटतम पहुँच योग्य आश्रय: {name}, {km} किमी, {min} मिनट। मार्ग कोड {code}।",
        "ta": "அருகிலுள்ள அணுகக்கூடிய தங்குமிடம்: {name}, {km} கி.மீ., {min} நிமிடம். வழி குறியீடு {code}.",
    },
    "no_shelter": {
        "en": "No shelter can be reached safely now. Do not drive through water. Move to higher ground if safe. Emergency: 112.",
        "hi": "अभी कोई आश्रय सुरक्षित रूप से नहीं पहुँचा जा सकता। पानी से वाहन न चलाएँ। यदि सुरक्षित हो तो ऊँचे स्थान पर जाएँ। आपातकाल: 112।",
        "ta": "இப்போது எந்தத் தங்குமிடத்தையும் பாதுகாப்பாக அடைய முடியாது. தண்ணீரில் வாகனம் ஓட்ட வேண்டாம். பாதுகாப்பாக இருந்தால் உயரமான இடத்திற்குச் செல்லுங்கள். அவசரம்: 112.",
    },
    "out_of_area": {
        "en": "That location is outside the covered area.",
        "hi": "यह स्थान कवर किए गए क्षेत्र से बाहर है।",
        "ta": "இந்த இடம் உள்ளடக்கப்பட்ட பகுதிக்கு வெளியே உள்ளது.",
    },
    "help": {
        "en": "Reply: ROUTE <code> = recheck a route; SHELTER <lat>,<lon> = nearest shelter; SAFE = I am safe; LANG EN/HI/TA; STOP. Emergency: 112.",
        "hi": "उत्तर दें: ROUTE <कोड> = मार्ग जाँचें; SHELTER <अक्षांश>,<देशांतर> = निकटतम आश्रय; SAFE = मैं सुरक्षित हूँ; LANG EN/HI/TA; STOP। आपातकाल: 112।",
        "ta": "பதில்: ROUTE <குறியீடு> = வழியைச் சரிபார்க்க; SHELTER <lat>,<lon> = அருகிலுள்ள தங்குமிடம்; SAFE = நான் பாதுகாப்பாக இருக்கிறேன்; LANG EN/HI/TA; STOP. அவசரம்: 112.",
    },
    "ack_safe": {"en": "Thanks, your SAFE message is recorded.", "hi": "धन्यवाद, आपका SAFE संदेश दर्ज हो गया।", "ta": "நன்றி, உங்கள் SAFE செய்தி பதிவு செய்யப்பட்டது."},
    "stop": {
        "en": "You will get no more messages from this service. Emergency: 112.",
        "hi": "इस सेवा से अब आपको संदेश नहीं मिलेंगे। आपातकाल: 112।",
        "ta": "இந்தச் சேவையிலிருந்து இனி செய்திகள் வராது. அவசரம்: 112.",
    },
    "lang_set": {"en": "Language set to English.", "hi": "भाषा हिन्दी पर सेट हो गई।", "ta": "மொழி தமிழுக்கு மாற்றப்பட்டது."},
    "unknown": {"en": "Not understood. Reply HELP.", "hi": "समझ नहीं आया। HELP भेजें।", "ta": "புரியவில்லை. HELP அனுப்பவும்."},
    "bad_code": {
        "en": "That route code is not valid. Check it and try again.",
        "hi": "यह मार्ग कोड मान्य नहीं है। जाँचकर फिर कोशिश करें।",
        "ta": "இந்த வழி குறியீடு செல்லாது. சரிபார்த்து மீண்டும் முயலவும்.",
    },
    "need_coords": {
        "en": "Send SHELTER <lat>,<lon>, for example SHELTER 12.95,80.15.",
        "hi": "SHELTER <अक्षांश>,<देशांतर> भेजें, जैसे SHELTER 12.95,80.15।",
        "ta": "SHELTER <lat>,<lon> அனுப்பவும், எ.கா. SHELTER 12.95,80.15.",
    },
}


def _short_name(name: str, limit: int = 30) -> str:
    """Shelter names go into a 1-2 segment message: cut at a word boundary and drop an unclosed bracket."""
    if len(name) <= limit:
        return name
    cut = name[:limit].rsplit(" ", 1)[0] if " " in name[:limit] else name[:limit]
    return re.sub(r"\s*\([^)]*$", "", cut).rstrip(" ,-")


def render(key: str, lang: str, **kw) -> str:
    return S[key].get(lang, S[key]["en"]).format(**kw)


# ----------------------------------------------------------------------------- inbound parsing
_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_inbound(text: str) -> tuple[str, tuple]:
    """Return (command, args). Keywords are English (Latin letters) so any phone keypad can send them."""
    t = (text or "").strip()
    up = t.upper()
    if up in ("HELP", "?", "H"):
        return "help", ()
    if up == "SAFE":
        return "safe", ()
    if up in ("STOP", "UNSUBSCRIBE"):
        return "stop", ()
    m = re.fullmatch(r"LANG\s+(EN|HI|TA)", up)
    if m:
        return "lang", (m.group(1).lower(),)
    m = re.fullmatch(r"ROUTE\s+([A-Za-z0-9_-]{6,120})", t, re.I)    # codes are case-sensitive: keep the original
    if m:
        return "route", (m.group(1),)
    m = re.fullmatch(rf"SHELTER\s+{_NUM}\s*[, ]\s*{_NUM}", t, re.I)
    if m:
        return "shelter", (float(m.group(1)), float(m.group(2)))
    if up == "SHELTER":
        return "shelter_help", ()
    return "unknown", ()


def normalise_phone(p: str) -> str:
    q = re.sub(r"[\s\-()]", "", p or "")
    if not re.fullmatch(r"\+?\d{8,15}", q):
        raise ValueError("phone number must be 8-15 digits, optionally starting with +")
    return q


# ----------------------------------------------------------------------------- providers
class SimulatedProvider:
    """Records messages only. Nothing leaves the process."""
    name = "simulated"

    def send(self, to: str, body: str, meta: dict) -> dict:
        return {"provider": self.name, "delivered": False, "simulated": True}


# ----------------------------------------------------------------------------- gateway
class SmsGateway:
    def __init__(self, svc, provider=None):
        self.svc = svc
        self.provider = provider or SimulatedProvider()
        self.clear()

    def clear(self):
        self.log: deque = deque(maxlen=500)
        self.lang: dict[str, str] = {}
        self.stopped: set[str] = set()
        self._seq = 0
        self.safe_checkins: dict[str, float] = {}

    # -------------------------------------------------------------- bookkeeping
    def _record(self, direction: str, phone: str, body: str, kind: str, **extra) -> dict:
        self._seq += 1
        rec = {"id": f"SMS-{self._seq:06d}", "direction": direction, "phone": phone, "body": body, "kind": kind,
               "t": self.svc.now(), **sms_info(body), **extra}
        self.log.append(rec)
        return rec

    def outbox(self, phone: str | None = None, limit: int = 100) -> list[dict]:
        rows = [r for r in self.log if phone is None or r["phone"] == phone]
        return rows[-limit:]

    # -------------------------------------------------------------- verification (mirrors the M6 gate)
    def _stale(self) -> float | None:
        age = self.svc.now() - self.svc.store.updated_at
        return age if age > self.svc.settings.hazard_ttl_s else None

    def _veh(self, vehicle: str, lang: str) -> str:
        return TPL.name(TPL.VEHICLE_NAMES, vehicle, lang)

    def _refusal(self, lang: str, vehicle: str, age: float | None) -> str:
        if age is not None:
            return render("stale", lang, age_min=round(age / 60))
        return render("no_safe", lang, vehicle=self._veh(vehicle, lang))

    def _leave_text(self, res: dict, lang: str) -> str:
        if res["status"] == "countdown":
            if res["urgency"] == "red":
                return render("leave_now", lang)
            return render("leave_by", lang, hhmm=ponr.hhmm(res["leave_by_t"]))
        return render("leave_clear", lang)

    # -------------------------------------------------------------- composers
    def compose_route(self, route_id: str, lang: str, vehicle: str = "car") -> dict:
        svc, s = self.svc, self.svc.settings
        now = svc.now()
        c = svc.routes.get(route_id)
        veh = c["vehicle"] if c else vehicle
        age = self._stale()
        ok, why = False, "unknown_route"
        if age is None and c is not None:
            if now - c["created"] > s.object_ttl_s:
                why = "route_too_old"
            else:
                a = svc.reassess(route_id, now)
                ok, why = (a is not None and a["feasible"]), ("ok" if a and a["feasible"] else "infeasible")
        elif age is not None:
            why = "stale"
        if not ok:
            return {"body": self._refusal(lang, veh, age), "verified": False, "reason": why}
        info = svc.route_info(route_id)
        res = svc.route_leave_by(route_id, lang)
        body = render("route_ok", lang, vehicle=self._veh(veh, lang), km=f"{info['distance_m'] / 1000:.1f}",
                      min=round(info["duration_s"] / 60), leave=self._leave_text(res, lang), code=svc.route_code(route_id, veh))
        return {"body": body, "verified": True, "reason": "ok", "route_id": route_id, "leave_by": res}

    def compose_trip(self, trip_id: str, lang: str) -> dict:
        svc = self.svc
        res = svc.trip_leave_by(trip_id, lang)       # raises KeyError for an unknown trip
        t = svc.registry.trips.get(trip_id) or svc.trip_archive.get(trip_id)
        if res["status"] == "inactive":
            return {"body": render("trip_done", lang), "verified": True, "reason": "inactive", "trip_id": trip_id}
        age = self._stale()
        if age is not None:
            return {"body": self._refusal(lang, t.vehicle, age), "verified": False, "reason": "stale", "trip_id": trip_id}
        veh = self._veh(t.vehicle, lang)
        haz = ponr._hazard_text(res["hazard_classes"], lang)
        if res["status"] == "blocked":
            body = render("trip_blocked", lang, vehicle=veh)
        elif res["status"] == "clear":
            body = render("trip_clear", lang, minutes=max(1, round(res["window_s"] / 60)))
        elif res["urgency"] == "red":
            body = render("trip_urgent", lang, hazard=haz, minutes=max(1, round(res["seconds_left"] / 60)))
        else:
            body = render("trip_countdown", lang, hazard=haz, minutes=max(1, round(res["seconds_left"] / 60)))
        return {"body": body, "verified": True, "reason": "ok", "trip_id": trip_id, "leave_by": res}

    def compose_shelter(self, lon: float, lat: float, lang: str, vehicle: str = "car") -> dict:
        svc = self.svc
        x, y = svc.frame.to_xy(lon, lat)
        if not (0 <= x < svc.width and 0 <= y < svc.height):
            return {"body": render("out_of_area", lang), "verified": True, "reason": "out_of_area"}
        age = self._stale()
        if age is not None:
            return {"body": self._refusal(lang, vehicle, age), "verified": False, "reason": "stale"}
        now = svc.now()
        for sh in svc.nearest_shelters(lon, lat, 3):
            plan = svc.plan([lon, lat], [sh["lon"], sh["lat"]], vehicle, with_baseline=False)
            if not plan["chosen"]:
                continue
            r = next(x for x in plan["routes"] if x["id"] == plan["chosen"])
            arrival = now + r["duration_s"]
            if svc.point_hazarded(sh["lon"], sh["lat"], arrival - 300, arrival + 3600, vehicle):
                continue
            body = render("shelter", lang, name=_short_name(sh["name"]), km=f"{r['distance_m'] / 1000:.1f}", min=round(r["duration_s"] / 60),
                          code=r["route_code"])
            return {"body": body, "verified": True, "reason": "ok", "shelter_id": sh["id"], "route_id": r["id"]}
        return {"body": render("no_shelter", lang), "verified": False, "reason": "no_safe_shelter"}

    # -------------------------------------------------------------- outbound
    def send(self, to: str, lang: str, kind: str, *, vehicle: str = "car", route_id: str | None = None,
             trip_id: str | None = None, lon: float | None = None, lat: float | None = None) -> dict:
        phone = normalise_phone(to)
        lang = lang if lang in LANGS else "en"
        if phone in self.stopped:
            return {"sent": False, "suppressed": True, "reason": "recipient sent STOP"}
        if kind == "route":
            if not route_id:
                raise ValueError("route_id is required for kind=route")
            comp = self.compose_route(route_id, lang, vehicle)
        elif kind == "trip":
            if not trip_id:
                raise ValueError("trip_id is required for kind=trip")
            comp = self.compose_trip(trip_id, lang)
        elif kind == "shelter":
            if lon is None or lat is None:
                raise ValueError("lon and lat are required for kind=shelter")
            comp = self.compose_shelter(lon, lat, lang, vehicle)
        else:
            raise ValueError("kind must be route, trip or shelter")
        self.lang[phone] = lang
        meta = {k: v for k, v in comp.items() if k in ("verified", "reason", "route_id", "trip_id", "shelter_id")}
        rec = self._record("out", phone, comp["body"], kind, **meta)
        delivery = self.provider.send(phone, comp["body"], meta)
        return {"sent": True, "message": rec, "delivery": delivery, "leave_by": comp.get("leave_by")}

    # -------------------------------------------------------------- inbound
    def receive(self, sender: str, text: str) -> dict:
        phone = normalise_phone(sender)
        lang = self.lang.get(phone, "en")
        inbound = self._record("in", phone, text[:320], "inbound")
        cmd, args = parse_inbound(text)
        verified, reason, meta = True, "ok", {}
        if cmd == "help":
            body = render("help", lang)
        elif cmd == "safe":
            self.safe_checkins[phone] = self.svc.now()
            body = render("ack_safe", lang)
        elif cmd == "stop":
            self.stopped.add(phone)
            body = render("stop", lang)
        elif cmd == "lang":
            lang = args[0]
            self.lang[phone] = lang
            body = render("lang_set", lang)
        elif cmd == "route":
            try:
                dec = self.svc.decode_route(args[0])
            except Exception as e:       # any malformed or out-of-region code
                log.warning("bad route code from %s: %s", phone[-4:], e)
                body, verified, reason = render("bad_code", lang), False, "bad_code"
            else:
                comp = self.compose_route(dec["route_id"], lang, dec["vehicle"])
                body, verified, reason = comp["body"], comp["verified"], comp["reason"]
                meta = {"route_id": dec["route_id"]}
        elif cmd == "shelter":
            lat, lon = args
            comp = self.compose_shelter(lon, lat, lang)
            body, verified, reason = comp["body"], comp["verified"], comp["reason"]
            meta = {k: v for k, v in comp.items() if k in ("route_id", "shelter_id")}
        elif cmd == "shelter_help":
            body = render("need_coords", lang)
        else:
            body = render("unknown", lang)
        if phone in self.stopped and cmd != "stop":
            return {"in": inbound, "reply": None}
        reply = self._record("out", phone, body, "reply", verified=verified, reason=reason, command=cmd, **meta)
        self.provider.send(phone, body, {"command": cmd})
        return {"in": inbound, "reply": reply}
