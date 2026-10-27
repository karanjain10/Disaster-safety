"""Answer generators.

* :class:`RulePlanner` – deterministic intent rules + tool calls. Used when no
  LLM key is configured (fully offline), and as the reference generator in the
  evaluation harness.
* :class:`GroqPlanner` – tool-calling LLM via the Groq OpenAI-compatible API
  (the original project already used Groq). Produces the same JSON schema.

Both only *propose* claims; nothing reaches the user without passing the gate.
"""
from __future__ import annotations

import json
import re

import httpx

from ..geography import parse_distance_question, parse_place_question
from .tools import TOOL_SPECS, Tools

SYSTEM_PROMPT = """You are the assistant of a disaster-aware navigation prototype in India.
For real-place names or coordinates, call get_place; for distances, call get_place_distance.
For current/local route, shelter,
or hazard facts, you MUST use tools (get_route, get_hazards, get_shelters, get_sop,
get_route_explanation, get_point_forecast).
Never guess current conditions or present general knowledge as a local observation.
For general educational geography or hazard questions, you may answer briefly from
general knowledge using a "general_information" claim and an explanation. This is
not local or live-verified information. Never present it as a current hazard alert.
For local hazards, route safety, and shelters, call the relevant tools and cite
their ids. If the user asks for local information but no location is available,
use a "needs_location" claim. Safety advice must cite get_sop.
When you are done, reply with ONLY a JSON object:
{"claims": [ {"type": "<type>", "ids": ["<ids returned by tools>"], ...} ], "explanation": "<optional short non-safety context>"}
Claim types:
- general_information: ids = [], brief educational answer goes in explanation; no local/current claims or safety instructions
- needs_location: ids = [], use when a location-specific question has no user location
- place_distance: ids = [place_distance_id returned by get_place_distance]
- place_lookup: ids = [place_id returned by get_place]
- route_recommendation: ids = [one route id with feasible=true]
- no_safe_route: ids = [all route ids returned, all infeasible]
- shelter_recommendation: ids = [shelter_id, route_id leading to it with reachable=true]
- hazard_warning: ids = [hazard ids]
- instruction: ids = [SOP clause ids from get_sop]
- hazard_report: {"type":"hazard_report","hazard":"flood|landslide|storm","depth":"ankle|knee|waist|unknown","text":"..."} when the user reports a hazard at their location
Do not add an unrelated SOP claim to general-information answers. Do not invent ids."""


def _coords(text: str):
    m = re.findall(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", text)
    pts = []
    for a, b in m:
        a, b = float(a), float(b)
        pts.append([b, a] if abs(a) < 40 and abs(b) > 40 else [a, b])  # accept "lat,lon" or "lon,lat" for India
    return pts


class RulePlanner:
    name = "rule-planner"

    def propose(self, question: str, tools: Tools, location, destination=None, feedback=None) -> dict:
        q = question.lower()
        claims, used_hazard = [], "any"
        general = offline_general_answer(question)
        pts = _coords(question)
        place_pair = parse_distance_question(question)
        if place_pair:
            result = tools.get_place_distance(*place_pair)
            return {"claims": [{"type": "place_distance", "ids": [result["place_distance_id"]]}],
                    "explanation": ""}
        place = parse_place_question(question)
        if place:
            result = tools.get_place(place)
            return {"claims": [{"type": "place_lookup", "ids": [result["place_id"]]}],
                    "explanation": ""}
        route_question = bool(re.search(r"\b(route|directions?|navigate|travel|drive|go to|reach|destination|safe to go)\b", q))
        dest = destination or (pts[-1] if pts and route_question else None)
        if general:
            return {"claims": [{"type": "general_information"}], "explanation": general}
        if re.search(r"\b(flooded|water logging|waterlogged|landslide here|tree fell|blocked here|report)\b", q) and location:
            hz = "landslide" if "landslide" in q else ("storm" if "tree" in q else "flood")
            depth = "waist" if "waist" in q else "knee" if "knee" in q else "ankle" if "ankle" in q else "unknown"
            claims.append({"type": "hazard_report", "hazard": hz, "depth": depth, "text": question[:200]})
            used_hazard = hz
        elif re.search(r"shelter|evacuat|where (should|can) i go|safe place|relief camp|आश्रय|தங்குமிட", q) and location:
            res = tools.get_shelters(location[0], location[1])
            ok = [s for s in res["shelters"] if s["reachable"]]
            if ok:
                claims.append({"type": "shelter_recommendation", "ids": [ok[0]["shelter_id"], ok[0]["route_id"]]})
            else:
                rids = [k for k, v in tools.ledger.objects.items() if v["kind"] == "route" and v["data"].get("shelter_id")]
                if rids:
                    claims.append({"type": "no_safe_route", "ids": rids})
            used_hazard = "flood"
        elif route_question and dest and location:
            res = tools.get_route(location, dest)
            if res["chosen"]:
                claims.append({"type": "route_recommendation", "ids": [res["chosen"]]})
            elif res["routes"]:
                claims.append({"type": "no_safe_route", "ids": [r["id"] for r in res["routes"]]})
            used_hazard = "flood"
        elif re.search(r"\b(hazard|danger|risk|flood(ing)?|landslide|storm|wind|near me|around me|at my location|खतरा|ஆபத்து)\b", q) and location:
            hz = _top_hazards(tools.get_hazards(location[0], location[1], 3)["hazards"])
            if hz:
                claims.append({"type": "hazard_warning", "ids": [h["id"] for h in hz]})
                used_hazard = hz[0]["class"]
        elif location_required(question):
            return {"claims": [{"type": "needs_location"}], "explanation": ""}
        else:
            claims.append({"type": "general_information"})
            return {"claims": claims, "explanation": (
                "I can answer general geography and hazard questions, but this offline demo "
                "only has built-in answers for common topics. Try asking about floods, "
                "landslides, storms, coordinates, elevation, or floodplains."
            )}
        sop = tools.get_sop(used_hazard if used_hazard != "closure" else "any")
        crit = [c["id"] for c in sop["clauses"] if c["critical"]][:2]
        if crit:
            claims.append({"type": "instruction", "ids": crit})
        return {"claims": claims, "explanation": ""}


def location_required(question: str) -> bool:
    return bool(re.search(
        r"\b(near me|around me|at my location|here now|current(ly)?|right now|"
        r"safe to go|safe to travel|route to|directions to|nearest shelter|"
        r"where should i go|is it flooded|is there flooding)\b",
        question.lower(),
    ))


def offline_general_answer(question: str) -> str | None:
    q = question.lower()
    topics = {
        "flood": "A flood is water covering land that is usually dry. Flooding can result from intense or prolonged rain, rivers overtopping their banks, coastal surge, or drainage being overwhelmed.",
        "landslide": "A landslide is the movement of rock, soil, or debris down a slope. Heavy rain can saturate and weaken slope material, although earthquakes, erosion, and human activity can also contribute.",
        "storm": "A storm is a period of disruptive weather that can include strong winds, heavy rain, thunder, or lightning. The hazards and impacts depend on the storm type and local conditions.",
        "cyclone": "A tropical cyclone is an organized rotating storm that forms over warm tropical waters. It can bring destructive winds, heavy rain, coastal surge, and flooding.",
        "earthquake": "An earthquake is ground shaking caused by a sudden release of energy in the Earth's crust, commonly along faults. Its impacts depend on magnitude, depth, distance, and local building and ground conditions.",
        "wildfire": "A wildfire is an uncontrolled fire spreading through vegetation. Its spread is affected by fuel, terrain, wind, humidity, and temperature.",
        "floodplain": "A floodplain is relatively flat land beside a river or stream that may be inundated when water levels rise. Being outside a mapped floodplain does not guarantee a place cannot flood.",
        "watershed": "A watershed (or drainage basin) is the area of land where rainfall and streams drain toward a common outlet, such as a river, lake, or the sea.",
        "latitude": "Latitude describes how far north or south a place is from the Equator, measured in degrees.",
        "longitude": "Longitude describes how far east or west a place is from the Prime Meridian, measured in degrees.",
        "elevation": "Elevation is the height of a location above a reference level, usually mean sea level. It is one factor in flood exposure, but does not by itself determine whether a location will flood.",
        "gis": "A geographic information system (GIS) stores, analyzes, and displays information tied to locations, such as roads, terrain, rainfall, and hazard areas.",
    }
    asks_definition = bool(re.search(r"\b(what is|define|meaning of|explain|how does|how do|why does|why do)\b", q))
    if not asks_definition:
        return None
    for topic, answer in topics.items():
        if re.search(rf"\b{re.escape(topic)}s?\b", q):
            return answer
    if "coordinate" in q or "coordinates" in q or "lat lon" in q:
        return "Geographic coordinates identify a point on Earth, commonly as latitude and longitude in degrees. Coordinate order matters, so check whether a system expects latitude-longitude or longitude-latitude."
    return None


class GroqPlanner:
    name = "groq-llm"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None, max_tool_rounds: int = 5):
        self.key, self.model = api_key, model
        self.client = client or httpx.Client(timeout=40)
        self.rounds = max_tool_rounds

    def _chat(self, messages):
        r = self.client.post("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {self.key}"},
                             json={"model": self.model, "messages": messages, "tools": TOOL_SPECS, "temperature": 0.1})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def propose(self, question: str, tools: Tools, location, destination=None, feedback=None) -> dict:
        ctx = f"User location (lon,lat): {location}. Vehicle: {tools.vehicle}. Answer language: {tools.lang}."
        if destination:
            ctx += f" Destination (lon,lat): {destination}."
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"{ctx}\n\nQuestion: {question}"}]
        if feedback:
            messages.append({"role": "user", "content": "Your previous answer failed verification: " + "; ".join(feedback) +
                             ". Call the tools again and return corrected JSON."})
        for _ in range(self.rounds):
            msg = self._chat(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                return _parse_json(msg.get("content") or "")
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = tools.call(c["function"]["name"], args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out)[:6000]})
        return {"claims": []}


def _top_hazards(items: list, n: int = 3) -> list:
    """Most relevant hazard per class (nearest, then most likely), up to n."""
    best = {}
    for h in sorted(items, key=lambda h: (h["distance_m"], -h["probability"], h["t_start"])):
        best.setdefault(h["class"], h)
    return list(best.values())[:n]


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"claims": [], "explanation": text[:300]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"claims": [], "explanation": text[:300]}
