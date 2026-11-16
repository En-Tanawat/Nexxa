"""Per-station and per-round inspection summaries: facts from the database, deterministic Thai text, optional AI wording.

The template text (render_text) is always produced and is the fallback; ai_narrate only rewrites it from the same facts
(text only, no images) and returns None on any problem. Numbers come from the facts, never from the model.
Day boundaries are UTC, like stats.py.
"""
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import anthropic
import openai
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.db
from app import runtime_config, stats
from app.config import settings
from app.models import Analysis, Batch, Capture, DailyStat, Round, Station, Summary
from app.providers import get_provider

log = logging.getLogger(__name__)

ANGLES = ("front", "right", "back", "left")
ANGLE_TH = {"front": "หน้า", "right": "ขวา", "back": "หลัง", "left": "ซ้าย"}
BASELINE_DAYS, TREND_DAYS = 7, 14
TREND_MIN_DAYS = 3  # fewer days with data than this in the trend window: "ข้อมูลไม่พอ"
TREND_PP = 5.0  # first-7 vs last-7 change that counts as a trend (percentage points)
ALERT_RATE = 0.3  # abnormal rate from which the template advice says "check by hand"
AI_BUDGET_S = 25  # hard cap for the whole optional AI call; the template text is used after that
AI_MAX_CHARS = 1500
TREND_LABELS = {"increase": "เพิ่มขึ้น", "decrease": "ลดลง", "flat": "ทรงตัว", "insufficient": "ข้อมูลไม่พอ"}


# --- pure helpers -----------------------------------------------------------------------------

def _utc_day(dt: datetime) -> date:
    return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).date()


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def abnormal_rate(normal: int, yellow: int, brown: int) -> float | None:
    labelled = normal + yellow + brown
    return None if not labelled else (yellow + brown) / labelled


def _pp(a: float | None, b: float | None) -> float | None:
    """a - b in percentage points (1 decimal); None when either side is unknown."""
    return None if a is None or b is None else round((a - b) * 100, 1)


def history_facts(by_day: dict, day: date, current: float | None) -> tuple[dict, dict]:
    """(baseline, trend) from {day: (normal, yellow, brown)} of the days before `day`.

    baseline = the 7 days before `day` (not `day` itself), summed, not an average of daily rates.
    trend = first 7 vs last 7 of the 14 days before `day`.
    """
    def agg(lo: int, hi: int) -> tuple[int, int, int]:  # days day-lo .. day-hi-1 ... as offsets, lo > hi
        n = y = b = d = 0
        for k in range(lo, hi, -1):
            vals = by_day.get(day - timedelta(days=k))
            if vals and sum(vals) > 0:
                n, y, b, d = n + vals[0], y + vals[1], b + vals[2], d + 1
        return n, y, b, d

    n, y, b, d = agg(BASELINE_DAYS, 0)
    base_rate = abnormal_rate(n, y, b)
    baseline = {"days": d, "labelled": n + y + b, "abnormal": y + b, "rate": base_rate, "delta_pp": _pp(current, base_rate)}

    fn, fy, fb, fd = agg(TREND_DAYS, BASELINE_DAYS)
    first, last = abnormal_rate(fn, fy, fb), base_rate
    trend = {"direction": "insufficient", "days": fd + d, "first_rate": first, "last_rate": last, "delta_pp": _pp(last, first)}
    if fd + d >= TREND_MIN_DAYS and first is not None and last is not None:
        delta = (last - first) * 100
        trend["direction"] = "increase" if delta >= TREND_PP - 1e-9 else "decrease" if delta <= -TREND_PP + 1e-9 else "flat"
    trend["label"] = TREND_LABELS[trend["direction"]]
    return baseline, trend


def headline(text: str) -> str:
    """First sentence (or first line) of a summary text, for lists."""
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    return first if len(first) <= 200 else first[:199] + "…"


# --- facts ------------------------------------------------------------------------------------

def _angle_results(session: Session, round_id: int, station_id: int) -> tuple[dict, int, datetime | None]:
    """Per angle the latest usable capture (quality_ok not False) and its latest analysis; plus the quality-failure count."""
    caps = session.execute(
        select(Capture.id, Capture.camera_angle, Capture.quality_ok, Capture.taken_at)
        .where(Capture.round_id == round_id, Capture.station_id == station_id).order_by(Capture.id)).all()
    latest = {}  # capture_id -> newest analysis (ascending id, later rows overwrite)
    for a in session.scalars(select(Analysis).where(Analysis.capture_id.in_([c.id for c in caps])).order_by(Analysis.id)):
        latest[a.capture_id] = a
    usable = {c.camera_angle: c for c in caps if c.camera_angle in ANGLES and c.quality_ok is not False}
    angles = {}
    for ang in ANGLES:
        c = usable.get(ang)
        a = latest.get(c.id) if c else None
        angles[ang] = None if c is None else {
            "label": a.label if a else None, "confidence": a.confidence if a else None, "needs_review": bool(a and a.needs_review)}
    return angles, sum(1 for c in caps if c.quality_ok is False), max((c.taken_at for c in caps), default=None)


def _counts(angles: dict) -> dict:
    counts = {stats.NORMAL: 0, stats.YELLOW: 0, stats.BROWN: 0}
    for a in angles.values():
        if a and a["label"] in counts:
            counts[a["label"]] += 1
    return counts


def _rate_of(counts: dict) -> float | None:
    return abnormal_rate(counts[stats.NORMAL], counts[stats.YELLOW], counts[stats.BROWN])


def station_facts(session: Session, round_id: int, station_id: int) -> dict:
    station, rnd = session.get(Station, station_id), session.get(Round, round_id)
    day = _utc_day(rnd.started_at)
    angles, retakes, _ = _angle_results(session, round_id, station_id)
    counts = _counts(angles)
    rate = _rate_of(counts)

    rows = session.execute(
        select(DailyStat.day, func.sum(DailyStat.n_normal), func.sum(DailyStat.n_yellow), func.sum(DailyStat.n_brown))
        .where(DailyStat.station_id == station_id, DailyStat.day >= day - timedelta(days=TREND_DAYS), DailyStat.day < day)
        .group_by(DailyStat.day)).all()
    baseline, trend = history_facts({r[0]: tuple(int(x or 0) for x in r[1:]) for r in rows}, day, rate)

    previous = None
    prev_id = session.scalar(select(func.max(Capture.round_id)).where(Capture.station_id == station_id, Capture.round_id < round_id))
    if prev_id is not None:
        prev_counts = _counts(_angle_results(session, prev_id, station_id)[0])
        prev_rate = _rate_of(prev_counts)
        previous = {"round_id": prev_id, "day": _utc_day(session.get(Round, prev_id).started_at).isoformat(),
                    "abnormal_rate": prev_rate, "delta_pp": _pp(rate, prev_rate), "counts": prev_counts}

    batch = None
    found = stats.active_batch(list(session.scalars(select(Batch).where(Batch.station_id == station_id, Batch.planted_on <= day))), day)
    if found:
        age = stats.age_days(found, day)
        batch = {"age_days": age, "harvest_in_days": stats.HARVEST_AGE_DAYS - age, "cycle_days_assumed": stats.HARVEST_AGE_DAYS}

    return {
        "scope": "station", "round_id": round_id, "station_id": station_id, "station": station.name, "route_order": station.route_order,
        "day": day.isoformat(),
        "angles": angles, "counts": counts, "labelled": sum(counts.values()),
        "present": [a for a in ANGLES if angles[a]], "missing": [a for a in ANGLES if not angles[a]],
        "retakes": retakes, "needs_review": sum(1 for a in angles.values() if a and a["needs_review"]),
        "abnormal_rate": rate, "baseline": baseline, "trend": trend, "previous_visit": previous, "batch": batch,
    }


def round_station_ids(session: Session, round_id: int) -> list[int]:
    """Stations with at least one capture in the round, in route order."""
    return list(session.scalars(
        select(Station.id).where(Station.id.in_(select(Capture.station_id).where(Capture.round_id == round_id)))
        .order_by(Station.route_order, Station.id)))


def round_facts(session: Session, round_id: int) -> dict:
    rnd = session.get(Round, round_id)
    visited = round_station_ids(session, round_id)
    expected = set(visited) | set(session.scalars(select(Station.id).where(Station.kind == "station", Station.active.is_(True))))
    names = dict(session.execute(select(Station.id, Station.name).where(Station.id.in_(expected))).all()) if expected else {}
    order = {r[0]: i for i, r in enumerate(session.execute(
        select(Station.id).where(Station.id.in_(expected)).order_by(Station.route_order, Station.id)))}

    total = {stats.NORMAL: 0, stats.YELLOW: 0, stats.BROWN: 0}
    base_n = base_abn = review = 0
    rows, last_at = [], None
    for sid in sorted(visited, key=order.get):
        f = station_facts(session, round_id, sid)
        for k in total:
            total[k] += f["counts"][k]
        base_n += f["baseline"]["labelled"]
        base_abn += f["baseline"]["abnormal"]
        review += f["needs_review"]
        last_at = max(filter(None, [last_at, _angle_results(session, round_id, sid)[2]]), default=None)
        rows.append({"station_id": sid, "station": f["station"], "route_order": f["route_order"], "present": len(f["present"]), "missing": f["missing"],
                     "abnormal_rate": f["abnormal_rate"], "delta_pp": f["baseline"]["delta_pp"], "trend": f["trend"]["direction"]})
    rate = _rate_of(total)
    base_rate = None if not base_n else base_abn / base_n
    ranked = [r for r in rows if r["abnormal_rate"]]
    worst = max(ranked, key=lambda r: r["abnormal_rate"], default=None)
    seen = {r["station_id"]: r["present"] for r in rows}
    skipped = [{"station": names[s], "present": seen.get(s, 0)} for s in sorted(expected, key=order.get) if seen.get(s, 0) < len(ANGLES)]
    minutes = None if last_at is None else max(0, round((_naive_utc(last_at) - _naive_utc(rnd.started_at)).total_seconds() / 60))
    return {
        "scope": "round", "round_id": round_id, "day": _utc_day(rnd.started_at).isoformat(),
        "stations_inspected": len(rows), "stations_expected": len(expected), "stations": rows, "counts": total,
        "labelled": sum(total.values()), "abnormal_rate": rate, "needs_review": review,
        "baseline": {"labelled": base_n, "rate": base_rate, "delta_pp": _pp(rate, base_rate)},
        "worst": None if worst is None else {"station": worst["station"], "station_id": worst["station_id"],
                                              "route_order": worst["route_order"], "abnormal_rate": worst["abnormal_rate"]},
        "rising": [r["station"] for r in rows if r["trend"] == "increase"], "skipped": skipped, "duration_min": minutes,
    }


# --- template text ----------------------------------------------------------------------------

def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _change(pp: float, noun: str) -> str:
    return f"{noun}เท่าเดิม" if abs(pp) < 0.05 else f"{noun}{'สูงขึ้น' if pp > 0 else 'ต่ำลง'} {abs(pp):.1f} จุดเปอร์เซ็นต์"


def _advice(rate: float | None, rising: bool) -> str:
    if rate is None:
        return "ข้อแนะนำ: ยังสรุปสุขภาพผักไม่ได้ ควรตรวจกล้องและให้หุ่นยนต์ถ่ายภาพซ้ำ"
    if rate >= ALERT_RATE or (rising and rate > 0):
        return "ข้อแนะนำ: ควรให้ผู้ดูแลตรวจต้นผักด้วยตนเองโดยเร็ว"
    return "ข้อแนะนำ: เฝ้าติดตามในรอบถัดไป" if rate > 0 else "ข้อแนะนำ: ยังไม่ต้องดำเนินการเพิ่มเติม"


def _findings(counts: dict, where=None) -> str:
    """'ใบเหลือง 2 ด้าน (หน้า, ขวา) จุดน้ำตาล 1 ด้าน (หลัง)': only the two disease classes, never a normal count."""
    parts = []
    for label in (stats.YELLOW, stats.BROWN):
        if counts[label]:
            angles = f" ({', '.join(where[label])})" if where else ""
            parts.append(f"{label} {counts[label]} ด้าน{angles}")
    return " ".join(parts)


def _affected(angles: dict) -> dict:
    return {label: [ANGLE_TH[a] for a in ANGLES if angles[a] and angles[a]["label"] == label] for label in (stats.YELLOW, stats.BROWN)}


def _station_text(f: dict) -> str:
    c, name = f["counts"], f["station"]
    label = name if name.startswith("สถานี") else f"สถานี {name}"
    if f["labelled"]:
        found = f"พบ{_findings(c, _affected(f['angles']))}" if c[stats.YELLOW] or c[stats.BROWN] else "ไม่พบใบเหลืองหรือจุดน้ำตาล"
        first = f"{label} {found} จาก {f['labelled']} ด้านที่วิเคราะห์ได้ (อัตราผิดปกติ {_pct(f['abnormal_rate'])})."
    else:
        first = f"{label} ยังไม่มีผลวิเคราะห์ที่ใช้ได้ (ถ่ายได้ {len(f['present'])} จาก 4 มุม)."
    lines, notes = [first], []
    if f["missing"]:
        notes.append("ขาดมุม" + " ".join(ANGLE_TH[a] for a in f["missing"]))
    if f["retakes"]:
        notes.append(f"ถ่ายซ้ำ {f['retakes']} ครั้ง")
    if f["needs_review"]:
        notes.append(f"รอตรวจทาน {f['needs_review']} มุม")
    if notes:
        lines.append("หมายเหตุ: " + ", ".join(notes) + ".")

    b, rate = f["baseline"], f["abnormal_rate"]
    if b["rate"] is None:
        lines.append("ไม่มีข้อมูลย้อนหลัง 7 วันของสถานีนี้ จึงยังเทียบกับวันก่อนหน้าไม่ได้.")
    elif b["delta_pp"] is None:
        lines.append(f"7 วันก่อนหน้าอัตราผิดปกติ {_pct(b['rate'])} (จาก {b['labelled']} ภาพ) แต่รอบนี้ยังเทียบไม่ได้.")
    else:
        lines.append(f"เทียบ 7 วันก่อนหน้า (อัตราผิดปกติ {_pct(b['rate'])} จาก {b['labelled']} ภาพ): {_change(b['delta_pp'], 'รอบนี้')}.")

    t = f["trend"]
    if t["direction"] == "insufficient":
        lines.append(f"แนวโน้ม 14 วัน: ข้อมูลไม่พอ (มีข้อมูล {t['days']} วัน).")
    else:
        lines.append(f"แนวโน้ม 14 วัน: {t['label']} (7 วันแรก {_pct(t['first_rate'])} เป็น 7 วันหลัง {_pct(t['last_rate'])}).")

    p = f["previous_visit"]
    if p is None:
        lines.append("ยังไม่มีรอบตรวจก่อนหน้าที่สถานีนี้.")
    elif p["abnormal_rate"] is None:
        lines.append(f"รอบตรวจก่อนหน้า (รอบที่ {p['round_id']}) ไม่มีผลวิเคราะห์ที่ใช้ได้ จึงเทียบไม่ได้.")
    elif p["delta_pp"] is not None:
        lines.append(f"เทียบรอบตรวจก่อนหน้า (รอบที่ {p['round_id']} อัตราผิดปกติ {_pct(p['abnormal_rate'])}): {_change(p['delta_pp'], 'รอบนี้')}.")

    batch = f["batch"]
    if batch:
        left = batch["harvest_in_days"]
        due = f"อีกประมาณ {left} วันถึงกำหนดเก็บเกี่ยว" if left >= 0 else f"เกินกำหนดเก็บเกี่ยว {-left} วัน"
        lines.append(f"ล็อตที่ปลูกอยู่อายุ {batch['age_days']} วัน {due} (รอบเก็บเกี่ยว {batch['cycle_days_assumed']} วันเป็นค่าสมมติ).")
    lines.append(_advice(rate, t["direction"] == "increase"))
    return "\n".join(lines)


def _round_text(f: dict) -> str:
    rid, n, m = f["round_id"], f["stations_inspected"], f["stations_expected"]
    if f["labelled"]:
        c = f["counts"]
        found = f"พบ{_findings(c)}" if c[stats.YELLOW] or c[stats.BROWN] else "ไม่พบใบเหลืองหรือจุดน้ำตาล"
        first = f"รอบที่ {rid} ตรวจ {n} จาก {m} สถานี {found} จาก {f['labelled']} ด้านที่วิเคราะห์ได้ (อัตราผิดปกติรวม {_pct(f['abnormal_rate'])})."
    else:
        first = f"รอบที่ {rid} ตรวจ {n} จาก {m} สถานี ยังไม่มีผลวิเคราะห์ที่ใช้ได้."
    lines = [first]
    b = f["baseline"]
    if b["rate"] is None:
        lines.append("ไม่มีข้อมูลย้อนหลัง 7 วัน จึงยังเทียบกับวันก่อนหน้าไม่ได้.")
    elif b["delta_pp"] is not None:
        lines.append(f"เทียบ 7 วันก่อนหน้า (อัตราผิดปกติ {_pct(b['rate'])} จาก {b['labelled']} ภาพ): {_change(b['delta_pp'], 'รอบนี้')}.")
    if f["worst"]:
        lines.append(f"สถานีที่ผิดปกติสูงสุด: {f['worst']['station']} ({_pct(f['worst']['abnormal_rate'])}).")
    if f["rising"]:
        lines.append("สถานีที่แนวโน้ม 14 วันเพิ่มขึ้น: " + ", ".join(f["rising"]) + ".")
    if f["skipped"]:
        lines.append("สถานีที่ตรวจไม่ครบ 4 มุม: " + ", ".join(f"{s['station']} ({s['present']}/4)" for s in f["skipped"]) + ".")
    extra = []
    if f["needs_review"]:
        extra.append(f"รอตรวจทานรวม {f['needs_review']} มุม")
    if f["duration_min"] is not None:
        extra.append(f"ใช้เวลาประมาณ {f['duration_min']} นาที")
    if extra:
        lines.append(", ".join(extra) + ".")
    lines.append(_advice(f["abnormal_rate"], bool(f["rising"])))
    return "\n".join(lines)


def render_text(facts: dict) -> str:
    """Deterministic Thai text from the facts; always available, used as the fallback for the AI wording."""
    return _round_text(facts) if facts["scope"] == "round" else _station_text(facts)


# --- robot screen: the three comparisons (findings only: yellow and brown, never a normal count) ---

COMPARE_DAYS = 14


def _has_data(vals) -> bool:
    return bool(vals) and sum(vals) > 0


def screen_compare(by_day: dict, day: date, previous: dict | None) -> dict:
    """What the current counts are compared with: the latest earlier day with data, the 7-day daily average, the previous visit."""
    earlier = [(day - timedelta(days=k)) for k in range(1, COMPARE_DAYS) if _has_data(by_day.get(day - timedelta(days=k)))]
    yesterday = None
    if earlier:
        d = earlier[0]
        yesterday = {"day": d.isoformat(), "yellow": by_day[d][1], "brown": by_day[d][2]}
    week = [d for d in earlier if (day - d).days <= BASELINE_DAYS]
    avg7 = None
    if week:
        avg7 = {"days": len(week), "yellow": round(sum(by_day[d][1] for d in week) / len(week), 1),
                "brown": round(sum(by_day[d][2] for d in week) / len(week), 1)}
    return {"yesterday": yesterday, "avg7": avg7, "previous_visit": previous}


def round_trend(facts: dict) -> str:
    """Direction word for a whole round (same thresholds as the robot display)."""
    delta = facts["baseline"]["delta_pp"]
    if facts["rising"] or (delta is not None and delta >= TREND_PP):
        return "increase"
    if delta is not None and delta <= -TREND_PP:
        return "decrease"
    return "flat" if delta is not None else "insufficient"


# --- robot display (128x64 OLED: ASCII only, 21 chars x 6 lines) --------------------------------

DISPLAY_COLS = 21
HEADLINE_MAX = 90
_TREND_ASCII = {"increase": "UP", "decrease": "DOWN", "flat": "FLAT", "insufficient": "NO DATA"}


def _ascii(s: str) -> str:
    """Printable ASCII only, no double quote or backslash (the firmware reads the JSON by hand), at most one OLED line."""
    return "".join(ch if " " <= ch <= "~" and ch not in ('"', "\\") else "?" for ch in s)[:DISPLAY_COLS]


def _signed(pp: float | None) -> str:
    return "n/a" if pp is None else f"{pp:+.0f}pp"


def _st(order, station_id) -> str:
    return f"ST{order or station_id}"


def _delta(n: int) -> str:
    return f"{n:+d}" if n else "0"


def _where(angles: dict, label: str) -> str:
    return ",".join(a[0].upper() for a in ANGLES if angles[a] and angles[a]["label"] == label)


def display(facts: dict) -> dict:
    """The robot-screen payload, built from the facts only (never from AI text, never Thai or station names).
    Only the two disease classes are shown; a clean station is just 'No disease found'."""
    c, rate = facts["counts"], facts["abnormal_rate"]
    y, b = c[stats.YELLOW], c[stats.BROWN]
    if facts["scope"] == "round":
        delta, rising = facts["baseline"]["delta_pp"], bool(facts["rising"])
        base, worst = facts["baseline"]["rate"], facts["worst"]
        trend = "UP" if rising or (delta is not None and delta >= TREND_PP) else "DOWN" if delta is not None and delta <= -TREND_PP             else "FLAT" if delta is not None else "NO DATA"
        level = "alert" if (rate or 0) >= 0.5 or (delta or 0) >= 10 else "warn" if rate or facts["skipped"] else "ok"
        found = "No analysed angles" if not facts["labelled"] else f"Y {y} B {b} of {facts['labelled']} views" if y or b else "No disease found"
        lines = [
            f"ROUND {facts['round_id']} DONE",
            found if len(found) <= DISPLAY_COLS else found.removesuffix(" views"),
            "Abn n/a" if rate is None else f"Abn {rate * 100:.0f}% (7d {'n/a' if base is None else f'{base * 100:.0f}%'})",
            "Worst: " + (_st(worst["route_order"], worst["station_id"]) if worst else "none"),
            f"Trend: {trend}",
            f"Review: {facts['needs_review']}" if facts["needs_review"] else "",
            f"Skipped: {len(facts['skipped'])}" if facts["skipped"] else "",
        ]
    else:
        t, prev = facts["trend"], facts["previous_visit"]
        level = "alert" if (rate or 0) >= 0.5 or (t["direction"] == "increase" and (t["delta_pp"] or 0) >= 10)             else "warn" if y or b or facts["missing"] else "ok"
        if prev is None:
            before = "Prev: none"
        elif prev["abnormal_rate"] is None or rate is None:
            before = "Prev: n/a"
        else:
            before = f"Prev: Y{_delta(y - prev['counts'][stats.YELLOW])} B{_delta(b - prev['counts'][stats.BROWN])}"
        groups = [f"{k}:{_where(facts['angles'], label)}" for k, label in (("Y", stats.YELLOW), ("B", stats.BROWN)) if c[label]]
        vs = facts["baseline"]["delta_pp"]
        lines = [
            f"{_st(facts['route_order'], facts['station_id'])} {len(facts['present'])}/4 angles",
            "No analysed angles" if rate is None else f"Yellow {y} Brown {b}" if y or b else "No disease found",
            "Abn n/a" if rate is None else f"Abn {rate * 100:.0f}% vs 7d {_signed(vs)}",
            f"Trend: {_TREND_ASCII[t['direction']]}",
            before,
            f"Review: {facts['needs_review']}" if facts["needs_review"] else "",
            "  ".join(groups),
        ]
    out = {"level": level, "title": _ascii(lines[0])}
    out.update({f"l{i}": _ascii(x) for i, x in enumerate(lines[1:], 1)})
    return out


def device_headline(text: str) -> str:
    h = headline(text)
    return h if len(h) <= HEADLINE_MAX else h[:HEADLINE_MAX - 1] + "…"


# --- optional AI wording ----------------------------------------------------------------------

PROMPT = (
    "คุณเป็นผู้ช่วยสรุปผลตรวจผักในโรงเรือน เขียนสรุปภาษาไทยไม่เกิน 120 คำ จากข้อมูล JSON ด้านล่างเท่านั้น "
    "ใช้เฉพาะตัวเลขที่มีในข้อมูล ห้ามเดาหรือเพิ่มตัวเลขเอง บอกว่าเมื่อเทียบกับวันก่อนหน้าเปลี่ยนไปอย่างไร แนวโน้มเป็นอย่างไร "
    "และผู้ปลูกควรทำอะไร พูดถึงเฉพาะใบเหลืองและจุดน้ำตาล ห้ามระบุจำนวนหรือผลของใบที่ปกติ ถ้าข้อมูลไม่พอให้บอกตรง ๆ ตอบเป็นข้อความธรรมดา ไม่ใช้ markdown\n\nข้อมูล:\n"
)


def _without_normal(o):
    """Facts for the AI without any ปกติ / normal count, so its wording cannot mention one."""
    if isinstance(o, dict):
        return {k: _without_normal(v) for k, v in o.items() if k not in (stats.NORMAL, "normal", "n_normal")}
    if isinstance(o, list):
        return [_without_normal(v) for v in o]
    return o


def _ai_call(facts: dict) -> tuple[str, str] | None:
    """One text-only request to the configured provider. Same key lookup and base-URL checks as analyzer.py."""
    try:
        prov = get_provider(runtime_config.vision_provider())
        content = PROMPT + json.dumps(_without_normal(facts), ensure_ascii=False, separators=(",", ":"))
        timeout = AI_BUDGET_S - 1
        if prov.kind == "anthropic":
            model = runtime_config.get_setting(runtime_config.VISION_MODEL) or settings.vision_model
            key = runtime_config.db_provider_key("anthropic")  # none: the SDK reads ANTHROPIC_API_KEY itself
            client = anthropic.Anthropic(**({"api_key": key} if key else {}), timeout=timeout, max_retries=0)
            resp = client.messages.create(model=model, max_tokens=4096, messages=[{"role": "user", "content": content}])
            if resp.stop_reason in ("refusal", "max_tokens"):
                return None
            text, used = next((b.text for b in resp.content if b.type == "text"), ""), getattr(resp, "model", None) or model
        else:
            model = runtime_config.vision_model()
            db_url = runtime_config.get_setting(runtime_config.VISION_BASE_URL)
            # checked at use time: a stale web-set URL must never receive the key
            if not model or (db_url and runtime_config.base_url_error(db_url, prov.name)):
                return None
            key = runtime_config.provider_api_key(prov.name)
            if not key:
                return None
            client = openai.OpenAI(api_key=key, base_url=db_url or settings.vision_base_url or prov.base_url,
                                   timeout=timeout, max_retries=0)
            resp = client.chat.completions.create(
                model=model, timeout=timeout, messages=[{"role": "user", "content": content}],
                **{"max_completion_tokens" if prov.name == "openai" else "max_tokens": 4096})
            choice = resp.choices[0]
            if choice.finish_reason in ("length", "content_filter"):
                return None
            text, used = choice.message.content, getattr(resp, "model", None) or model
        text = (text or "").strip()
        return (text[:AI_MAX_CHARS], used) if text else None
    except Exception as e:  # type only: provider errors can echo request details
        log.warning("summary AI narration failed: %s", type(e).__name__)
        return None


def ai_narrate(facts: dict) -> tuple[str, str] | None:
    """(text, model) or None. Never raises; gives up after AI_BUDGET_S so the caller can use the template text."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_ai_call, facts).result(timeout=AI_BUDGET_S)
    except Exception as e:  # includes the budget TimeoutError
        log.warning("summary AI narration failed: %s", type(e).__name__)
        return None
    finally:
        pool.shutdown(wait=False)  # a straggler thread ends by its own client timeout


# --- storage ----------------------------------------------------------------------------------

def _upsert(session: Session, round_id: int, station_id: int | None, facts: dict, text: str, source: str, model: str | None) -> Summary:
    same = Summary.station_id.is_(None) if station_id is None else Summary.station_id == station_id
    rows = session.scalars(select(Summary).where(Summary.round_id == round_id, same).order_by(Summary.id)).all()
    row = rows[0] if rows else Summary(round_id=round_id, station_id=station_id, scope=facts["scope"])
    for extra in rows[1:]:  # NULL station_id is not unique-protected by the database
        session.delete(extra)
    row.text, row.facts, row.source, row.model, row.updated_at = text, facts, source, model, datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    return row


def _generate(session: Session, round_id: int, station_id: int | None, facts: dict, use_ai: bool) -> Summary:
    text, source, model = render_text(facts), "template", None
    session.commit()  # end the read transaction: the AI call below must not hold a database connection
    ai = None
    if use_ai:
        try:
            ai = ai_narrate(facts)
        except Exception as e:  # ai_narrate never raises by contract; the template text must survive a broken one anyway
            log.warning("summary AI narration failed: %s", type(e).__name__)
    if ai:
        text, (source, model) = ai[0], ("ai", ai[1])
    return _upsert(session, round_id, station_id, facts, text, source, model)


def generate_station_summary(session: Session, round_id: int, station_id: int, use_ai: bool = True) -> Summary:
    return _generate(session, round_id, station_id, station_facts(session, round_id, station_id), use_ai)


def generate_round_summary(session: Session, round_id: int, use_ai: bool = True) -> Summary:
    return _generate(session, round_id, None, round_facts(session, round_id), use_ai)


def ensure_station_summaries(session: Session, round_id: int) -> None:
    """Template summaries for the round's stations that have none yet."""
    have = set(session.scalars(select(Summary.station_id).where(Summary.round_id == round_id, Summary.station_id.is_not(None))))
    for sid in round_station_ids(session, round_id):
        if sid not in have:
            generate_station_summary(session, round_id, sid, use_ai=False)


def has_round_summary(session: Session, round_id: int) -> bool:
    return session.scalar(select(Summary.id).where(Summary.round_id == round_id, Summary.station_id.is_(None)).limit(1)) is not None


# --- background entry points (own Session; never raise) ---------------------------------------

_busy: set[tuple[str, int]] = set()
_busy_lock = threading.Lock()


def _background(kind: str, round_id: int, work) -> None:
    with _busy_lock:  # repeated docked heartbeats or double clicks must not pile up duplicate work
        if (kind, round_id) in _busy:
            return
        _busy.add((kind, round_id))
    try:
        with Session(app.db.engine) as s:
            work(s)
    except Exception as e:
        log.warning("background summary failed: %s", type(e).__name__)
    finally:
        with _busy_lock:
            _busy.discard((kind, round_id))


def docked_fallback(round_id: int) -> None:
    """Robot docked: if the round has captures but no round summary, make the template one (the robot did not call /finish)."""
    def work(s: Session) -> None:
        if has_round_summary(s, round_id) or not round_station_ids(s, round_id):
            return
        ensure_station_summaries(s, round_id)
        generate_round_summary(s, round_id, use_ai=False)
    _background("docked", round_id, work)


def regenerate_round(round_id: int) -> None:
    """Admin button: every station summary and the round summary again, with AI when it is configured."""
    def work(s: Session) -> None:
        for sid in round_station_ids(s, round_id):
            generate_station_summary(s, round_id, sid, use_ai=True)
        generate_round_summary(s, round_id, use_ai=True)
    _background("regen", round_id, work)
