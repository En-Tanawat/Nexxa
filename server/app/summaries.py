"""Per-station and per-round inspection summaries: facts from the database, deterministic Thai text, optional AI wording.

Every summary sentence is written by the AI (and only the AI), from records retrieved from the database (retrieve():
this visit, earlier visits, the lot's daily history, the 7/14-day baseline). The AI must answer a fixed JSON shape
(result / compare / trend / advice) that render_summary() lays out the same way every time, and an answer that uses a
number which is not in the retrieved records is rejected. When the AI is unavailable the row is stored as "pending"
(facts only, empty text) and retry_pending() fills it in later: there is no code-written fallback text.
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
from app.analyzer import _first_json_object
from app.config import settings
from app.models import Batch, Capture, DailyStat, Round, Station, StationAnalysis, Summary
from app.providers import get_provider

log = logging.getLogger(__name__)

BASELINE_DAYS, TREND_DAYS = 7, 14
TREND_MIN_DAYS = 3  # fewer days with data than this in the trend window: "ข้อมูลไม่พอ"
TREND_PP = 5.0  # first-7 vs last-7 change that counts as a trend (percentage points)
AI_BUDGET_S = 25  # hard cap for the AI call; after that the summary stays pending and is retried later
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

def _station_result(session: Session, round_id: int, station_id: int) -> dict:
    """What one station visit found: plant counts from its (single) analysis, plus photo bookkeeping."""
    caps = session.execute(
        select(Capture.id, Capture.camera_angle, Capture.quality_ok, Capture.taken_at)
        .where(Capture.round_id == round_id, Capture.station_id == station_id).order_by(Capture.id)).all()
    usable = {c.camera_angle or f"#{c.id}": c for c in caps if c.quality_ok is not False}
    an = session.scalars(select(StationAnalysis).where(
        StationAnalysis.round_id == round_id, StationAnalysis.station_id == station_id).order_by(StationAnalysis.id.desc()).limit(1)).first()
    counts = {stats.NORMAL: 0, stats.YELLOW: 0, stats.BROWN: 0}
    if an is not None and an.n_total is not None:
        counts = {stats.NORMAL: an.n_total - an.n_yellow - an.n_brown, stats.YELLOW: an.n_yellow, stats.BROWN: an.n_brown}
    return {"counts": counts, "present": list(usable),
            "retakes": sum(1 for c in caps if c.quality_ok is False), "last_at": max((c.taken_at for c in caps), default=None),
            "needs_review": int(an is not None and an.needs_review)}


def _rate_of(counts: dict) -> float | None:
    return abnormal_rate(counts[stats.NORMAL], counts[stats.YELLOW], counts[stats.BROWN])


def station_facts(session: Session, round_id: int, station_id: int) -> dict:
    station, rnd = session.get(Station, station_id), session.get(Round, round_id)
    day = _utc_day(rnd.started_at)
    res = _station_result(session, round_id, station_id)
    counts, expected = res["counts"], settings.photos_per_station
    rate = _rate_of(counts)

    rows = session.execute(
        select(DailyStat.day, func.sum(DailyStat.n_normal), func.sum(DailyStat.n_yellow), func.sum(DailyStat.n_brown))
        .where(DailyStat.station_id == station_id, DailyStat.day >= day - timedelta(days=TREND_DAYS), DailyStat.day < day)
        .group_by(DailyStat.day)).all()
    baseline, trend = history_facts({r[0]: tuple(int(x or 0) for x in r[1:]) for r in rows}, day, rate)

    previous = None
    prev_id = session.scalar(select(func.max(Capture.round_id)).where(Capture.station_id == station_id, Capture.round_id < round_id))
    if prev_id is not None:
        prev_counts = _station_result(session, prev_id, station_id)["counts"]
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
        "counts": counts, "labelled": sum(counts.values()),  # counts are PLANTS; labelled = plants the AI saw
        "present": res["present"], "expected": expected, "missing": max(0, expected - len(res["present"])),
        "retakes": res["retakes"], "needs_review": res["needs_review"],
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
        last_at = max(filter(None, [last_at, _station_result(session, round_id, sid)["last_at"]]), default=None)
        rows.append({"station_id": sid, "station": f["station"], "route_order": f["route_order"], "present": len(f["present"]), "missing": f["missing"],
                     "abnormal_rate": f["abnormal_rate"], "delta_pp": f["baseline"]["delta_pp"], "trend": f["trend"]["direction"]})
    rate = _rate_of(total)
    base_rate = None if not base_n else base_abn / base_n
    ranked = [r for r in rows if r["abnormal_rate"]]
    worst = max(ranked, key=lambda r: r["abnormal_rate"], default=None)
    seen = {r["station_id"]: r["present"] for r in rows}
    skipped = [{"station": names[s], "present": seen.get(s, 0)} for s in sorted(expected, key=order.get) if seen.get(s, 0) < settings.photos_per_station]
    minutes = None if last_at is None else max(0, round((_naive_utc(last_at) - _naive_utc(rnd.started_at)).total_seconds() / 60))
    return {
        "scope": "round", "round_id": round_id, "day": _utc_day(rnd.started_at).isoformat(), "photos_expected": settings.photos_per_station,
        "stations_inspected": len(rows), "stations_expected": len(expected), "stations": rows, "counts": total,
        "labelled": sum(total.values()), "abnormal_rate": rate, "needs_review": review,
        "baseline": {"labelled": base_n, "rate": base_rate, "delta_pp": _pp(rate, base_rate)},
        "worst": None if worst is None else {"station": worst["station"], "station_id": worst["station_id"],
                                              "route_order": worst["route_order"], "abnormal_rate": worst["abnormal_rate"]},
        "rising": [r["station"] for r in rows if r["trend"] == "increase"], "skipped": skipped, "duration_min": minutes,
    }


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
        found = "No analysis" if not facts["labelled"] else f"Y {y} B {b} of {facts['labelled']}" if y or b else "No disease found"
        lines = [
            f"ROUND {facts['round_id']} DONE",
            found,
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
        vs = facts["baseline"]["delta_pp"]
        lines = [
            f"{_st(facts['route_order'], facts['station_id'])} {len(facts['present'])}/{facts['expected']} photos",
            "No analysis" if rate is None else f"Yellow {y} Brown {b}" if y or b else "No disease found",
            "Abn n/a" if rate is None else f"Abn {rate * 100:.0f}% vs 7d {_signed(vs)}",
            f"Trend: {_TREND_ASCII[t['direction']]}",
            before,
            f"Review: {facts['needs_review']}" if facts["needs_review"] else "",
            "",
        ]
    out = {"level": level, "title": _ascii(lines[0])}
    out.update({f"l{i}": _ascii(x) for i, x in enumerate(lines[1:], 1)})
    return out


def device_headline(text: str) -> str:
    h = headline(text)
    return h if len(h) <= HEADLINE_MAX else h[:HEADLINE_MAX - 1] + "…"


# --- the AI summary: retrieve from the database, then the model writes every sentence ----------------

SECTIONS = ("result", "compare", "trend", "advice")
SECTION_LABEL = {"compare": "เทียบก่อนหน้า", "trend": "แนวโน้ม", "advice": "ข้อแนะนำ"}  # "result" is the first line, no label
LINE_MAX = 200
HISTORY_VISITS = 10  # earlier visits of the station handed to the AI
LOT_DAYS = 60  # most recent days of the lot's history handed to the AI
ALLOWED_NUMBERS = {0.0, 1.0, float(BASELINE_DAYS), float(TREND_DAYS)}  # words like "7 วัน" / "14 วัน" are not data

NL = chr(10)
PROMPT = (
    "คุณเป็นผู้ช่วยสรุปผลตรวจผักในโรงเรือน สรุปจากข้อมูลที่ดึงมาจากฐานข้อมูล (JSON ด้านล่าง) เท่านั้น "
    "ตอบเป็น JSON object เดียว มี 4 คีย์เท่านั้น แต่ละคีย์เป็นข้อความภาษาไทย 1 ประโยค บรรทัดเดียว ไม่เกิน 160 ตัวอักษร: "
    "result = ผลตรวจครั้งนี้ (จำนวนต้นใบเหลือง จำนวนต้นจุดน้ำตาล จากผักที่ตรวจกี่ต้น), "
    "compare = เทียบกับ 7 วันก่อนหน้าและกับการตรวจครั้งก่อน, "
    "trend = แนวโน้ม 14 วันและตลอดล็อต, advice = ข้อแนะนำสำหรับผู้ปลูก. "
    "กฎ: ใช้เฉพาะตัวเลขที่มีในข้อมูล ห้ามคำนวณหรือเดาตัวเลขเอง ถ้าไม่มีข้อมูลให้เปรียบเทียบให้เขียนว่ายังเปรียบเทียบไม่ได้ "
    "พูดถึงเฉพาะใบเหลืองและจุดน้ำตาล ห้ามพูดถึงผักที่ปกติ ไม่ใช้ markdown ตอบเฉพาะ JSON" + NL + NL + "ข้อมูล:" + NL
)


def _without_normal(o):
    """Records for the AI without any ปกติ / normal count, so its wording cannot mention one."""
    if isinstance(o, dict):
        return {k: _without_normal(v) for k, v in o.items() if k not in (stats.NORMAL, "normal", "n_normal")}
    if isinstance(o, list):
        return [_without_normal(v) for v in o]
    return o


def _with_percentages(o):
    """Every *rate (a fraction) also as <name>_pct so the AI quotes a percentage instead of doing arithmetic."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            out[k] = _with_percentages(v)
            if k.endswith("rate") and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k + "_pct"] = round(v * 100, 1)
        return out
    if isinstance(o, list):
        return [_with_percentages(v) for v in o]
    return o


def retrieve(session: Session, round_id: int, station_id: int | None, facts: dict) -> dict:
    """The records the AI may use: this visit's facts plus what the database holds about earlier visits and the lot."""
    ctx = {"facts": _without_normal(facts)}
    if station_id is not None:
        rows = session.execute(
            select(StationAnalysis.round_id, StationAnalysis.created_at, StationAnalysis.n_total, StationAnalysis.n_yellow, StationAnalysis.n_brown)
            .where(StationAnalysis.station_id == station_id, StationAnalysis.round_id < round_id, StationAnalysis.n_total.is_not(None))
            .order_by(StationAnalysis.round_id.desc()).limit(HISTORY_VISITS)).all()
        ctx["earlier_visits"] = [{"round_id": r, "day": _utc_day(at).isoformat(), "plants": t, "yellow": y, "brown": b}
                                 for r, at, t, y, b in rows]
        if facts.get("batch"):  # the lot's daily history since planting, newest LOT_DAYS days with data
            day = date.fromisoformat(facts["day"])
            since = day - timedelta(days=facts["batch"]["age_days"])
            days = session.execute(
                select(DailyStat.day, func.sum(DailyStat.n_normal), func.sum(DailyStat.n_yellow), func.sum(DailyStat.n_brown))
                .where(DailyStat.station_id == station_id, DailyStat.day >= since, DailyStat.day < day)
                .group_by(DailyStat.day).order_by(DailyStat.day.desc()).limit(LOT_DAYS)).all()
            ctx["lot_days"] = [{"day": d.isoformat(), "plants": int((n or 0) + (y or 0) + (b or 0)), "yellow": int(y or 0), "brown": int(b or 0)}
                               for d, n, y, b in days if (n or 0) + (y or 0) + (b or 0) > 0]
    return _with_percentages(ctx)


def _numbers_in(text: str) -> set[float]:
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)}


def _parse_summary(text: str, context: dict) -> dict | None:
    """The four sections as clean one-line strings, or None when the answer cannot be used (wrong shape, too long,
    mentions normal plants, or quotes a number that is not in the retrieved records)."""
    try:
        data = _first_json_object(text)
        out = {k: " ".join(str(data[k]).split()) for k in SECTIONS}
    except (ValueError, KeyError, TypeError):
        return None
    allowed = _numbers_in(json.dumps(context, ensure_ascii=False)) | ALLOWED_NUMBERS
    for line in out.values():
        if not line or len(line) > LINE_MAX or re.search(r"(?<!ผิด)ปกติ", line):
            return None
        for token in re.findall(r"\d+(?:\.\d+)?", line):
            v, places = float(token), len(token.split(".")[1]) if "." in token else 0
            if not any(a == v or round(a, places) == v for a in allowed):
                return None
    return out


def render_summary(sections: dict) -> str:
    """Always the same layout: the result line, then the compare / trend / advice lines with fixed labels."""
    return NL.join([sections["result"]] + [f"{SECTION_LABEL[k]}: {sections[k]}" for k in SECTIONS[1:]])


def device_headline(text: str) -> str:
    h = headline(text)
    return h if len(h) <= HEADLINE_MAX else h[:HEADLINE_MAX - 1] + "…"


def _ai_call(context: dict) -> tuple[dict, str] | None:
    """One text-only request to the configured provider. Same key lookup and base-URL checks as analyzer.py."""
    try:
        prov = get_provider(runtime_config.vision_provider())
        content = PROMPT + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
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
        parsed = _parse_summary(text or "", context)
        if parsed is None:
            log.warning("summary AI answer rejected (shape, length, normal-plant mention or an unknown number)")
        return (parsed, used) if parsed else None
    except Exception as e:  # type only: provider errors can echo request details
        log.warning("summary AI call failed: %s", type(e).__name__)
        return None


def ai_narrate(context: dict) -> tuple[dict, str] | None:
    """(sections, model) or None. Never raises; gives up after AI_BUDGET_S (the summary then stays pending)."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_ai_call, context).result(timeout=AI_BUDGET_S)
    except Exception as e:  # includes the budget TimeoutError
        log.warning("summary AI call failed: %s", type(e).__name__)
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
    """Store the facts and, when the AI answers, its summary. No answer: the row stays "pending" with an empty text."""
    context = retrieve(session, round_id, station_id, facts)
    session.commit()  # end the read transaction: the AI call below must not hold a database connection
    ai = None
    if use_ai:
        try:
            ai = ai_narrate(context)
        except Exception as e:  # ai_narrate never raises by contract; the row must be stored anyway
            log.warning("summary AI call failed: %s", type(e).__name__)
    if ai:
        return _upsert(session, round_id, station_id, facts, render_summary(ai[0]), "ai", ai[1])
    return _upsert(session, round_id, station_id, facts, "", "pending", None)


def generate_station_summary(session: Session, round_id: int, station_id: int, use_ai: bool = True) -> Summary:
    return _generate(session, round_id, station_id, station_facts(session, round_id, station_id), use_ai)


def generate_round_summary(session: Session, round_id: int, use_ai: bool = True) -> Summary:
    return _generate(session, round_id, None, round_facts(session, round_id), use_ai)


def ensure_station_summaries(session: Session, round_id: int) -> None:
    """Pending rows (facts only) for the round's stations that have none yet; retry_pending() asks the AI later."""
    have = set(session.scalars(select(Summary.station_id).where(Summary.round_id == round_id, Summary.station_id.is_not(None))))
    for sid in round_station_ids(session, round_id):
        if sid not in have:
            generate_station_summary(session, round_id, sid, use_ai=False)


def has_round_summary(session: Session, round_id: int) -> bool:
    return session.scalar(select(Summary.id).where(Summary.round_id == round_id, Summary.station_id.is_(None)).limit(1)) is not None


def retry_pending(limit: int = 6) -> list[tuple[int, int | None]]:
    """Ask the AI again for summaries that have no AI text yet (newest first). Returns the (round, station) pairs it filled in."""
    filled = []
    with Session(app.db.engine) as s:
        todo = s.execute(select(Summary.round_id, Summary.station_id).where(Summary.source != "ai").order_by(Summary.id.desc()).limit(limit)).all()
        for rid, sid in todo:
            try:
                row = generate_station_summary(s, rid, sid) if sid is not None else generate_round_summary(s, rid)
            except Exception as e:
                log.warning("summary retry failed: %s", type(e).__name__)
                s.rollback()
                continue
            if row.source == "ai":
                filled.append((rid, sid))
    return filled


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
    """Robot docked: if the round has captures but no round summary, make it now (the robot did not call /finish)."""
    def work(s: Session) -> None:
        if has_round_summary(s, round_id) or not round_station_ids(s, round_id):
            return
        ensure_station_summaries(s, round_id)
        generate_round_summary(s, round_id)
    _background("docked", round_id, work)
