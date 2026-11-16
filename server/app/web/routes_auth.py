import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AdminUser, Batch, DeviceKey, ProviderKey, Station
from app.web import deps, secretbox, security
from app.web.templating import render

router = APIRouter()

throttle = security.LoginThrottle()  # shortcut: in-memory, per process (Ruling 19); move to the DB for multiple workers
_now = time.time
# verified against when the username is unknown, so both failure paths cost one scrypt
_DUMMY_HASH = security.hash_password(secrets.token_urlsafe(16))

BAD_LOGIN = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"
THROTTLED = "พยายามเข้าสู่ระบบผิดหลายครั้งเกินไป กรุณารอสักครู่ (ประมาณ 15 นาที) แล้วลองใหม่"
CSRF_EXPIRED = "แบบฟอร์มหมดอายุหรือไม่ถูกต้อง กรุณาลองเข้าสู่ระบบอีกครั้ง"
NO_SECRET = "ยังไม่ได้ตั้ง NEXXA_SESSION_SECRET หรือ NEXXA_SECRET_KEY บนเซิร์ฟเวอร์ จึงยังเข้าสู่ระบบไม่ได้"


def _login_page(request: Request, session: Session, *, next: str | None, error: str | None = None,
                username: str = "", status_code: int = 200):
    has_admin = session.scalar(select(func.count(AdminUser.id))) > 0
    return render(request, "login.html", status_code=status_code, next=deps.safe_next(next),
                  error=error, username=username, has_admin=has_admin, admin=None,
                  logged_out="logged_out" in request.query_params)


@router.get("/login")
def login_page(request: Request, next: str | None = None, session: Session = Depends(get_session)):
    if deps.current_admin(request, session) is not None:
        return RedirectResponse(deps.safe_next(next), status_code=303)
    return _login_page(request, session, next=next)


@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str | None = Form(None),
    session: Session = Depends(get_session),
):
    if not security.session_enabled():
        return _login_page(request, session, next=next, status_code=503)
    expected = (deps.session_data(request) or {}).get("csrf")
    if not isinstance(expected, str) or not security.csrf_ok(expected, csrf_token):
        return _login_page(request, session, next=next, error=CSRF_EXPIRED, username=username, status_code=403)

    # shortcut: request.client is the proxy's address behind a reverse proxy; run uvicorn with --proxy-headers there
    key = f"{request.client.host if request.client else '-'}|{username.strip().lower()}"
    now = _now()
    if not throttle.allowed(key, now):
        return _login_page(request, session, next=next, error=THROTTLED, username=username, status_code=429)

    user = session.scalar(select(AdminUser).where(AdminUser.username == username))
    ok = security.verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not ok:
        throttle.record_failure(key, now)
        return _login_page(request, session, next=next, error=BAD_LOGIN, username=username, status_code=401)

    throttle.reset(key)
    user.last_login_at = datetime.now(timezone.utc)
    session.commit()
    response = RedirectResponse(deps.safe_next(next), status_code=303)
    # fresh csrf on login: a token seen before authentication never authorises admin actions
    deps.write_session(response, request, {
        "uid": user.id, "csrf": security.new_csrf_token(),
        "flash": {"text": f"ยินดีต้อนรับ {user.username}", "kind": "success"},
    })
    return response


@router.post("/logout", dependencies=[Depends(deps.require_csrf)])
def logout():
    response = RedirectResponse("/login?logged_out=1", status_code=303)
    deps.clear_session(response)
    return response


@router.get("/admin")
def admin_home(request: Request, admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    count = session.scalar
    stats = {
        "stations": count(select(func.count(Station.id)).where(Station.kind != "home")),
        "stations_active": count(select(func.count(Station.id)).where(Station.kind != "home", Station.active)),
        "batches": count(select(func.count(Batch.id))),
        "batches_growing": count(select(func.count(Batch.id)).where(Batch.harvested_on.is_(None))),
        "ai_keys": count(select(func.count(ProviderKey.id))),
        "ai_keys_active": count(select(func.count(ProviderKey.id)).where(ProviderKey.active)),
        "devices": count(select(func.count(DeviceKey.id)).where(DeviceKey.revoked_at.is_(None))),
    }
    return render(request, "admin_home.html", stats=stats, secrets_enabled=secretbox.secrets_enabled())
