"""100-Day Season Demo Seed for NEXXA.

Generates deterministic synthetic demo data across multiple days (default 100 days)
simulating inspection rounds, camera captures from 3 angles, quality retakes,
and crop-age-dependent leaf disease diagnosis.
Also provides a clean purge utility to remove demo records.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import random
import sys

_server_dir = str(Path(__file__).resolve().parents[1])
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from sqlalchemy import delete, func, not_, select
from sqlalchemy.orm import Session

from app import stats
from app.models import AppSetting, Batch, Capture, Crop, DailyStat, Round, Station, StationAnalysis, Summary

ANGLES = ["top", "left", "right"]
PLANTS_PER_STATION = (3, 9)  # demo: a station holds this many plants (fixed per station); the simulated AI counts the diseased ones
DEMO_PREFIX = "demo://"
DEMO_IMAGE_TPL = "demo://synthetic/{station_id}/{day}/{angle}.jpg"
DEMO_IMAGE_ATTEMPT_TPL = "demo://synthetic/{station_id}/{day}/{angle}-attempt{attempt}.jpg"
DEMO_TAG = "demo"

# NOTE: The disease, defect, and symptom distributions defined below are purely synthetic
# heuristic distributions created for user interface demonstration and testing purposes.
# They DO NOT represent real AI model accuracy, real sensor precision, or real greenhouse prevalence rates.
SYMPTOM_WEIGHTS = {
    # Younger crops (age < 15): mostly normal (~90%), low yellowing (~5%), low tipburn (~2%), some review (~3%)
    "young": {"normal": 0.90, "yellow": 0.05, "brown": 0.02, "review": 0.03},
    # Mid crops (age 15-30): normal (~80%), yellowing (~10%), brown (~5%), review (~5%)
    "mid": {"normal": 0.80, "yellow": 0.10, "brown": 0.05, "review": 0.05},
    # Near-harvest crops (age > 30): tipburn/brown (~18%), yellowing (~15%), review (~5%), normal (~62%)
    "near_harvest": {"normal": 0.62, "yellow": 0.15, "brown": 0.18, "review": 0.05},
}


def _simulate_diagnosis(rng: random.Random, age_days: int) -> tuple[str, float, bool, str | None]:
    """Simulate diagnosis label, confidence, needs_review, and note based on crop age.

    These are synthetic distributions for demo purposes, not real AI model accuracy.
    """
    if age_days < 15:
        weights = SYMPTOM_WEIGHTS["young"]
    elif age_days <= 30:
        weights = SYMPTOM_WEIGHTS["mid"]
    else:
        weights = SYMPTOM_WEIGHTS["near_harvest"]

    categories = list(weights.keys())
    probs = [weights[c] for c in categories]
    choice = rng.choices(categories, weights=probs, k=1)[0]

    if choice == "normal":
        label = "ปกติ"
        confidence = round(rng.uniform(0.90, 0.99), 3)
        needs_review = False
        note = None
    elif choice == "yellow":
        label = "ใบเหลือง"
        confidence = round(rng.uniform(0.82, 0.95), 3)
        needs_review = False
        note = "ใบแก่ด้านนอกเริ่มเหลือง"
    elif choice == "brown":
        label = "จุดน้ำตาล"
        confidence = round(rng.uniform(0.80, 0.94), 3)
        needs_review = False
        note = "น่าจะ tipburn"
    else:  # "review"
        label = rng.choice(["ปกติ", "ใบเหลือง", "จุดน้ำตาล"])
        confidence = round(rng.uniform(0.40, 0.65), 3)
        needs_review = True
        note = "AI ความมั่นใจต่ำ ต้องตรวจทาน"

    return label, confidence, needs_review, note


def _ensure_crop(session: Session) -> Crop:
    crop = session.scalar(select(Crop).where(Crop.name == "กรีนคอส"))
    if crop is None:
        crop = Crop(
            name="กรีนคอส",
            labels=["ปกติ", "ใบเหลือง", "จุดน้ำตาล"],
            prompt="ดูภาพถ่ายใบผักสลัดกรีนคอส (green cos) แล้วเลือกป้ายกำกับเพียงหนึ่งอย่างจากรายการของพืชนี้ ได้แก่ ปกติ, ใบเหลือง, จุดน้ำตาล.",
            notes="ขอบใบสีน้ำตาลที่ใบอ่อน/ใบด้านใน โดยไม่มีแผลลุกลาม น่าจะ tipburn ให้ตอบ จุดน้ำตาล พร้อมหมายเหตุว่า \"น่าจะ tipburn\" ส่วนใบแก่ด้านนอกที่เหลืองตามธรรมชาติให้ตอบ ปกติ",
        )
        session.add(crop)
        session.flush()
    return crop


def _ensure_stations(session: Session, crop: Crop) -> list[Station]:
    """Find or create 3 inspection stations + 1 home station."""
    stations = session.scalars(
        select(Station).where(Station.kind == "station").order_by(Station.route_order, Station.id)
    ).all()

    if len(stations) < 3:
        # Create missing demo stations
        needed = 3 - len(stations)
        existing_count = len(stations)
        for i in range(needed):
            idx = existing_count + i + 1
            st = Station(
                rfid_uid=f"DEMO-RFID-S{idx}",
                name=f"สถานี {idx}",
                description=DEMO_TAG,
                kind="station",
                route_order=idx,
                map_x=float(idx),
                map_y=0.0,
                crop_id=crop.id,
                active=True,
            )
            session.add(st)
        session.flush()
        stations = session.scalars(
            select(Station).where(Station.kind == "station").order_by(Station.route_order, Station.id)
        ).all()

    # Ensure Home station exists
    home = session.scalar(select(Station).where(Station.kind == "home"))
    if home is None:
        home = Station(
            rfid_uid="DEMO-RFID-HOME",
            name="จุดเริ่มต้น",
            description=DEMO_TAG,
            kind="home",
            route_order=0,
            map_x=0.0,
            map_y=0.0,
            crop_id=None,
            active=True,
        )
        session.add(home)
        session.flush()

    for s in stations[:3]:
        if s.crop_id is None:
            s.crop_id = crop.id
    session.flush()

    return list(stations[:3])


def _setup_batches(
    session: Session,
    stations: list[Station],
    crop: Crop,
    start_day: date,
    today: date,
) -> list[Batch]:
    """Create staggered batches for each station so that on `today` stations have crops of various ages."""
    created_batches: list[Batch] = []
    # Target ages on `today`: station 0: young (12d), station 1: mid (25d), station 2: near-harvest (40d)
    target_ages = [12, 25, 40]
    cycle_length = 41

    for s_idx, station in enumerate(stations):
        cur_age = target_ages[s_idx % len(target_ages)]
        cur_planted = today - timedelta(days=cur_age)

        # Currently growing batch
        cur_batch = Batch(
            station_id=station.id,
            crop_id=crop.id,
            planted_on=cur_planted,
            harvested_on=None,
            notes=DEMO_TAG,
        )
        session.add(cur_batch)
        created_batches.append(cur_batch)

        # Preceding batches covering history back past start_day
        prev_end = cur_planted - timedelta(days=1)
        while prev_end >= start_day:
            prev_start = prev_end - timedelta(days=cycle_length)
            b = Batch(
                station_id=station.id,
                crop_id=crop.id,
                planted_on=prev_start,
                harvested_on=prev_end,
                notes=DEMO_TAG,
            )
            session.add(b)
            created_batches.append(b)
            prev_end = prev_start - timedelta(days=1)

    session.flush()
    return created_batches


def run(
    session: Session,
    days: int = 100,
    seed: int = 1,
    force: bool = False,
    today: date | None = None,
) -> dict:
    """Run demo seed generating `days` of inspection data.

    Returns summary dictionary with counts of created rows.
    """
    if days < 1:
        raise ValueError("days must be at least 1")

    # Safety check: real captures must not be overwritten unless force=True
    has_real_captures = session.scalar(
        select(Capture.id).where(not_(Capture.image_path.startswith(DEMO_PREFIX))).limit(1)
    ) is not None

    if has_real_captures and not force:
        raise ValueError("database contains real captures; pass force=True to proceed")

    # Clear prior demo data so repeat with same seed produces identical results
    clear(session)

    if today is None:
        today = stats.today()
    start_day = today - timedelta(days=days - 1)

    rng = random.Random(seed)

    crop = _ensure_crop(session)
    stations = _ensure_stations(session, crop)
    created_batches = _setup_batches(session, stations, crop, start_day, today)

    # Collect batches per station to compute active batch and crop age
    batches_by_station: dict[int, list[Batch]] = {}
    for b in created_batches:
        batches_by_station.setdefault(b.station_id, []).append(b)

    plants = {st.id: rng.randint(*PLANTS_PER_STATION) for st in stations}  # same plants every day, like a real bed

    n_rounds = 0
    n_captures = 0
    n_analyses = 0

    # Inspection time during day (08:00 UTC)
    base_time = time(8, 0, 0)

    for day_idx in range(days):
        cur_day = start_day + timedelta(days=day_idx)
        round_dt = datetime.combine(cur_day, base_time, tzinfo=timezone.utc)

        rnd = Round(started_at=round_dt, status="demo")
        session.add(rnd)
        session.flush()
        n_rounds += 1

        sec_offset = 0

        for station in stations:
            active_b = stats.active_batch(batches_by_station.get(station.id, []), cur_day)
            crop_age = stats.age_days(active_b, cur_day) if active_b else 20

            for angle in ANGLES:
                sec_offset += 15
                cap_dt = round_dt + timedelta(seconds=sec_offset)

                # ~10% quality failure (blurry or dark)
                quality_fail = rng.random() < 0.10

                if quality_fail:
                    # Attempt 1: quality failed
                    reason = rng.choice(["blurry", "dark"])
                    path1 = DEMO_IMAGE_ATTEMPT_TPL.format(
                        station_id=station.id, day=cur_day.isoformat(), angle=angle, attempt=1
                    )
                    c1 = Capture(
                        round_id=rnd.id,
                        station_id=station.id,
                        camera_angle=angle,
                        attempt=1,
                        image_path=path1,
                        taken_at=cap_dt,
                        quality_ok=False,
                        quality_reason=reason,
                        crop_id=crop.id,
                    )
                    session.add(c1)
                    n_captures += 1

                    # Attempt 2: retake succeeds
                    sec_offset += 5
                    cap_dt2 = round_dt + timedelta(seconds=sec_offset)
                    path2 = DEMO_IMAGE_TPL.format(
                        station_id=station.id, day=cur_day.isoformat(), angle=angle
                    )
                    c2 = Capture(
                        round_id=rnd.id,
                        station_id=station.id,
                        camera_angle=angle,
                        attempt=2,
                        image_path=path2,
                        taken_at=cap_dt2,
                        quality_ok=True,
                        quality_reason=None,
                        crop_id=crop.id,
                    )
                    session.add(c2)
                    session.flush()
                    n_captures += 1

                else:
                    # Single successful capture
                    path = DEMO_IMAGE_TPL.format(
                        station_id=station.id, day=cur_day.isoformat(), angle=angle
                    )
                    c = Capture(
                        round_id=rnd.id,
                        station_id=station.id,
                        camera_angle=angle,
                        attempt=1,
                        image_path=path,
                        taken_at=cap_dt,
                        quality_ok=True,
                        quality_reason=None,
                        crop_id=crop.id,
                    )
                    session.add(c)
                    session.flush()
                    n_captures += 1

            # one analysis per station visit: plants seen, and how many of them the simulated AI marks yellow / brown
            diag = [_simulate_diagnosis(rng, crop_age) for _ in range(plants[station.id])]
            session.add(StationAnalysis(
                round_id=rnd.id, station_id=station.id, n_photos=len(ANGLES), n_total=plants[station.id],
                n_yellow=sum(d[0] == "ใบเหลือง" for d in diag), n_brown=sum(d[0] == "จุดน้ำตาล" for d in diag),
                confidence=round(sum(d[1] for d in diag) / len(diag), 2), note=None, model="synthetic-demo-v1",
                needs_review=any(d[2] for d in diag), created_at=round_dt + timedelta(seconds=sec_offset + 10)))
            n_analyses += 1
        session.flush()

        # Populate daily_stats for this day
        stats.rollup(session, cur_day)

    # Set demo setting flag
    setting = session.get(AppSetting, "demo")
    if setting is None:
        session.add(AppSetting(key="demo", value="1"))
    else:
        setting.value = "1"
    session.commit()

    total_stats = session.scalar(
        select(func.count(DailyStat.id)).where(DailyStat.day >= start_day, DailyStat.day <= today)
    ) or 0

    return {
        "days": days,
        "rounds": n_rounds,
        "captures": n_captures,
        "analyses": n_analyses,
        "batches": len(created_batches),
        "daily_stats": int(total_stats),
    }


def clear(session: Session) -> dict:
    """Delete all demo data and reset demo setting flag."""
    # Find demo captures
    demo_cap_ids = session.scalars(
        select(Capture.id).where(Capture.image_path.startswith(DEMO_PREFIX))
    ).all()

    if demo_cap_ids:
        session.execute(delete(Capture).where(Capture.id.in_(demo_cap_ids)))

    # Find demo rounds
    demo_round_ids = session.scalars(select(Round.id).where(Round.status == "demo")).all()
    n_analyses = 0
    if demo_round_ids:
        session.execute(delete(Summary).where(Summary.round_id.in_(demo_round_ids)))
        n_analyses = session.execute(delete(StationAnalysis).where(StationAnalysis.round_id.in_(demo_round_ids))).rowcount
        session.execute(delete(Round).where(Round.id.in_(demo_round_ids)))

    # Find demo batches
    demo_batches = session.scalars(select(Batch.id).where(Batch.notes == DEMO_TAG)).all()

    # Delete daily_stats associated with demo batches or demo stations
    demo_stations = session.scalars(select(Station.id).where(Station.description == DEMO_TAG)).all()

    del_stats_conds = []
    if demo_batches:
        del_stats_conds.append(DailyStat.batch_id.in_(demo_batches))
    if demo_stations:
        del_stats_conds.append(DailyStat.station_id.in_(demo_stations))

    n_del_stats = 0
    if del_stats_conds:
        # Delete daily stats matching demo batches or stations
        cond = del_stats_conds[0] if len(del_stats_conds) == 1 else (del_stats_conds[0] | del_stats_conds[1])
        st_ids = session.scalars(select(DailyStat.id).where(cond)).all()
        n_del_stats = len(st_ids)
        if st_ids:
            session.execute(delete(DailyStat).where(DailyStat.id.in_(st_ids)))

    if demo_batches:
        session.execute(delete(Batch).where(Batch.id.in_(demo_batches)))

    if demo_stations:
        session.execute(delete(Station).where(Station.id.in_(demo_stations)))

    # Reset demo setting flag
    setting = session.get(AppSetting, "demo")
    if setting is not None:
        setting.value = "0"

    session.commit()

    # If any real captures remain, recompute rollup for real data
    real_caps_exist = session.scalar(select(Capture.id).limit(1)) is not None
    if real_caps_exist:
        try:
            stats.rollup(session)
        except Exception:
            session.rollback()

    return {
        "captures": len(demo_cap_ids),
        "analyses": n_analyses,
        "rounds": len(demo_round_ids),
        "batches": len(demo_batches),
        "daily_stats": n_del_stats,
        "stations": len(demo_stations),
    }


if __name__ == "__main__":
    from pathlib import Path
    _server_dir = str(Path(__file__).resolve().parents[1])
    if _server_dir not in sys.path:
        sys.path.insert(0, _server_dir)

    parser = argparse.ArgumentParser(description="100-Day Season Demo Seed for NEXXA")
    parser.add_argument("--days", type=int, default=100, help="Number of demo days (default: 100)")
    parser.add_argument("--seed", type=int, default=1, help="Random seed (default: 1)")
    parser.add_argument("--force", action="store_true", help="Allow seeding even if real captures exist")
    parser.add_argument("--clear", action="store_true", help="Clear demo data")
    args = parser.parse_args()

    from app.db import engine, init_db

    init_db(engine)
    with Session(engine) as s:
        if args.clear:
            res = clear(s)
            print(f"Demo data cleared: {res}")
        else:
            try:
                res = run(s, days=args.days, seed=args.seed, force=args.force)
                print(f"Demo data seeded: {res}")
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                sys.exit(1)
