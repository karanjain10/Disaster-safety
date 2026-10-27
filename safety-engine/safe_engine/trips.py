"""M5 — active-trip registry with a segment(cell)->trip reverse index, and the
capacity counters used for anti-herding route assignment.

The reverse index maps grid cells (default 250 m) crossed by the *remaining*
part of each trip to the trip id and the time window in which the vehicle is
expected inside that cell. When a new hazard version arrives, only trips with
a cell inside a changed part *and* an overlapping time window are re-evaluated.
"""
from __future__ import annotations

import itertools
import math
import threading
import time
from dataclasses import dataclass, field

from shapely.geometry import box

from .routing.base import Route

_tid = itertools.count(1)


@dataclass
class Trip:
    route: Route
    depart_t: float
    vehicle: str
    origin: tuple
    dest: tuple
    id: str = ""
    status: str = "active"
    events: list = field(default_factory=list)
    reroutes: int = 0
    last_p_safe: float = 1.0
    last_margin: float | None = None

    def __post_init__(self):
        if not self.id:
            self.id = f"T{next(_tid):06d}"

    def position(self, t: float) -> tuple[int, tuple]:
        """Index of the next coordinate and the interpolated position at time t."""
        el = t - self.depart_t
        if el <= 0:
            return 0, self.route.coords[0]
        acc = 0.0
        for i, d in enumerate(self.route.seg_dur):
            if acc + d >= el:
                f = (el - acc) / d if d > 0 else 0
                a, b = self.route.coords[i], self.route.coords[i + 1]
                return i + 1, (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            acc += d
        return len(self.route.coords) - 1, self.route.coords[-1]

    def remaining(self, t: float) -> tuple[Route, float]:
        """Remaining route from the current position, and the time it starts."""
        i, p = self.position(t)
        coords = [p] + list(self.route.coords[i:])
        durs = list(self.route.seg_dur[i:])
        if i > 0 and i - 1 < len(self.route.seg_dur):
            a, b = self.route.coords[i - 1], self.route.coords[i]
            full = math.dist(a, b) or 1.0
            durs = [self.route.seg_dur[i - 1] * math.dist(p, b) / full] + durs
        else:
            durs = [0.0] + durs if len(coords) - 1 > len(durs) else durs
        durs = durs[: len(coords) - 1]
        elev = None
        if self.route.elevated is not None:
            # keep the bridge / flyover flags (they exempt a segment from ponding) aligned with the shortened route
            src = self.route.elevated
            first = src[i - 1] if 0 < i <= len(src) else (src[0] if src else False)
            elev = ([first] + list(src[i:]) + [False] * len(durs))[: len(durs)]
        return Route(coords=coords, seg_dur=durs, id=self.route.id + "~", engine=self.route.engine, elevated=elev), max(t, self.depart_t)

    def arrived(self, t: float) -> bool:
        return t >= self.depart_t + self.route.duration


class TripRegistry:
    def __init__(self, cell_m: float = 250.0, eta_u: float = 0.15, capacity_veh_per_hr: float = 600.0):
        self.cell = cell_m
        self.u = eta_u
        self.cap = capacity_veh_per_hr
        self.trips: dict[str, Trip] = {}
        self.reverse: dict[tuple, dict[str, tuple]] = {}
        self.trip_cells: dict[str, list] = {}
        self.load: dict[tuple, int] = {}
        self.trip_load_keys: dict[str, list] = {}
        self.lock = threading.RLock()
        self.work = {"lookups": 0, "candidates": 0}

    # ------------------------------------------------------------------ helpers
    def _cells_with_times(self, route: Route, start_t: float):
        out: dict[tuple, list] = {}
        t = start_t
        for i, (a, b) in enumerate(zip(route.coords, route.coords[1:])):
            d = route.seg_dur[i] if i < len(route.seg_dur) else 0
            n = max(1, int(math.dist(a, b) // (self.cell / 2)))
            for k in range(n + 1):
                f = k / n
                x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                key = (int(x // self.cell), int(y // self.cell))
                tt = t + d * f
                lo, hi = tt - (tt - start_t) * self.u - 60, tt + (tt - start_t) * self.u + 60
                if key in out:
                    out[key][0], out[key][1] = min(out[key][0], lo), max(out[key][1], hi)
                else:
                    out[key] = [lo, hi]
            t += d
        return out

    def _load_keys(self, route: Route, depart_t: float):
        keys, t = set(), depart_t
        for i, (a, b) in enumerate(zip(route.coords, route.coords[1:])):
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            keys.add((round(mid[0] / 100), round(mid[1] / 100), int(t // 3600)))
            t += route.seg_dur[i] if i < len(route.seg_dur) else 0
        return list(keys)

    # ------------------------------------------------------------------ mutations
    def register(self, trip: Trip, now: float | None = None):
        with self.lock:
            self.trips[trip.id] = trip
            self._index(trip, now if now is not None else trip.depart_t)
            keys = self._load_keys(trip.route, trip.depart_t)
            for k in keys:
                self.load[k] = self.load.get(k, 0) + 1
            self.trip_load_keys[trip.id] = keys

    def _index(self, trip: Trip, now: float):
        for key in self.trip_cells.pop(trip.id, []):
            self.reverse.get(key, {}).pop(trip.id, None)
        rem, t0 = trip.remaining(now)
        cells = self._cells_with_times(rem, t0)
        for key, (lo, hi) in cells.items():
            self.reverse.setdefault(key, {})[trip.id] = (lo, hi)
        self.trip_cells[trip.id] = list(cells)

    def replace_route(self, trip: Trip, route: Route, now: float):
        with self.lock:
            for k in self.trip_load_keys.pop(trip.id, []):
                self.load[k] = max(0, self.load.get(k, 0) - 1)
            trip.route, trip.depart_t = route, now
            self._index(trip, now)
            keys = self._load_keys(route, now)
            for k in keys:
                self.load[k] = self.load.get(k, 0) + 1
            self.trip_load_keys[trip.id] = keys

    def remove(self, trip_id: str):
        with self.lock:
            trip = self.trips.pop(trip_id, None)
            for key in self.trip_cells.pop(trip_id, []):
                self.reverse.get(key, {}).pop(trip_id, None)
            for k in self.trip_load_keys.pop(trip_id, []):
                self.load[k] = max(0, self.load.get(k, 0) - 1)
            return trip

    # ------------------------------------------------------------------ queries
    def affected(self, parts) -> set[str]:
        """Trips whose remaining cells intersect a changed part during its time window."""
        out = set()
        with self.lock:
            for geom, t0, t1 in parts:
                minx, miny, maxx, maxy = geom.bounds
                for cx in range(int(minx // self.cell), int(maxx // self.cell) + 1):
                    for cy in range(int(miny // self.cell), int(maxy // self.cell) + 1):
                        entry = self.reverse.get((cx, cy))
                        if not entry:
                            continue
                        self.work["lookups"] += 1
                        cell_geom = box(cx * self.cell, cy * self.cell, (cx + 1) * self.cell, (cy + 1) * self.cell)
                        if not geom.intersects(cell_geom):
                            continue
                        for tid, (lo, hi) in entry.items():
                            self.work["candidates"] += 1
                            if lo <= t1 and hi >= t0:
                                out.add(tid)
        return out

    def load_factor(self, route: Route, depart_t: float) -> float:
        """Max assigned vehicles / capacity over the route's segment-hours."""
        keys = self._load_keys(route, depart_t)
        if not keys:
            return 0.0
        return max(self.load.get(k, 0) for k in keys) / self.cap

    def active(self) -> list[Trip]:
        return [t for t in self.trips.values() if t.status == "active"]
