"""Risk heatmap for the what-if time slider.

Rasterises the approved hazard slices that are active at a chosen moment onto a
coarse grid. A cell's value is the highest probability among the slices covering
it, so the picture is exactly what the router sees (model forecasts, approved
crowd reports, official alerts and admin closures all included).

With a vehicle given, slices the vehicle can drive through (water below its
threshold, wind below its limit) are left out, so the map shows risk *for that
vehicle*.
"""
from __future__ import annotations

import math

import numpy as np
import shapely

from .vehicles import get_profile

MAX_SIDE = 90   # longest side of the returned grid, in cells
CLASSES = ("flood", "landslide", "storm", "closure")


def risk_grid(svc, t: float, hclass: str | None = None, vehicle: str | None = None) -> dict:
    T = svc.terrain
    nx, ny = T.nx, T.ny
    block = max(1, math.ceil(max(nx, ny) / MAX_SIDE))
    gx, gy = math.ceil(nx / block), math.ceil(ny / block)
    size = T.cell * block
    xs = (np.arange(gx) + 0.5) * size
    ys = (np.arange(gy) + 0.5) * size
    X, Y = np.meshgrid(xs, ys)             # row 0 = southern edge
    grid = np.zeros((gy, gx), dtype=float)
    veh = get_profile(vehicle) if vehicle else None
    used = 0
    for s in svc.store.slices.values():
        if s.status != "approved" or not s.overlaps(t, t + 1):
            continue
        if hclass and s.hclass != hclass:
            continue
        if veh is not None and s.hclass in ("flood", "storm") and s.intensity <= veh.threshold(s.hclass):
            continue
        minx, miny, maxx, maxy = s.geom.bounds
        c0, c1 = max(0, int(minx // size)), min(gx - 1, int(maxx // size))
        r0, r1 = max(0, int(miny // size)), min(gy - 1, int(maxy // size))
        if c0 > c1 or r0 > r1:
            continue
        sub_x, sub_y = X[r0:r1 + 1, c0:c1 + 1], Y[r0:r1 + 1, c0:c1 + 1]
        inside = shapely.contains_xy(s.geom, sub_x, sub_y)
        if inside.any():
            used += 1
            region = grid[r0:r1 + 1, c0:c1 + 1]
            np.maximum(region, np.where(inside, s.probability, 0.0), out=region)
    lon0, lat0 = svc.frame.to_lonlat(0, 0)
    lon1, lat1 = svc.frame.to_lonlat(gx * size, gy * size)
    return {"t": t, "offset_min": round((t - svc.now()) / 60), "vehicle": vehicle, "class": hclass,
            "bbox": [lon0, lat0, lon1, lat1], "nx": gx, "ny": gy, "cell_m": size,
            "values": np.round(grid, 3).tolist(),      # row 0 = south; the client flips it for display
            "max": round(float(grid.max()), 3) if grid.size else 0.0,
            "share_at_risk": round(float((grid >= 0.3).mean()), 4) if grid.size else 0.0,
            "slices_used": used, "hazard_version": svc.store.version, "synthetic": svc.synthetic}
