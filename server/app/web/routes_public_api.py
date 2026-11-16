"""Public, read-only, login-free JSON API: aggregate numbers, statuses, station/crop names, route order, map positions.
Never add image paths, notes, rfid_uid, keys or user data here.

Rate limit is in memory per client IP (request.client). Behind a reverse proxy run uvicorn with --proxy-headers
(and --forwarded-allow-ips) or every visitor shares the proxy's IP.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app import robot, stats, summaries
from app.db import get_session
from app.models import Analysis, AppSetting, Batch, Capture, Crop, DailyStat, Round, Station, Summary

RATE_LIMIT = 60  # requests per WINDOW_S per IP
WINDOW_S = 60
MAX_TRACKED_IPS = 10_000
HARVEST_AGE_DAYS = stats.HARVEST_AGE_DAYS  # reported as harvest_cycle_days_assumed
NEAR_HARVEST_DAYS = 7  # list a lot when it has <= this many days left ...
OVERDUE_SHOWN_DAYS = 14  # ... and until it is this many days overdue (harvest_in_days is then negative)
WEEKLY_OVER = 100  # a series with more points than this is bucketed to weeks (Monday start)
MAX_SAME_AGE_DAYS, MAX_SAME_AGE_BATCHES = 120, 6
REVIEW_WINDOW_DAYS = 7  # no "resolved" state exists yet, so "pending" = needs_review within the last week
INT_MAX = 2**31 - 1

_clock = time.monotonic
_lock = threading.Lock()  # limit_and_cache is a sync dependency: it runs in the threadpool
_hits: dict[str, list] = {}  # ip -> [window_start, count]; shortcut: single process, use a shared store if scaled out


def allow(ip: str) -> bool:
    with _lock:
        return _allow(ip)


def _allow(ip: str) -> bool:
    now = _clock()
    if len(_hits) >= MAX_TRACKED_IPS and ip not in _hits:
        for k in [k for k, v in _hits.items() if now - v[0] >= WINDOW_S]:
            del _hits[k]
        if len(_hits) >= MAX_TRACKED_IPS:  # still full of live windows: drop the oldest, never grow unbounded
            for k in sorted(_hits, key=lambda k: _hits[k][0])[: MAX_TRACKED_IPS // 10 + 1]:
                del _hits[k]
    entry = _hits.get(ip)
    if entry is None or now - entry[0] >= WINDOW_S:
        _hits[ip] = [now, 1]
        return True
    entry[1] += 1
    return entry[1] <= RATE_LIMIT


def limit_and_cache(request: Request, response: Response) -> None:
    ip = request.client.host if request.client else "unknown"
    if not allow(ip):
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(WINDOW_S), "Cache-Control": "no-store"})
    req_url = getattr(request, "url", None)
    req_path = getattr(req_url, "path", "") if req_url else ""
    if not req_path.endswith("/robot/frame"):  # the frame route sets its own no-store
        response.headers["Cache-Control"] = "public, max-age=30"


router = APIRouter(prefix="/api/public", dependencies=[Depends(limit_and_cache)])


VISIBLE = (Station.kind == "station", Station.active.is_(True))  # what the public API may show at all


def _rate(num, den):
    return None if not den else num / den


def _demo(session: Session) -> bool:
    return session.scalar(select(AppSetting.value).where(AppSetting.key == "demo")) == "1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/summary")
def summary(session: Session = Depends(get_session)):
    day = stats.today()
    n_cap, normal, yellow, brown = (int(x or 0) for x in session.execute(
        select(func.sum(DailyStat.n_captures), func.sum(DailyStat.n_normal), func.sum(DailyStat.n_yellow), func.sum(DailyStat.n_brown))
        .where(DailyStat.day == day)).one())
    since = datetime.combine(day, datetime.min.time()) - timedelta(days=REVIEW_WINDOW_DAYS)
    pending = session.scalar(
        select(func.count(Analysis.id)).join(Capture, Analysis.capture_id == Capture.id)
        .where(Analysis.needs_review.is_(True), Capture.taken_at >= since))
    open_batches = session.execute(
        select(Batch, Station.name, Crop.name).join(Station, Batch.station_id == Station.id)
        .join(Crop, Batch.crop_id == Crop.id).where(Batch.harvested_on.is_(None), Batch.planted_on <= day, *VISIBLE))
    lots = []
    for b, station, crop in open_batches:
        age = stats.age_days(b, day)
        left = HARVEST_AGE_DAYS - age
        if -OVERDUE_SHOWN_DAYS <= left <= NEAR_HARVEST_DAYS:
            lots.append({"station": station, "crop": crop, "age_days": age, "harvest_in_days": left})
    lots.sort(key=lambda x: (x["harvest_in_days"], x["station"]))
    return {
        "updated_at": _now_iso(),
        "demo": _demo(session),
        "stations_active": session.scalar(select(func.count(Station.id)).where(*VISIBLE)),
        "captures_today": n_cap,
        "abnormal_rate_today": _rate(yellow + brown, normal + yellow + brown),
        "review_pending": pending,
        "lots_near_harvest": lots,
        "harvest_cycle_days_assumed": HARVEST_AGE_DAYS,
    }


def _point(day, n, ok, normal, yellow, brown, review) -> dict:
    labelled = normal + yellow + brown  # yellow/brown rates are over analysed leaves, review/rejected over all captures
    return {"day": day.isoformat(), "n": n, "yellow_rate": _rate(yellow, labelled), "brown_rate": _rate(brown, labelled),
            "review_rate": _rate(review, n), "rejected_rate": _rate(n - ok, n)}


def _weekly(rows: list) -> list:
    buckets: dict = {}
    for day, *vals in rows:
        key = day - timedelta(days=day.weekday())
        acc = buckets.setdefault(key, [0] * len(vals))
        for i, v in enumerate(vals):
            acc[i] += v
    return [(k, *v) for k, v in sorted(buckets.items())]


@router.get("/trends")
def trends(
    days: int = Query(30, ge=1, le=365),
    station_id: int | None = Query(None, ge=1, le=INT_MAX),
    session: Session = Depends(get_session),
):
    start = stats.today() - timedelta(days=days - 1)
    where = [DailyStat.day >= start, DailyStat.day <= stats.today()]
    if station_id is not None:
        where.append(DailyStat.station_id == station_id)
    q = (
        select(DailyStat.station_id, DailyStat.day, *(func.sum(c) for c in (
            DailyStat.n_captures, DailyStat.n_quality_ok, DailyStat.n_normal, DailyStat.n_yellow, DailyStat.n_brown, DailyStat.n_review)))
        .join(Station, DailyStat.station_id == Station.id).where(*where, *VISIBLE)
        .group_by(DailyStat.station_id, DailyStat.day).order_by(DailyStat.station_id, DailyStat.day)
    )
    per_station: dict[int, list] = {}
    for sid, day, *vals in session.execute(q):
        per_station.setdefault(sid, []).append((day, *(int(v or 0) for v in vals)))
    names = dict(session.execute(select(Station.id, Station.name).where(Station.id.in_(per_station))).all()) if per_station else {}
    series = []
    for sid, pts in per_station.items():
        if len(pts) > WEEKLY_OVER:
            pts = _weekly(pts)
        series.append({"station_id": sid, "station": names.get(sid), "points": [_point(*p) for p in pts]})
    return {"days": days, "series": series, "same_age": _same_age(session, where)}


def _same_age(session: Session, where: list) -> list:
    # the newest MAX batches that have stats in the window, compared on the same age (days since planting)
    top = [r[0] for r in session.execute(
        select(DailyStat.batch_id).join(Batch, DailyStat.batch_id == Batch.id)
        .join(Station, DailyStat.station_id == Station.id).where(*where, *VISIBLE)
        .group_by(DailyStat.batch_id, Batch.planted_on).order_by(Batch.planted_on.desc(), DailyStat.batch_id.desc())
        .limit(MAX_SAME_AGE_BATCHES))]
    if not top:
        return []
    rows = session.execute(
        select(Batch.planted_on, Batch.id, Station.name, DailyStat.day, DailyStat.n_normal, DailyStat.n_yellow, DailyStat.n_brown)
        .join(Batch, DailyStat.batch_id == Batch.id).join(Station, Batch.station_id == Station.id)
        .where(*where, DailyStat.batch_id.in_(top))).all()
    by_age: dict[int, dict] = {}
    for planted, bid, station, day, normal, yellow, brown in rows:
        age = (day - planted).days
        labelled = normal + yellow + brown
        if not 0 <= age < MAX_SAME_AGE_DAYS or not labelled:
            continue
        label = f"{station} ({planted.isoformat()})"
        acc = by_age.setdefault(age, {}).setdefault(label, [0, 0])
        acc[0] += yellow + brown
        acc[1] += labelled
    return [{"age_day": a, "batches": [{"batch_label": k, "abnormal_rate": v[0] / v[1]} for k, v in sorted(bs.items())]}
            for a, bs in sorted(by_age.items())]


_STATUS = {stats.NORMAL: "normal", stats.YELLOW: "yellow", stats.BROWN: "brown"}


def _utc_z(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat() + "Z"


@router.get("/twin")
def twin(session: Session = Depends(get_session)):
    day = stats.today()
    stations = session.execute(
        select(Station, Crop.name).outerjoin(Crop, Station.crop_id == Crop.id)
        .where(*VISIBLE).order_by(Station.route_order, Station.id)).all()
    batches = session.scalars(
        select(Batch).where(
            Batch.planted_on <= day,
            or_(Batch.harvested_on.is_(None), Batch.harvested_on >= day),
        )
    ).all()
    out = []
    for st, crop in stations:
        # one indexed LIMIT 1 per station (captures(station_id, taken_at), analyses(capture_id)); a window query
        # over all analyses would scan the whole table on every unauthenticated request.
        # shortcut: fine for a handful of stations; revisit at hundreds.
        last = session.execute(
            select(Analysis.label, Analysis.confidence, Analysis.needs_review, Capture.taken_at)
            .join(Capture, Analysis.capture_id == Capture.id).where(Capture.station_id == st.id)
            .order_by(Capture.taken_at.desc(), Analysis.id.desc()).limit(1)).first()
        batch = stats.active_batch([b for b in batches if b.station_id == st.id], day)
        status, seen, conf = "unknown", None, None
        if last:
            status = "review" if last.needs_review else _STATUS.get(last.label, "unknown")
            seen, conf = _utc_z(last.taken_at), last.confidence
        out.append({"id": st.id, "name": st.name, "crop": crop, "map_x": st.map_x, "map_y": st.map_y,
                    "route_order": st.route_order, "status": status,
                    "age_days": stats.age_days(batch, day) if batch else None, "last_seen_at": seen, "confidence": conf})
    return {"stations": out, "updated_at": _now_iso(), "demo": _demo(session)}


@router.get("/robot")
def robot_status(session: Session = Depends(get_session)):
    return robot.robot.get_robot_status(session)


@router.get("/robot/frame")
def robot_frame():
    """Latest relayed camera frame (JPEG), 204 when there is none or it is older than 60 s. Same per-IP limiter."""
    got = robot.robot.get_frame()
    if got is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    data, age = got
    return Response(data, media_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Age": str(age)})


# --- inspection summaries (text + numbers only: no image paths, notes or rfid_uid) -----------------------

def _summary_out(row: Summary) -> dict:
    return {"text": row.text, "source": row.source, "updated_at": _utc_z(row.updated_at)}


@router.get("/rounds")
def rounds(limit: int = Query(20, ge=1, le=50), session: Session = Depends(get_session)):
    rows = session.scalars(select(Round).order_by(Round.started_at.desc(), Round.id.desc()).limit(limit)).all()
    ids = [r.id for r in rows]
    n_stations = dict(session.execute(
        select(Capture.round_id, func.count(func.distinct(Capture.station_id))).join(Station, Capture.station_id == Station.id)
        .where(Capture.round_id.in_(ids), *VISIBLE).group_by(Capture.round_id)).all())
    heads = dict(session.execute(select(Summary.round_id, Summary.text).where(Summary.round_id.in_(ids), Summary.station_id.is_(None))).all())
    return {"rounds": [
        {"id": r.id, "started_at": _utc_z(r.started_at), "status": r.status, "stations": n_stations.get(r.id, 0),
         "headline": summaries.headline(heads[r.id]) if r.id in heads else None} for r in rows],
        "demo": _demo(session)}


@router.get("/rounds/{round_id}")
def round_detail(round_id: Annotated[int, Path(ge=1, le=INT_MAX)], session: Session = Depends(get_session)):
    rnd = session.get(Round, round_id)
    if rnd is None:
        raise HTTPException(404, "unknown round")
    rows = {s.station_id: s for s in session.scalars(select(Summary).where(Summary.round_id == round_id))}
    stations = session.execute(
        select(Station.id, Station.name).where(*VISIBLE, Station.id.in_(select(Capture.station_id).where(Capture.round_id == round_id)))
        .order_by(Station.route_order, Station.id)).all()
    out = []
    for sid, name in stations:
        row = rows.get(sid)
        out.append({"station_id": sid, "name": name, "summary": _summary_out(row) if row else None,
                    "facts": row.facts if row else None})
    whole = rows.get(None)
    return {"id": rnd.id, "started_at": _utc_z(rnd.started_at), "status": rnd.status,
            "summary": _summary_out(whole) if whole else None, "facts": whole.facts if whole else None,
            "stations": out, "demo": _demo(session)}


@router.get("/summaries/latest")
def latest_summary(session: Session = Depends(get_session)):
    row = session.execute(
        select(Summary, Station.name).outerjoin(Station, Summary.station_id == Station.id)
        .where(or_(Summary.station_id.is_(None), and_(*VISIBLE)))
        .order_by(Summary.created_at.desc(), Summary.id.desc()).limit(1)).first()
    if row is None:
        return {"summary": None}
    s, station = row
    return {"summary": {"round_id": s.round_id, "scope": s.scope, "station": station, "headline": summaries.headline(s.text),
                        **_summary_out(s)}}


# --- robot screen: one small read for /screen (robot status + this round's station results + the round result) ----

def _screen_station(row: Summary) -> dict:
    """Findings only: ใบเหลือง / จุดน้ำตาล with their angles, never a count or a label for ปกติ."""
    f = row.facts
    where = summaries._affected(f["angles"])
    prev = f["previous_visit"]
    return {"id": row.id, "level": summaries.display(f)["level"], "present": len(f["present"]),
            "yellow": f["counts"][stats.YELLOW], "yellow_angles": where[stats.YELLOW],
            "brown": f["counts"][stats.BROWN], "brown_angles": where[stats.BROWN],
            "abnormal_rate": f["abnormal_rate"], "vs_baseline_pp": f["baseline"]["delta_pp"],
            "vs_previous_pp": prev["delta_pp"] if prev else None,
            "trend": f["trend"]["direction"], "trend_label": f["trend"]["label"], **_summary_out(row)}


def _screen_round(row: Summary) -> dict:
    f = row.facts
    worst = f["worst"]
    return {"id": row.id, "level": summaries.display(f)["level"],
            "stations_inspected": f["stations_inspected"], "stations_expected": f["stations_expected"],
            "yellow": f["counts"][stats.YELLOW], "brown": f["counts"][stats.BROWN],
            "abnormal_rate": f["abnormal_rate"], "vs_baseline_pp": f["baseline"]["delta_pp"],
            "worst": {"station": worst["station"], "abnormal_rate": worst["abnormal_rate"]} if worst else None,
            "rising": f["rising"], **_summary_out(row)}


@router.get("/screen")
def screen(response: Response, session: Session = Depends(get_session)):
    status = robot.robot.get_robot_status(session)
    # the robot's round while it is reporting one, else the newest round
    round_id = status["round_id"] or session.scalar(select(Round.id).order_by(Round.started_at.desc(), Round.id.desc()).limit(1))
    stations, whole, latest = [], None, None
    if round_id is not None:
        rows = {s.station_id: s for s in session.scalars(select(Summary).where(Summary.round_id == round_id))}
        for sid, name, order in session.execute(select(Station.id, Station.name, Station.route_order).where(*VISIBLE).order_by(Station.route_order, Station.id)):
            stations.append({"station_id": sid, "name": name, "order": order, "summary": _screen_station(rows[sid]) if sid in rows else None})
        shown = {x["station_id"] for x in stations}
        done = [r for sid, r in rows.items() if sid in shown]
        latest = max(done, key=lambda r: (r.created_at, r.id)).station_id if done else None
        whole = _screen_round(rows[None]) if None in rows else None
    response.headers["Cache-Control"] = "public, max-age=2"  # live view polled every 4 s: the default 30 s would show old data
    return {"updated_at": _now_iso(), "demo": _demo(session), "robot": status, "round_id": round_id,
            "stations": stations, "latest": latest, "round": whole}
