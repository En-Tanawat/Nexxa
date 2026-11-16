"""Public, read-only, login-free JSON API: aggregate numbers, statuses, station/crop names, route order, map positions.
Never add image paths, notes, rfid_uid, keys or user data here.

Rate limit is in memory per client IP (request.client). Behind a reverse proxy run uvicorn with --proxy-headers
(and --forwarded-allow-ips) or every visitor shares the proxy's IP.
"""
import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

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

def _screen_station(row: Summary, by_day: dict, day) -> dict:
    """Findings only: ใบเหลือง / จุดน้ำตาล with their angles, never a count or a label for ปกติ."""
    f = row.facts
    where = summaries._affected(f["angles"])
    prev = f["previous_visit"]
    yellow, brown = f["counts"][stats.YELLOW], f["counts"][stats.BROWN]
    last = None if prev is None or prev["abnormal_rate"] is None else {
        "round_id": prev["round_id"], "yellow": prev["counts"][stats.YELLOW], "brown": prev["counts"][stats.BROWN]}
    return {"id": row.id, "level": summaries.display(f)["level"], "present": len(f["present"]),
            "yellow": yellow, "yellow_angles": where[stats.YELLOW],
            "brown": brown, "brown_angles": where[stats.BROWN],
            "abnormal_rate": f["abnormal_rate"], "vs_baseline_pp": f["baseline"]["delta_pp"],
            "vs_previous_pp": prev["delta_pp"] if prev else None,
            "trend": f["trend"]["direction"], "trend_label": f["trend"]["label"],
            "compare": summaries.screen_compare(by_day, day, last), **_summary_out(row)}


def _screen_round(row: Summary, by_day: dict, day, last: dict | None) -> dict:
    f = row.facts
    worst = f["worst"]
    yellow, brown = f["counts"][stats.YELLOW], f["counts"][stats.BROWN]
    trend = summaries.round_trend(f)
    return {"id": row.id, "level": summaries.display(f)["level"],
            "stations_inspected": f["stations_inspected"], "stations_expected": f["stations_expected"],
            "yellow": yellow, "brown": brown,
            "abnormal_rate": f["abnormal_rate"], "vs_baseline_pp": f["baseline"]["delta_pp"],
            "worst": {"station": worst["station"], "abnormal_rate": worst["abnormal_rate"]} if worst else None,
            "rising": f["rising"], "trend": trend, "trend_label": summaries.TREND_LABELS[trend],
            "compare": summaries.screen_compare(by_day, day, last), **_summary_out(row)}


def _previous_round_counts(session: Session, round_id: int) -> dict | None:
    """Yellow / brown totals of the round before this one (all its stations), or None when there is none or nothing was analysed."""
    prev = session.scalar(select(func.max(Capture.round_id)).where(Capture.round_id < round_id))
    if prev is None:
        return None
    total = {stats.NORMAL: 0, stats.YELLOW: 0, stats.BROWN: 0}
    for sid in summaries.round_station_ids(session, prev):
        for k, v in summaries._counts(summaries._angle_results(session, prev, sid)[0]).items():
            total[k] += v
    if not sum(total.values()):
        return None
    return {"round_id": prev, "yellow": total[stats.YELLOW], "brown": total[stats.BROWN]}


@router.get("/screen")
def screen(response: Response, session: Session = Depends(get_session)):
    status = robot.robot.get_robot_status(session)
    # the robot's round while it is reporting one, else the newest round
    round_id = status["round_id"] or session.scalar(select(Round.id).order_by(Round.started_at.desc(), Round.id.desc()).limit(1))
    stations, whole, latest = [], None, None
    if round_id is not None:
        rows = {s.station_id: s for s in session.scalars(select(Summary).where(Summary.round_id == round_id))}
        day = summaries._utc_day(session.get(Round, round_id).started_at)
        daily: dict[int, dict] = {}  # station id -> {day: (normal, yellow, brown)} for the days before the round's day (server side only: yesterday and 7-day average)
        for sid, d, n, y, b in session.execute(
                select(DailyStat.station_id, DailyStat.day, func.sum(DailyStat.n_normal), func.sum(DailyStat.n_yellow), func.sum(DailyStat.n_brown))
                .where(DailyStat.day >= day - timedelta(days=summaries.COMPARE_DAYS - 1), DailyStat.day < day)
                .group_by(DailyStat.station_id, DailyStat.day)):
            daily.setdefault(sid, {})[d] = (int(n or 0), int(y or 0), int(b or 0))
        for sid, name, order in session.execute(select(Station.id, Station.name, Station.route_order).where(*VISIBLE).order_by(Station.route_order, Station.id)):
            stations.append({"station_id": sid, "name": name, "order": order,
                             "summary": _screen_station(rows[sid], daily.get(sid, {}), day) if sid in rows else None})
        shown = {x["station_id"] for x in stations}
        done = [r for sid, r in rows.items() if sid in shown]
        latest = max(done, key=lambda r: (r.created_at, r.id)).station_id if done else None
        if None in rows:
            total: dict = {}
            for x in rows[None].facts["stations"]:
                for d, vals in daily.get(x["station_id"], {}).items():
                    total[d] = tuple(a + b for a, b in zip(total.get(d, (0, 0, 0)), vals))
            whole = _screen_round(rows[None], total, day, _previous_round_counts(session, round_id))
    response.headers["Cache-Control"] = "public, max-age=2"  # live view polled every 4 s: the default 30 s would show old data
    return {"updated_at": _now_iso(), "demo": _demo(session), "robot": status, "round_id": round_id,
            "stations": stations, "latest": latest, "round": whole}


# --- robot screen: the lot's history (columnar and small; the 4 s poll above never carries it) -------------

HIST_FALLBACK_DAYS = 30
HIST_MAX_DAYS = 366
HIST_BUCKET_OVER = 90  # a longer span is summed per 7 days so the payload stays tiny


def _hist_span(session: Session, station_id: int | None, today: date) -> tuple[date, date, Batch | None] | None:
    """(first day, last day, batch) of "the lot": planting .. harvest (or today). None when there is no lot to follow."""
    q = select(Batch).where(Batch.planted_on <= today)
    q = q.where(Batch.station_id == station_id) if station_id is not None else q.join(Station, Batch.station_id == Station.id).where(*VISIBLE)
    batches = session.scalars(q).all()
    if station_id is not None:
        # the batch growing now; else the most recent one (harvested): its history ends on its harvest day
        b = stats.active_batch(list(batches), today) or max(batches, key=lambda x: (x.planted_on, x.id), default=None)
        return (b.planted_on, b.harvested_on or today, b) if b and (b.harvested_on or today) >= b.planted_on else None
    growing = [b for b in batches if b.harvested_on is None]
    return (min(b.planted_on for b in growing), today, None) if growing else None  # all stations: the oldest lot still growing


@router.get("/screen/history")
def screen_history(
    request: Request,
    station_id: int | None = Query(None, ge=1, le=INT_MAX),
    rng: str = Query("lot", alias="range", pattern="^(lot|30|14|7)$"),
    session: Session = Depends(get_session),
):
    """Yellow / brown counts per day for one station (or all), over the lot or the last 30/14/7 days. Columnar: arrays
    y and b with null for a day without data. More than 90 days: one value per 7 days (bucket 7). Never a normal count."""
    if station_id is not None and session.scalar(select(Station.id).where(Station.id == station_id, *VISIBLE)) is None:
        raise HTTPException(404, "unknown station")
    today = stats.today()
    span = _hist_span(session, station_id, today)
    batch = span[2] if span else None  # the lot is reported whatever the range, so the screen can always show its age
    end = span[1] if span else today
    if rng == "lot":
        start = span[0] if span else end - timedelta(days=HIST_FALLBACK_DAYS - 1)
    else:
        start = end - timedelta(days=int(rng) - 1)
    start = max(start, end - timedelta(days=HIST_MAX_DAYS - 1))
    q = select(DailyStat.day, func.sum(DailyStat.n_normal), func.sum(DailyStat.n_yellow), func.sum(DailyStat.n_brown))         .where(DailyStat.day >= start, DailyStat.day <= end).group_by(DailyStat.day)
    q = q.where(DailyStat.station_id == station_id) if station_id is not None else q.join(Station, DailyStat.station_id == Station.id).where(*VISIBLE)
    by_day = {d: (int(y or 0), int(b or 0)) for d, n, y, b in session.execute(q) if (n or 0) + (y or 0) + (b or 0) > 0}  # a day with no labelled result is null
    days = (end - start).days + 1
    bucket = 7 if days > HIST_BUCKET_OVER else 1
    ys, bs = [], []
    for i in range(0, days, bucket):
        got = [by_day[start + timedelta(days=k)] for k in range(i, min(i + bucket, days)) if start + timedelta(days=k) in by_day]
        ys.append(sum(g[0] for g in got) if got else None)
        bs.append(sum(g[1] for g in got) if got else None)
    body = {"start": start.isoformat(), "days": days, "bucket": bucket, "y": ys, "b": bs,
            "age0": (start - batch.planted_on).days if batch else None, "harvested": bool(batch and batch.harvested_on),
            "batch": {"planted_on": batch.planted_on.isoformat(), "harvested_on": batch.harvested_on.isoformat() if batch.harvested_on else None,
                      "cycle_days": HARVEST_AGE_DAYS} if batch else None}
    raw = json.dumps(body, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
    headers = {"Cache-Control": "public, max-age=60", "ETag": etag}
    if etag in [t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(raw, media_type="application/json", headers=headers)
