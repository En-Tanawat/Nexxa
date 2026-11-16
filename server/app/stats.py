"""Daily per-station rollup of captures/analyses into daily_stats.

Day boundaries are UTC (captures.taken_at is stored as UTC).
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Date, case, func, select
from sqlalchemy.orm import Session

from app.models import Analysis, Batch, Capture, DailyStat

NORMAL, YELLOW, BROWN = "ปกติ", "ใบเหลือง", "จุดน้ำตาล"


def today() -> date:
    return datetime.now(timezone.utc).date()


def age_days(batch: Batch, on: date) -> int:
    """Days since planting; negative before planting (such a day is never attributed to the batch)."""
    return (on - batch.planted_on).days


def active_batch(batches: list[Batch], day: date) -> Batch | None:
    """The batch covering `day`: planted_on <= day <= harvested_on (or not harvested).
    If several overlap, the latest planted_on wins, then the highest id (deterministic)."""
    cover = [b for b in batches if b.planted_on <= day and (b.harvested_on is None or day <= b.harvested_on)]
    return max(cover, key=lambda b: (b.planted_on, b.id), default=None)


def _count(cond):
    return func.sum(case((cond, 1), else_=0))


def _day_rows(session: Session, day: date, station_id: int | None) -> int:
    start = datetime.combine(day, time.min)
    in_day = [Capture.taken_at >= start, Capture.taken_at < start + timedelta(days=1)]
    if station_id is not None:
        in_day.append(Capture.station_id == station_id)

    totals = session.execute(
        select(Capture.station_id, func.count(Capture.id), _count(Capture.quality_ok.is_(True)))
        .where(*in_day).group_by(Capture.station_id)
    ).all()
    if not totals:
        return 0
    # Only the newest analysis of a capture is final; unknown labels land in no symptom column.
    final = Analysis.id == select(func.max(Analysis.id)).where(Analysis.capture_id == Capture.id).correlate(Capture).scalar_subquery()
    symptoms = {
        r[0]: r[1:] for r in session.execute(
            select(Capture.station_id, _count(Analysis.label == NORMAL), _count(Analysis.label == YELLOW),
                   _count(Analysis.label == BROWN), _count(Analysis.needs_review.is_(True)), func.avg(Analysis.confidence))
            .join(Capture, Analysis.capture_id == Capture.id).where(*in_day, final).group_by(Capture.station_id)
        )
    }
    batches = session.scalars(
        select(Batch).where(Batch.station_id.in_([t[0] for t in totals]), Batch.planted_on <= day)
    ).all()

    for sid, n, n_ok in totals:
        batch = active_batch([b for b in batches if b.station_id == sid], day)
        bid = batch.id if batch else None
        normal, yellow, brown, review, conf = symptoms.get(sid, (0, 0, 0, 0, None))
        # One row per (day, station): look up by those two only. UNIQUE(day, station_id, batch_id) cannot
        # catch batch_id IS NULL duplicates, and this also replaces a row whose batch changed since last run.
        existing = session.scalars(
            select(DailyStat).where(DailyStat.day == day, DailyStat.station_id == sid).order_by(DailyStat.id)
        ).all()
        row = existing[0] if existing else DailyStat(day=day, station_id=sid)
        for extra in existing[1:]:
            session.delete(extra)
        row.batch_id = bid
        row.n_captures, row.n_quality_ok = int(n), int(n_ok or 0)
        row.n_normal, row.n_yellow, row.n_brown, row.n_review = (int(x or 0) for x in (normal, yellow, brown, review))
        row.mean_confidence = conf
        session.add(row)
    session.commit()
    return len(totals)


def rollup(session: Session, day: date | None = None, station_id: int | None = None) -> int:
    """Upsert daily_stats rows; returns rows written. Idempotent.

    day given: recompute that day (optionally one station). day None: recompute today plus every day that has
    a (day, station) with captures but no row yet. Days without captures get no row.
    """
    if day is not None:
        return _day_rows(session, day, station_id)
    captured = {tuple(r) for r in session.execute(
        select(func.date(Capture.taken_at, type_=Date), Capture.station_id).group_by(func.date(Capture.taken_at), Capture.station_id)
    )}
    done = {tuple(r) for r in session.execute(select(DailyStat.day, DailyStat.station_id))}
    days = {d for d, _ in captured - done} | {today()}
    return sum(_day_rows(session, d, None) for d in sorted(days))
