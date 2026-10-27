"""Place lookup and approximate distances using OpenStreetMap services."""
from __future__ import annotations

import math
import re
import threading
import time
from urllib.parse import quote

import httpx


def parse_distance_question(question: str) -> tuple[str, str] | None:
    q = question.strip().rstrip("?.! ")
    patterns = (
        r"\bdistance\s+(?:between|from)\s+(.+?)\s+(?:and|to)\s+(.+)$",
        r"\bhow\s+far\s+is\s+(.+?)\s+from\s+(.+)$",
        r"\bhow\s+far\s+from\s+(.+?)\s+to\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, q, re.I)
        if match:
            places = tuple(part.strip(" ,.") for part in match.groups())
            if all(places):
                return places
    return None


def parse_place_question(question: str) -> str | None:
    q = question.strip().rstrip("?.! ")
    patterns = (
        r"\bwhere\s+is\s+(.+?)(?:\s+located)?$",
        r"\b(?:coordinates?|location)\s+of\s+(.+)$",
        r"\blocate\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, q, re.I)
        if match:
            place = match.group(1).strip(" ,.")
            if place:
                return place
    return None


class PlaceLookup:
    def __init__(
        self,
        geocoder_url: str,
        router_url: str,
        user_agent: str,
        client: httpx.Client | None = None,
        min_geocode_interval_s: float = 1.05,
    ):
        self.geocoder_url = geocoder_url.rstrip("/")
        self.router_url = router_url.rstrip("/")
        self.client = client or httpx.Client(timeout=12, headers={"User-Agent": user_agent})
        self.min_geocode_interval_s = min_geocode_interval_s
        self._lock = threading.Lock()
        self._last_geocode = 0.0
        self._cache: dict[str, dict | None] = {}

    def _geocode(self, name: str) -> dict | None:
        key = name.casefold().strip()
        if key in self._cache:
            return self._cache[key]
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            wait = self.min_geocode_interval_s - (time.monotonic() - self._last_geocode)
            if wait > 0:
                time.sleep(wait)
            self._last_geocode = time.monotonic()
            response = self.client.get(
                f"{self.geocoder_url}/search",
                params={"q": name, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Place lookup returned an unexpected response")
            place = None
            if rows:
                row = rows[0]
                address = row.get("address") or {}
                place = {
                    "query": name,
                    "name": row.get("name") or name,
                    "display_name": row.get("display_name") or name,
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "country": address.get("country"),
                }
            self._cache[key] = place
            return place

    def find_place(self, name: str) -> dict:
        try:
            place = self._geocode(name)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return {"error": f"Place lookup failed: {exc}"}
        if not place:
            return {"error": f"Couldn't find '{name}'. Try adding a state or country to the place name."}
        return place

    @staticmethod
    def _straight_distance_km(a: dict, b: dict) -> float:
        lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
        dlat = lat2 - lat1
        dlon = math.radians(b["lon"] - a["lon"])
        hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, hav)))

    def distance_between(self, origin: str, destination: str) -> dict:
        try:
            a = self._geocode(origin)
            b = self._geocode(destination)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return {"error": f"Place lookup failed: {exc}"}
        if not a or not b:
            missing = origin if not a else destination
            return {"error": f"Couldn't find '{missing}'. Try adding a state or country to the place name."}

        result = {
            "origin": a,
            "destination": b,
            "straight_distance_km": round(self._straight_distance_km(a, b)),
            "road_distance_km": None,
            "road_duration_min": None,
            "road_source": None,
        }
        coordinates = f'{a["lon"]},{a["lat"]};{b["lon"]},{b["lat"]}'
        try:
            response = self.client.get(
                f"{self.router_url}/route/v1/driving/{quote(coordinates, safe=';,')}",
                params={"overview": "false", "steps": "false"},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Road-routing service returned an unexpected response")
            routes = data.get("routes", [])
            if data.get("code") == "Ok" and routes:
                result.update(
                    road_distance_km=round(float(routes[0]["distance"]) / 1000),
                    road_duration_min=round(float(routes[0]["duration"]) / 60),
                    road_source="OSRM",
                )
            else:
                result["road_unavailable"] = data.get("message") or data.get("code") or "No driving route found"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            result["road_unavailable"] = str(exc)
        return result
