import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analyzer, runtime_config, stats, storage
from app.config import settings
from app.db import engine, get_session, init_db
from app.models import Analysis, Capture, Crop, Round, Station
from app.quality import ImageDecodeError, check_quality
from app.seed import seed
from app.web import deps, routes_admin, routes_auth, routes_keys, routes_public_api
from app.web.templating import STATIC_DIR

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # form fields + boundaries on top of the image
log = logging.getLogger(__name__)


def safe_rollup(session: Session, day: date | None = None, station_id: int | None = None) -> None:
    """Stats must never break startup or a capture: swallow, log the exception type only (no values)."""
    try:
        stats.rollup(session, day, station_id)
    except Exception as e:
        log.warning("stats rollup failed: %s", type(e).__name__)
        try:
            session.rollback()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    analyzer.check_vision_config()
    with Session(engine) as s:
        seed(s)
    with Session(engine) as s:
        safe_rollup(s)  # catch-up: fill days that have captures but no daily_stats yet
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def reject_oversized(request: Request, call_next):
    # Runs before the form is parsed, so Starlette never spools a huge upload.
    # shortcut: a chunked upload has no Content-Length and is only caught by the read-time check; put a proxy limit in front when deployed.
    declared = request.headers.get("content-length", "")
    if request.url.path == "/captures" and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
        return JSONResponse({"detail": "image too large"}, status_code=413)
    return await call_next(request)


# No 'unsafe-inline': all CSS/JS are files under /static. When the three.js importmap lands, generate a
# per-request nonce in this middleware (request.state.csp_nonce), add 'nonce-<n>' to script-src and
# put nonce="{{ request.state.csp_nonce }}" on that one <script type="importmap">.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
_NO_PAGE_HEADERS = ("/captures", "/rounds", "/api/")  # device/JSON endpoints keep their responses untouched


@app.middleware("http")
async def page_security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(_NO_PAGE_HEADERS):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(routes_auth.router)
app.include_router(routes_admin.router)
app.include_router(routes_keys.router)
app.include_router(routes_public_api.router)
app.add_exception_handler(deps.CsrfError, routes_auth.csrf_error_page)


def require_device_key(x_device_key: str | None = Header(None), session: Session = Depends(get_session)) -> None:
    # web-created keys (hash) or env NEXXA_DEVICE_KEY; fail closed: no configured key means nobody gets in
    if not runtime_config.device_key_valid(x_device_key, session):
        raise HTTPException(401, "invalid device key")


@app.post("/rounds", dependencies=[Depends(require_device_key)])
def create_round(session: Session = Depends(get_session)):
    rnd = Round(status="open")
    session.add(rnd)
    session.commit()
    return {"round_id": rnd.id}


@app.post("/captures", dependencies=[Depends(require_device_key)])
def create_capture(
    image: UploadFile,
    rfid_uid: str = Form(),
    round_id: int = Form(),
    camera_angle: str | None = Form(None),
    session: Session = Depends(get_session),
):
    station = session.scalar(select(Station).where(Station.rfid_uid == rfid_uid))
    if station is None:
        raise HTTPException(404, "unknown rfid_uid")
    if station.kind == "home":
        raise HTTPException(422, "not an inspection station")
    if session.get(Round, round_id) is None:
        raise HTTPException(404, "unknown round")

    data = image.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image too large")
    try:
        quality = check_quality(data)
    except ImageDecodeError:
        raise HTTPException(422, "not an image")

    same_angle = (Capture.round_id == round_id, Capture.station_id == station.id, Capture.camera_angle == camera_angle)
    if session.scalar(select(Analysis.id).join(Capture, Analysis.capture_id == Capture.id).where(*same_angle).limit(1)):
        raise HTTPException(409, "already analyzed")
    previous = session.scalars(select(Capture.quality_ok).where(*same_angle)).all()
    attempt = len(previous) + 1
    failed_attempt = previous.count(False) + 1  # retake budget counts quality failures only
    capture = Capture(
        round_id=round_id,
        station_id=station.id,
        camera_angle=camera_angle,
        attempt=attempt,
        image_path="",
        quality_ok=quality.ok,
        quality_reason=quality.reason,
        crop_id=station.crop_id,
    )
    session.add(capture)
    session.flush()
    day = capture.taken_at.date()
    capture.image_path = storage.save_image(data, round_id, station.id, capture.id)

    label = confidence = None
    if not quality.ok:
        status = "retake" if failed_attempt <= settings.max_retakes else "needs_review"
    else:
        result = analyzer.analyze_leaf(data, session.get(Crop, station.crop_id))
        session.add(Analysis(
            capture_id=capture.id, label=result.label, confidence=result.confidence,
            note=result.note, model=result.model, needs_review=result.needs_review,
        ))
        label, confidence = result.label, result.confidence
        status = "needs_review" if result.needs_review else "analyzed"
    session.commit()
    body = {"capture_id": capture.id, "status": status, "label": label, "confidence": confidence, "attempt": attempt}
    safe_rollup(session, day, station.id)  # one upsert for this day/station; failure is swallowed
    return body
