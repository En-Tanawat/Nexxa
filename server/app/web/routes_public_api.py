"""Public, read-only, login-free JSON API: aggregate numbers, statuses, station/crop names, route order, map positions.
Never add image paths, notes, rfid_uid, keys or user data here.

Rate limit is in memory per client IP (request.client). Behind a reverse proxy run uvicorn with --proxy-headers
(and --forwarded-allow-ips) or every visitor shares the proxy's IP.
"""
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import stats
from app.db import get_session
from app.models import Analysis, AppSetting, Batch, Capture, Crop, DailyStat, Station

RATE_LIMIT = 60  # requests per WINDOW_S per IP
WINDOW_S = 60
MAX_TRACKED_IPS = 10_000
HARVEST_AGE_DAYS = 45  # shortcut: one cycle length for every crop (not in the data model); move to Crop when it differs
NEAR_HARVEST_DAYS = 7
WEEKLY_OVER = 100  # a series with more points than this is bucketed to weeks (Monday start)
MAX_SAME_AGE_DAYS, MAX_SAME_AGE_BATCHES = 120, 6
REVIEW_WINDOW_DAYS = 7  # no "resolved" state exists yet, so "pending" = needs_review within the last week
INT_MAX = 2**31 - 1

_clock = time.monotonic
_hits: dict[str, list] = {}  # ip -> [window_start, count]; shortcut: single process, use a shared store if scaled out


def allow(ip: str) -> bool:
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
    response.headers["Cache-Control"] = "public, max-age=30"


router = APIRouter(prefix="/api/public", dependencies=[Depends(limit_and_cache)])


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
        .join(Crop, Batch.crop_id == Crop.id).where(Batch.harvested_on.is_(None), Batch.planted_on <= day))
    lots = []
    for b, station, crop in open_batches:
        age = stats.age_days(b, day)
        if HARVEST_AGE_DAYS - age <= NEAR_HARVEST_DAYS:
            lots.append({"station": station, "crop": crop, "age_days": age, "harvest_in_days": max(0, HARVEST_AGE_DAYS - age)})
    lots.sort(key=lambda x: (x["harvest_in_days"], x["station"]))
    return {
        "updated_at": _now_iso(),
        "demo": _demo(session),
        "stations_active": session.scalar(select(func.count(Station.id)).where(Station.kind == "station", Station.active.is_(True))),
        "captures_today": n_cap,
        "abnormal_rate_today": _rate(yellow + brown, normal + yellow + brown),
        "review_pending": pending,
        "lots_near_harvest": lots,
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
        .where(*where).group_by(DailyStat.station_id, DailyStat.day).order_by(DailyStat.station_id, DailyStat.day)
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
        select(DailyStat.batch_id).join(Batch, DailyStat.batch_id == Batch.id).where(*where)
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


@router.get("/twin")
def twin(session: Session = Depends(get_session)):
    day = stats.today()
    stations = session.execute(
        select(Station, Crop.name).outerjoin(Crop, Station.crop_id == Crop.id)
        .where(Station.kind == "station", Station.active.is_(True)).order_by(Station.route_order, Station.id)).all()
    batches = session.scalars(select(Batch).where(Batch.planted_on <= day)).all()
    out = []
    for st, crop in stations:
        # shortcut: one query per station (a handful of stations); fold into a window query if it grows to hundreds
        last = session.execute(
            select(Analysis.label, Analysis.confidence, Analysis.needs_review, Capture.taken_at)
            .join(Capture, Analysis.capture_id == Capture.id).where(Capture.station_id == st.id)
            .order_by(Capture.taken_at.desc(), Analysis.id.desc()).limit(1)).first()
        batch = stats.active_batch([b for b in batches if b.station_id == st.id], day)
        status, seen, conf = "unknown", None, None
        if last:
            label, conf, review, taken = last
            status = "review" if review else _STATUS.get(label, "unknown")
            seen = taken.isoformat()
        out.append({"id": st.id, "name": st.name, "crop": crop, "map_x": st.map_x, "map_y": st.map_y,
                    "route_order": st.route_order, "status": status,
                    "age_days": stats.age_days(batch, day) if batch else None, "last_seen_at": seen, "confidence": conf})
    return {"stations": out, "updated_at": _now_iso(), "demo": _demo(session)}
