"""FastAPI application for the safety engine.

Run:  uvicorn safe_engine.api:app --port 8000

The Spring Boot gateway forwards /engine/** here and adds:
  X-Engine-Key  : shared secret (required when ENGINE_KEY is set)
  X-User-Email  : signed-in user
  X-User-Role   : USER or ADMIN
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .service import EngineService
from .vehicles import PROFILES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("safe_engine.api")

app = FastAPI(title="Disaster-Aware Safety Engine", version="1.0.0",
              description="Spatio-temporal, vehicle-specific hazard-aware routing with verified guidance (M1–M6).")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","), allow_methods=["*"], allow_headers=["*"])

_svc: EngineService | None = None
_svc_lock = threading.Lock()


def svc() -> EngineService:
    global _svc
    with _svc_lock:
        if _svc is None:
            _svc = EngineService(settings)
            if settings.mode == "live":
                threading.Thread(target=_live_loop, daemon=True).start()
        return _svc


def _live_loop():
    while True:
        time.sleep(300)
        try:
            _svc.refresh()
        except Exception as e:  # pragma: no cover
            log.exception("refresh failed: %s", e)


class Caller(BaseModel):
    email: str = "anonymous"
    role: str = "USER"


def caller(x_engine_key: Optional[str] = Header(None), x_user_email: Optional[str] = Header(None),
           x_user_role: Optional[str] = Header(None)) -> Caller:
    if settings.engine_key and x_engine_key != settings.engine_key:
        raise HTTPException(401, "invalid engine key")
    return Caller(email=x_user_email or "anonymous", role=(x_user_role or "USER").upper())


def admin(c: Caller = Depends(caller)) -> Caller:
    if c.role != "ADMIN" and not (settings.mode == "scenario" and os.environ.get("DEMO_ADMIN", "1") == "1"):
        raise HTTPException(403, "admin role required")
    return c


# ---------------------------------------------------------------------------- models
class PlanReq(BaseModel):
    origin: list[float] = Field(..., description="[lon, lat]")
    destination: list[float] = Field(..., description="[lon, lat]")
    vehicle: str = "car"
    depart_in_min: float = 0
    k: Optional[int] = None


class ReportReq(BaseModel):
    lon: float
    lat: float
    hazard: str = "flood"
    depth: str = "unknown"
    text: str = ""


class AskReq(BaseModel):
    question: str
    lang: str = "en"
    location: Optional[list[float]] = None
    destination: Optional[list[float]] = None
    vehicle: str = "car"
    horizon_min: int = Field(60, ge=0, le=120)


class SmsSendReq(BaseModel):
    to: str = Field(..., description="recipient phone number (simulated provider: nothing is sent)")
    lang: str = Field("en", pattern="^(en|hi|ta)$")
    kind: str = Field(..., pattern="^(route|trip|shelter)$")
    vehicle: str = "car"
    route_id: Optional[str] = None
    trip_id: Optional[str] = None
    lon: Optional[float] = None
    lat: Optional[float] = None


class SmsInboundReq(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    sender: str = Field(..., alias="from")
    text: str = Field(..., max_length=320)


class ClosureReq(BaseModel):
    geometry: dict
    hazard: str = "closure"
    hours: float = 6
    note: str = ""


# ---------------------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"ok": True, "initialised": _svc is not None}


@app.get("/status")
def status(c: Caller = Depends(caller)):
    return svc().status()


@app.get("/vehicles")
def vehicles():
    return [p.to_dict() for p in PROFILES.values()]


@app.post("/scenario/advance")
def advance(minutes: float = Query(15, ge=0, le=24 * 60), c: Caller = Depends(caller)):
    s = svc()
    if not s.synthetic:
        raise HTTPException(400, "clock control is only available in scenario mode")
    return s.advance(minutes * 60)


@app.post("/scenario/reset")
def reset(seed: int = Query(7), c: Caller = Depends(caller)):
    s = svc()
    if not s.synthetic:
        raise HTTPException(400, "scenario mode only")
    s.reset(seed)
    return s.status()


@app.post("/plan")
def plan(req: PlanReq, c: Caller = Depends(caller)):
    s = svc()
    try:
        return s.plan(req.origin, req.destination, req.vehicle, s.now() + req.depart_in_min * 60, req.k)
    except Exception as e:
        log.exception("plan failed")
        raise HTTPException(502, f"routing failed: {e}")


@app.get("/routes/{route_id}/reassess")
def reassess(route_id: str, c: Caller = Depends(caller)):
    a = svc().reassess(route_id, svc().now())
    if a is None:
        raise HTTPException(404, "unknown route")
    return a


@app.get("/routes/{route_id}/explain")
def explain_route(route_id: str, lang: str = Query("en", pattern="^(en|hi|ta)$"), c: Caller = Depends(caller)):
    rec = svc().explain_route(route_id, lang)
    if rec is None:
        raise HTTPException(404, "unknown route")
    return rec


@app.get("/forecast/point")
def forecast_point(lon: float, lat: float, horizon_min: int = Query(60, ge=0, le=120), c: Caller = Depends(caller)):
    fc = svc().forecast_point(lon, lat, horizon_min)
    if fc is None:
        raise HTTPException(400, "point outside forecast region")
    return fc


@app.get("/routes/{route_id}/leave-by")
def route_leave_by(route_id: str, lang: str = Query("en", pattern="^(en|hi|ta)$"), c: Caller = Depends(caller)):
    res = svc().route_leave_by(route_id, lang)
    if res is None:
        raise HTTPException(404, "unknown route")
    return res


@app.get("/routes/decode/{code}")
def decode(code: str, c: Caller = Depends(caller)):
    try:
        return svc().decode_route(code)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/hazards")
def hazards(t: Optional[float] = None, offset_min: float = 0, window_min: float = Query(0, ge=0, le=120),
            pending: bool = True, c: Caller = Depends(caller)):
    s = svc()
    start = (s.now() if t is None else t) + offset_min * 60
    return s.hazards_geojson(start, include_pending=pending, t_end=start + max(0, window_min) * 60)


@app.get("/hazards/heatmap")
def heatmap(offset_min: float = Query(0, ge=0, le=120), hazard_class: Optional[str] = Query(None, pattern="^(flood|landslide|storm|closure)$"),
            vehicle: Optional[str] = None, c: Caller = Depends(caller)):
    if vehicle is not None and vehicle not in PROFILES:
        raise HTTPException(400, "unknown vehicle")
    return svc().heatmap(offset_min, hazard_class, vehicle)


@app.get("/hazards/truth")
def truth(offset_min: float = 0, c: Caller = Depends(caller)):
    s = svc()
    return s.truth_geojson(s.now() + offset_min * 60)


@app.post("/trips")
def start_trip(route_id: str = Body(..., embed=True), c: Caller = Depends(caller)):
    try:
        return svc().start_trip(route_id, c.email)
    except KeyError:
        raise HTTPException(404, "unknown route")


@app.get("/trips/{trip_id}")
def trip(trip_id: str, since: int = 0, c: Caller = Depends(caller)):
    try:
        return svc().trip_status(trip_id, since)
    except KeyError:
        raise HTTPException(404, "unknown trip")


@app.get("/trips/{trip_id}/leave-by")
def trip_leave_by(trip_id: str, lang: str = Query("en", pattern="^(en|hi|ta)$"), c: Caller = Depends(caller)):
    try:
        return svc().trip_leave_by(trip_id, lang)
    except KeyError:
        raise HTTPException(404, "unknown trip")


@app.delete("/trips/{trip_id}")
def cancel(trip_id: str, c: Caller = Depends(caller)):
    svc().cancel_trip(trip_id)
    return {"ok": True}


@app.post("/reports")
def report(req: ReportReq, c: Caller = Depends(caller)):
    return svc().report(req.lon, req.lat, req.hazard, req.depth, c.email, req.text)


@app.get("/evidence")
def evidence(c: Caller = Depends(caller)):
    s = svc()
    return {"items": s.fusion.to_list(s.now())}


@app.post("/assistant/ask")
def ask(req: AskReq, c: Caller = Depends(caller)):
    s = svc()
    return s.assistant.ask(req.question, req.lang, req.location, req.vehicle, req.destination,
                           hazard_horizon_min=req.horizon_min)


@app.post("/sms/send")
def sms_send(req: SmsSendReq, c: Caller = Depends(caller)):
    try:
        return svc().sms.send(req.to, req.lang, req.kind, vehicle=req.vehicle, route_id=req.route_id, trip_id=req.trip_id,
                              lon=req.lon, lat=req.lat)
    except KeyError:
        raise HTTPException(404, "unknown trip")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/sms/inbound")
def sms_inbound(req: SmsInboundReq, c: Caller = Depends(caller)):
    try:
        return svc().sms.receive(req.sender, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/sms/outbox")
def sms_outbox(phone: Optional[str] = None, limit: int = Query(100, ge=1, le=500), c: Caller = Depends(caller)):
    from .sms import normalise_phone
    try:
        p = normalise_phone(phone) if phone else None
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"provider": svc().sms.provider.name, "messages": svc().sms.outbox(p, limit)}


@app.get("/shelters")
def shelters(lon: float, lat: float, n: int = 5, c: Caller = Depends(caller)):
    return svc().nearest_shelters(lon, lat, n)


@app.get("/offline/pack")
def offline_pack(lon: float, lat: float, radius_km: float = 5, hours: float = 3, c: Caller = Depends(caller)):
    return svc().offline_pack(lon, lat, radius_km, hours)


@app.get("/model/metrics")
def model_metrics(c: Caller = Depends(caller)):
    return svc().model.metrics


@app.get("/stats/revalidation")
def reval_stats(c: Caller = Depends(caller)):
    return svc().reval_stats[-50:]


# ---------------------------------------------------------------------------- admin
@app.get("/admin/pending")
def pending(c: Caller = Depends(admin)):
    s = svc()
    out = []
    for p in s.store.pending():
        d = p.to_dict(s.frame)
        out.append(d)
    return out


@app.post("/admin/slices/{slice_id}/{action}")
def admin_action(slice_id: str, action: str, c: Caller = Depends(admin)):
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    try:
        return svc().admin_set(slice_id, "approved" if action == "approve" else "rejected", c.email)
    except KeyError:
        raise HTTPException(404, "unknown slice")


@app.post("/admin/closures")
def admin_closure(req: ClosureReq, c: Caller = Depends(admin)):
    return svc().admin_add_closure(req.geometry, req.hazard, req.hours, c.email, req.note)


@app.post("/admin/cap")
async def admin_cap(request: Request, c: Caller = Depends(admin)):
    body = (await request.body()).decode("utf-8", "replace")
    try:
        return svc().ingest_cap(body)
    except Exception as e:
        raise HTTPException(400, f"invalid CAP XML: {e}")


@app.get("/admin/audit")
def audit(c: Caller = Depends(admin)):
    return svc().store.audit[-200:]


# ---------------------------------------------------------------------------- standalone UI
# When the engine runs on its own (no Spring gateway), it serves the same console
# with ENGINE_BASE = "" so the page calls this API directly.
_UI_DIR = os.environ.get("UI_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "src", "main", "resources", "static"))


@app.get("/", include_in_schema=False)
def ui_index():
    path = os.path.join(_UI_DIR, "index.html")
    if not os.path.exists(path):
        return PlainTextResponse("Safety engine running. UI not found; set UI_DIR.", 200)
    html = open(path, encoding="utf-8").read().replace('<script src="console.js">', '<script>window.ENGINE_BASE="";</script><script src="console.js">')
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.get("/console.{ext}", include_in_schema=False)
def ui_asset(ext: str):
    if ext not in ("js", "css"):
        raise HTTPException(404)
    return FileResponse(os.path.join(_UI_DIR, f"console.{ext}"))


@app.get("/predictions", include_in_schema=False)
@app.get("/predictions.html", include_in_schema=False)
def predictions_ui():
    path = os.path.join(_UI_DIR, "predictions.html")
    if not os.path.exists(path):
        return PlainTextResponse("Prediction UI not found; set UI_DIR.", 404)
    html = open(path, encoding="utf-8").read().replace(
        '<script src="predictions.js">',
        '<script>window.ENGINE_BASE="";</script><script src="predictions.js">'
    )
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.get("/predictions.{ext}", include_in_schema=False)
def predictions_asset(ext: str):
    if ext not in ("js", "css"):
        raise HTTPException(404)
    return FileResponse(os.path.join(_UI_DIR, f"predictions.{ext}"))
