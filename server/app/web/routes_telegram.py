"""Admin page for the Telegram notifications (admin role only: the 'setting' resource).

The bot token is write-only: it is Fernet-encrypted (secretbox), shown only as its last 4 characters, never returned in
a page, a flash message or an audit row. Without NEXXA_SECRET_KEY nothing can be stored.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import runtime_config, telegram
from app.db import get_session
from app.models import AdminUser, AppSetting
from app.web import audit, deps, secretbox
from app.web.routes_admin import CSRF, _done, _fields
from app.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
READ, UPDATE = Depends(deps.require_permission("setting", "read")), Depends(deps.require_permission("setting", "update"))
FLAGS = {"enabled": telegram.ENABLED, "notify_station": telegram.NOTIFY_STATION, "notify_round": telegram.NOTIFY_ROUND,
         "only_abnormal": telegram.ONLY_ABNORMAL}
TEST_TEXT = "NEXXA: ข้อความทดสอบจากหน้าตั้งค่า Telegram ถ้าเห็นข้อความนี้แสดงว่าการแจ้งเตือนพร้อมใช้งาน"


def _page(request: Request, session: Session, values: dict | None = None, errors: dict | None = None, status_code: int = 200):
    cfg = telegram.get_config(session)
    last4 = runtime_config.get_setting(telegram.TOKEN_LAST4, session)
    token = "none" if not last4 else "ok" if cfg["token"] else "broken"  # broken: stored but this server key cannot open it
    values = values or {"chat_id": cfg["chat_id"], "enabled": cfg["enabled"], "notify_station": cfg["station"],
                        "notify_round": cfg["round"], "only_abnormal": cfg["only_abnormal"]}
    return render(request, "admin/telegram.html", status_code=status_code, values=values, errors=errors or {}, token=token,
                  last4=last4 or "", secrets_on=secretbox.secrets_enabled(), mask=secretbox.mask)


@router.get("/telegram", dependencies=[READ])
def telegram_page(request: Request, session: Session = Depends(get_session)):
    return _page(request, session)


@router.post("/telegram", dependencies=[*CSRF, UPDATE])
def telegram_save(request: Request, f: dict = _fields("token", "chat_id", *FLAGS), admin: AdminUser = Depends(deps.require_permission("setting", "update")),
                  session: Session = Depends(get_session)):
    values = {"chat_id": f["chat_id"].strip(), **{k: f[k] == "1" for k in FLAGS}}  # the token is never echoed back
    if not secretbox.secrets_enabled():
        return _page(request, session, values, {}, status_code=409)
    e: dict[str, str] = {}
    token, chat_id = f["token"].strip(), values["chat_id"]
    if token and not telegram.TOKEN_RE.fullmatch(token):
        e["token"] = "รูปแบบโทเค็นไม่ถูกต้อง ต้องเป็นแบบ 123456789:AA... ที่ได้จาก @BotFather"
    if chat_id and not telegram.CHAT_ID_RE.fullmatch(chat_id):
        e["chat_id"] = "Chat ID ต้องเป็นตัวเลข (เช่น 123456789 หรือ -1001234567890) หรือ @ชื่อช่อง"
    stored = session.get(AppSetting, telegram.TOKEN_ENC) is not None
    if values["enabled"] and not e:
        if not (token or stored):
            e["token"] = "ต้องใส่โทเค็นบอทก่อนเปิดใช้งาน"
        if not chat_id:
            e["chat_id"] = "ต้องใส่ Chat ID ก่อนเปิดใช้งาน"
    if e:
        return _page(request, session, values, e, status_code=422)
    changed = []
    if token:  # an empty token field keeps the stored one
        telegram.set_setting(session, telegram.TOKEN_ENC, secretbox.encrypt(token).decode())
        telegram.set_setting(session, telegram.TOKEN_LAST4, secretbox.last4(token))
        changed.append("token")
    telegram.set_setting(session, telegram.CHAT_ID, chat_id or None)
    for field, key in FLAGS.items():
        telegram.set_setting(session, key, "1" if values[field] else "0")
    audit.record(session, admin.username, "telegram.update", "telegram", "แก้ไข: " + ", ".join(["chat_id", *FLAGS, *changed]))  # names only, never values
    session.commit()
    return _done(request, "/admin/telegram", "บันทึกการตั้งค่า Telegram แล้ว")


@router.post("/telegram/token/delete", dependencies=[*CSRF, UPDATE])
def telegram_token_delete(request: Request, admin: AdminUser = Depends(deps.require_permission("setting", "update")),
                          session: Session = Depends(get_session)):
    for key in (telegram.TOKEN_ENC, telegram.TOKEN_LAST4):
        telegram.set_setting(session, key, None)
    telegram.set_setting(session, telegram.ENABLED, "0")  # no token, nothing to send with
    audit.record(session, admin.username, "telegram.update", "telegram", "ลบโทเค็น")
    session.commit()
    return _done(request, "/admin/telegram", "ลบโทเค็นแล้ว และปิดการแจ้งเตือนแล้ว")


@router.post("/telegram/test", dependencies=[*CSRF, UPDATE])
def telegram_test(request: Request, admin: AdminUser = Depends(deps.require_permission("setting", "update")),
                  session: Session = Depends(get_session)):
    cfg = telegram.get_config(session)
    if not (cfg["token"] and cfg["chat_id"]):
        ok, reason = False, "ยังไม่ได้บันทึกโทเค็นและ Chat ID (หรือถอดรหัสโทเค็นไม่ได้) บันทึกการตั้งค่าก่อนแล้วลองใหม่"
    else:
        ok, reason = telegram.send_message(cfg["token"], cfg["chat_id"], TEST_TEXT)
    audit.record(session, admin.username, "telegram.test", "telegram", "สำเร็จ" if ok else "ไม่สำเร็จ: " + reason)
    session.commit()
    response = RedirectResponse("/admin/telegram", status_code=303)
    deps.flash(request, response, "ส่งข้อความทดสอบแล้ว ตรวจดูใน Telegram" if ok else "ส่งข้อความทดสอบไม่สำเร็จ: " + reason, "success" if ok else "error")
    return response
