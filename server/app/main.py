import logging
import os
import struct
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Path, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analyzer, robot, runtime_config, stats, storage, summaries, telegram
from app.config import settings
from app.db import engine, get_session, init_db
from app.models import Analysis, Capture, Crop, Round, Station
from app.quality import ImageDecodeError, check_quality, jpeg_decodes
from app.seed import seed
from app.web import deps, routes_admin, routes_auth, routes_keys, routes_public, routes_public_api, routes_telegram
from app.web.templating import STATIC_DIR

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # form fields + boundaries on top of the image
MAX_FRAME_BYTES = 200 * 1024  # one relayed camera frame (OV2640 JPEGs are typically 15-60 KB)
MAX_FRAME_DIM = 1600  # OV2640 max is 1600x1200
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


_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@app.middleware("http")
async def block_debug_docs(request: Request, call_next):
    raw_path = request.url.path.rstrip("/")
    if (request.url.path in _DOCS_PATHS or raw_path in _DOCS_PATHS) and os.getenv("NEXXA_ENV") != "development":
        return JSONResponse({"detail": "Not Found"}, status_code=404)
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
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store, private"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(routes_auth.router)
app.include_router(routes_admin.router)
app.include_router(routes_keys.router)
app.include_router(routes_telegram.router)
app.include_router(routes_public_api.router)
app.include_router(routes_public.router)
app.add_exception_handler(deps.CsrfError, routes_auth.csrf_error_page)
app.add_exception_handler(deps.PermissionDeniedError, routes_auth.permission_error_page)


def require_device_key(x_device_key: str | None = Header(None), session: Session = Depends(get_session)) -> None:
    # web-created keys (hash) or env NEXXA_DEVICE_KEY; fail closed: no configured key means nobody gets in
    if not runtime_config.device_key_valid(x_device_key, session):
        raise HTTPException(401, "invalid device key")


class RobotTelemetryInput(BaseModel):
    state: str | None = None
    round_id: int | None = None
    station_id: int | None = None
    rfid_uid: str | None = None
    camera_angle: str | None = None
    step: str | None = None
    step_index: int | None = None
    battery_pct: int | None = None
    wifi_rssi: int | None = None

    @field_validator("state")
    @classmethod
    def validate_state(cls, v):
        if v is not None and v not in robot.VALID_STATES:
            raise ValueError("invalid state")
        return v

    @field_validator("step_index")
    @classmethod
    def validate_step_index(cls, v):
        if v is not None and not (1 <= v <= 5):
            raise ValueError("step_index must be between 1 and 5")
        return v

    @field_validator("camera_angle")
    @classmethod
    def validate_camera_angle(cls, v):
        if v is not None and v not in {"front", "right", "back", "left"}:
            raise ValueError("camera_angle must be front, right, back, or left")
        return v

    @field_validator("battery_pct")
    @classmethod
    def validate_battery_pct(cls, v):
        if v is not None and not (0 <= v <= 100):
            raise ValueError("battery_pct must be between 0 and 100")
        return v


def _docked_summary(round_id: int) -> None:
    summaries.docked_fallback(round_id)
    telegram.notify(round_id, None)  # no-op when the round summary was not made or was already announced


@app.post("/api/robot/telemetry", dependencies=[Depends(require_device_key)])
def post_robot_telemetry(payload: RobotTelemetryInput, background: BackgroundTasks, session: Session = Depends(get_session)):
    robot.robot.update_telemetry(session, payload.model_dump(exclude_unset=True))
    if payload.state == "docked" and payload.round_id is not None:
        # fallback only: the robot normally calls POST /rounds/{id}/finish first, which makes the round summary
        try:
            if not summaries.has_round_summary(session, payload.round_id):
                background.add_task(_docked_summary, payload.round_id)
        except Exception as e:  # telemetry must never fail because of summaries
            log.warning("docked summary check failed: %s", type(e).__name__)
    return {"ok": True, "updated_at": robot.robot.updated_at}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the first SOF marker, or None. Read from the header so a tiny JPEG that claims
    65535x65535 is rejected before cv2 would allocate gigabytes for it."""
    if data[:3] != b"\xff\xd8\xff":
        return None
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # standalone markers, no length
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOFn
            if i + 9 > n:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker == 0xDA:  # start of scan before any SOF
            return None
        i += 2 + length
    return None


def _validate_and_store_frame(data: bytes) -> int:
    size = _jpeg_size(data)
    if size is None:
        raise HTTPException(422, "not a jpeg")
    if not (0 < size[0] <= MAX_FRAME_DIM and 0 < size[1] <= MAX_FRAME_DIM):
        raise HTTPException(422, f"image larger than {MAX_FRAME_DIM}x{MAX_FRAME_DIM}")
    if not jpeg_decodes(data):
        raise HTTPException(422, "not a jpeg")
    return robot.robot.store_frame(data)


@app.post("/api/robot/frame", dependencies=[Depends(require_device_key)])
async def post_robot_frame(request: Request):
    """Latest camera frame from the robot: raw JPEG body (Content-Type image/jpeg), <= 200 KB, <= 1600x1600,
    at most one accepted frame per second. Kept in memory only (see robot.py)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "image/jpeg":
        raise HTTPException(415, "Content-Type must be image/jpeg")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FRAME_BYTES:
        raise HTTPException(413, "frame too large")
    wait = robot.robot.frame_retry_after()
    if wait:
        raise HTTPException(429, "too many frames", headers={"Retry-After": str(wait)})
    chunks, total = [], 0
    async for chunk in request.stream():  # chunked uploads have no Content-Length: cap what we read
        total += len(chunk)
        if total > MAX_FRAME_BYTES:
            raise HTTPException(413, "frame too large")
        chunks.append(chunk)
    data = b"".join(chunks)
    wait = await run_in_threadpool(_validate_and_store_frame, data)
    if wait:
        raise HTTPException(429, "too many frames", headers={"Retry-After": str(wait)})
    return {"ok": True, "bytes": len(data), "interval_min_s": robot.FRAME_MIN_INTERVAL_S}


@app.post("/rounds", dependencies=[Depends(require_device_key)])
def create_round(session: Session = Depends(get_session)):
    rnd = Round(status="open")
    session.add(rnd)
    session.commit()
    robot.robot.update_from_round(rnd.id)
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
    # read everything the response and the rollup need before commit(): commit expires the ORM objects
    day, capture_id, station_id = capture.taken_at.date(), capture.id, station.id
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
    try:
        robot.robot.update_from_capture(session, round_id, station_id, camera_angle, quality.ok, status, attempt)
    except Exception:
        pass
    body = {"capture_id": capture_id, "status": status, "label": label, "confidence": confidence, "attempt": attempt}
    safe_rollup(session, day, station_id)  # one upsert for this day/station; failure is swallowed
    return body


RoundId = Annotated[int, Path(ge=1, le=2**31 - 1)]  # larger ids would overflow SQLite


def _summary_body(row, station_id: int | None) -> dict:
    """What the robot gets back. Key order matters: the firmware keeps only the first ~700 bytes, so `display` comes
    first and the (Thai, 3 bytes per character) headline last. `display` is ASCII built from the facts, never from AI text."""
    return {"ready": True, "display": summaries.display(row.facts), "source": row.source, "round_id": row.round_id,
            "station_id": station_id, "headline": summaries.device_headline(row.text)}


@app.post("/rounds/{round_id}/stations/{rfid_uid}/summary", dependencies=[Depends(require_device_key)])
def create_station_summary(round_id: RoundId, rfid_uid: str, background: BackgroundTasks, session: Session = Depends(get_session)):
    """The robot waits for this before driving on. Template text is always made; the AI wording gets 25 s at most."""
    if session.get(Round, round_id) is None:
        raise HTTPException(404, "unknown round")
    station = session.scalar(select(Station).where(Station.rfid_uid == rfid_uid))
    if station is None:
        raise HTTPException(404, "unknown rfid_uid")
    if session.scalar(select(Capture.id).where(Capture.round_id == round_id, Capture.station_id == station.id).limit(1)) is None:
        raise HTTPException(409, "no capture for this station in this round")
    body = _summary_body(summaries.generate_station_summary(session, round_id, station.id), station.id)
    background.add_task(telegram.notify, round_id, station.id)  # after the response; sent once per summary
    return body


@app.post("/rounds/{round_id}/finish", dependencies=[Depends(require_device_key)])
def finish_round(round_id: RoundId, background: BackgroundTasks, session: Session = Depends(get_session)):
    rnd = session.get(Round, round_id)
    if rnd is None:
        raise HTTPException(404, "unknown round")
    summaries.ensure_station_summaries(session, round_id)  # any the robot skipped: template only
    row = summaries.generate_round_summary(session, round_id)
    rnd = session.get(Round, round_id)
    if rnd.status == "open":  # a seeded "demo" round keeps its status
        rnd.status = "done"
        session.commit()
    body = _summary_body(row, None)
    background.add_task(telegram.notify, round_id, None)
    return body
