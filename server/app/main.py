from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import analyzer, storage
from app.config import settings
from app.db import engine, get_session, init_db
from app.models import Analysis, Capture, Crop, Round, Station
from app.quality import ImageDecodeError, check_quality
from app.seed import seed

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as s:
        seed(s)
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/rounds")
def create_round(session: Session = Depends(get_session)):
    rnd = Round(status="open")
    session.add(rnd)
    session.commit()
    return {"round_id": rnd.id}


@app.post("/captures")
async def create_capture(
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

    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image too large")
    try:
        quality = check_quality(data)
    except ImageDecodeError:
        raise HTTPException(422, "not an image")

    attempt = 1 + session.scalar(
        select(func.count(Capture.id)).where(Capture.round_id == round_id, Capture.station_id == station.id)
    )
    capture = Capture(
        round_id=round_id,
        station_id=station.id,
        camera_angle=camera_angle,
        attempt=attempt,
        image_path=storage.save_image(data, round_id, station.id, attempt),
        quality_ok=quality.ok,
        quality_reason=quality.reason,
        crop_id=station.crop_id,
    )
    session.add(capture)
    session.flush()

    label = confidence = None
    if not quality.ok:
        status = "retake" if attempt <= settings.max_retakes else "needs_review"
    else:
        result = analyzer.analyze_leaf(data, session.get(Crop, station.crop_id))
        session.add(Analysis(
            capture_id=capture.id, label=result.label, confidence=result.confidence,
            note=result.note, model=result.model, needs_review=result.needs_review,
        ))
        label, confidence = result.label, result.confidence
        status = "needs_review" if result.needs_review else "analyzed"
    session.commit()
    return {"capture_id": capture.id, "status": status, "label": label, "confidence": confidence, "attempt": attempt}
