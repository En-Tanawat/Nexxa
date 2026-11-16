"""Telegram notifications: the sender, the settings reader and the background notifier.

Safety rules: the only host ever called is api.telegram.org over https (hard-coded, no redirects, 10 s timeout); the bot
token is never logged, echoed or put in an audit row (only the error type and Telegram's HTTP status/description are);
the message is plain text (no parse_mode, so nothing in it can be read as markup). Nothing here may ever raise into a
caller: the robot's response and the web pages must not depend on Telegram.
"""
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

import app.db
from app import runtime_config, stats, summaries
from app.models import AppSetting, Summary
from app.web import secretbox

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and the URL carries the bot token: keep those records from existing at all
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

API_HOST = "api.telegram.org"
TIMEOUT_S = 10
MAX_CHARS = 3500
MAX_SENDS_PER_MIN = 20

ENABLED, TOKEN_ENC, TOKEN_LAST4, CHAT_ID = "telegram_enabled", "telegram_token_enc", "telegram_token_last4", "telegram_chat_id"
NOTIFY_STATION, NOTIFY_ROUND, ONLY_ABNORMAL = "telegram_notify_station", "telegram_notify_round", "telegram_only_abnormal"
SETTING_KEYS = (ENABLED, TOKEN_ENC, TOKEN_LAST4, CHAT_ID, NOTIFY_STATION, NOTIFY_ROUND, ONLY_ABNORMAL)

TOKEN_RE = re.compile(r"[0-9]{5,15}:[A-Za-z0-9_-]{20,64}")  # also keeps "/", "?" and "#" out of the request URL
CHAT_ID_RE = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{5,32}")


# --- sender -------------------------------------------------------------------------------------

def _description(r: httpx.Response, token: str) -> str:
    try:
        d = str(r.json().get("description", ""))[:200]
    except Exception:
        return ""
    return "" if token in d else d


def send_message(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """(ok, short Thai reason). Never raises."""
    try:
        r = httpx.post(f"https://{API_HOST}/bot{token}/sendMessage", follow_redirects=False, timeout=TIMEOUT_S,
                       json={"chat_id": chat_id, "text": text[:MAX_CHARS], "disable_web_page_preview": True})
    except Exception as e:  # str(e) may contain the URL: log the type only
        log.warning("telegram send failed: %s", type(e).__name__)
        return False, "เชื่อมต่อ Telegram ไม่ได้ (เครือข่ายขัดข้องหรือหมดเวลา)"
    if r.status_code == 200:
        return True, "ส่งแล้ว"
    desc = _description(r, token)
    log.warning("telegram rejected the message: HTTP %s %s", r.status_code, desc if 400 <= r.status_code < 500 else "")
    reasons = {
        401: "โทเค็นบอทไม่ถูกต้อง",
        404: "โทเค็นบอทไม่ถูกต้อง",
        400: "ไม่พบแชทนี้ หรือบอทไม่มีสิทธิ์เขียนในแชท (ตรวจ Chat ID และเพิ่มบอทเข้าแชทก่อน)",
        403: "บอทถูกบล็อกหรือถูกนำออกจากแชทนี้",
        429: "ส่งถี่เกินไป Telegram ขอให้ลองใหม่ภายหลัง",
    }
    return False, reasons.get(r.status_code, f"Telegram ตอบกลับผิดปกติ (รหัส {r.status_code})")


# --- settings -----------------------------------------------------------------------------------

def set_setting(session: Session, key: str, value: str | None) -> None:
    row = session.get(AppSetting, key)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def _flag(key: str, default: bool, session: Session | None = None) -> bool:
    v = runtime_config.get_setting(key, session)
    return default if v is None else v == "1"


def get_config(session: Session | None = None) -> dict:
    """The effective settings. token is the decrypted bot token, or None when unset or it cannot be decrypted."""
    token = None
    blob = runtime_config.get_setting(TOKEN_ENC, session)
    if blob:
        try:
            token = secretbox.decrypt(blob.encode())
        except Exception as e:
            log.warning("telegram token cannot be decrypted (%s)", type(e).__name__)
    return {"enabled": _flag(ENABLED, False, session), "token": token, "chat_id": runtime_config.get_setting(CHAT_ID, session) or "",
            "station": _flag(NOTIFY_STATION, True, session), "round": _flag(NOTIFY_ROUND, True, session),
            "only_abnormal": _flag(ONLY_ABNORMAL, False, session)}


# --- message ------------------------------------------------------------------------------------

def public_url(round_id: int) -> str | None:
    base = (os.getenv("NEXXA_PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    return f"{base}/history/{round_id}" if base.startswith(("http://", "https://")) else None


def found_count(facts: dict) -> int:
    return facts["counts"][stats.YELLOW] + facts["counts"][stats.BROWN]


def build_message(row: Summary) -> str:
    """Plain Thai text: only ใบเหลือง / จุดน้ำตาล are listed (never a normal count), then the comparison, the trend and the text."""
    f = row.facts
    delta = f["baseline"]["delta_pp"]
    compare = "เทียบ 7 วันก่อน: " + ("เทียบไม่ได้" if delta is None else summaries._change(delta, "รอบนี้"))
    if f["scope"] == "round":
        title = f"NEXXA สรุปทั้งรอบ: รอบที่ {row.round_id}"
        where = None
        extra = ["สถานีที่แนวโน้ม 14 วันเพิ่มขึ้น: " + ", ".join(f["rising"])] if f["rising"] else []
        if f["worst"]:
            extra.insert(0, f"สถานีที่ผิดปกติสูงสุด: {f['worst']['station']}")
    else:
        title = f"NEXXA สรุปสถานี {f['station']} (รอบที่ {row.round_id})"
        where = summaries._affected(f["angles"])
        extra = [f"แนวโน้ม 14 วัน: {f['trend']['label']}"]
    found = summaries._findings(f["counts"], where) if found_count(f) else "ไม่พบใบเหลืองหรือจุดน้ำตาล"
    lines = [title, ("พบ " if found_count(f) else "") + found, compare, *extra, "", row.text.strip()[:2500]]  # the link below must survive the cap
    if link := public_url(row.round_id):
        lines += ["", f"ดูรายละเอียด: {link}"]
    return "\n".join(lines)[:MAX_CHARS]


# --- background notifier --------------------------------------------------------------------------

_sends: deque[float] = deque()
_sends_lock = threading.Lock()


def _allow_send() -> bool:
    """At most MAX_SENDS_PER_MIN sends per minute in this process."""
    with _sends_lock:
        now = time.monotonic()
        while _sends and now - _sends[0] >= 60:
            _sends.popleft()
        if len(_sends) >= MAX_SENDS_PER_MIN:
            return False
        _sends.append(now)
        return True


def _notify(session: Session, round_id: int, station_id: int | None) -> None:
    cfg = get_config(session)
    if not (cfg["enabled"] and cfg["token"] and cfg["chat_id"]):
        return
    same = Summary.station_id.is_(None) if station_id is None else Summary.station_id == station_id
    row = session.scalars(select(Summary).where(Summary.round_id == round_id, same).order_by(Summary.id)).first()
    if row is None or row.notified_at is not None or not cfg["round" if station_id is None else "station"]:
        return
    if cfg["only_abnormal"] and not found_count(row.facts):
        return
    # claim before sending: a repeated robot call (or a second worker) sees notified_at and does nothing
    claimed = session.execute(update(Summary).where(Summary.id == row.id, Summary.notified_at.is_(None))
                              .values(notified_at=datetime.now(timezone.utc))).rowcount
    session.commit()
    if claimed != 1:
        return
    if not _allow_send():
        log.warning("telegram notification dropped: more than %s per minute", MAX_SENDS_PER_MIN)
        return
    text = build_message(row)
    ok, reason = send_message(cfg["token"], cfg["chat_id"], text)
    log.info("telegram notification %s (%s)", "sent" if ok else "failed", reason)


def notify(round_id: int, station_id: int | None) -> None:
    """Background entry point (own Session): tell Telegram about the station summary, or the round summary when station_id is None."""
    try:
        with Session(app.db.engine) as s:
            _notify(s, round_id, station_id)
    except Exception as e:
        log.warning("telegram notify failed: %s", type(e).__name__)
