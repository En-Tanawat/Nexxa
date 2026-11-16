"""Session cookie, current admin, CSRF and redirect helpers for the web UI."""
from urllib.parse import quote, urlsplit

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AdminUser
from app.web import security

COOKIE = "nexxa_session"
MAX_AGE_S = 8 * 60 * 60
DEFAULT_NEXT = "/admin"
FLASH_MAX = 200  # keeps the signed cookie far below the 4 KB browser limit even for long user-supplied names


class CsrfError(HTTPException):
    """A POST without a valid CSRF token; routes_auth renders an HTML page for it (JSON on /api/)."""


def session_data(request: Request) -> dict | None:
    """The signed cookie payload, or None when absent, invalid, expired or the secret is unset."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        data = security.read_session(token)
    except security.SessionSecretMissingError:
        return None
    return data if isinstance(data, dict) else None


def write_session(response: Response, request: Request, data: dict) -> None:
    response.set_cookie(
        COOKIE, security.sign_session(data, MAX_AGE_S), max_age=MAX_AGE_S, path="/",
        # scheme is "https" behind a TLS proxy only when uvicorn runs with --proxy-headers
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")


def flash(request: Request, response: Response, text: str, kind: str = "success") -> None:
    """Queue a one-shot toast shown by the next rendered page (kind: success | error | info)."""
    data = session_data(request) or {"csrf": security.new_csrf_token()}
    text = text if len(text) <= FLASH_MAX else text[:FLASH_MAX - 1] + "…"
    write_session(response, request, {**data, "flash": {"text": text, "kind": kind}})


def current_admin(request: Request, session: Session) -> AdminUser | None:
    uid = (session_data(request) or {}).get("uid")
    return session.get(AdminUser, uid) if isinstance(uid, int) else None


def _return_path(request: Request) -> str:
    """Where to come back to after login: the page itself for GET; for a POST, the same-origin form page it came from."""
    if request.method in ("GET", "HEAD"):
        return request.url.path + (f"?{request.url.query}" if request.url.query else "")
    ref = urlsplit(request.headers.get("referer", ""))
    if ref.netloc == request.url.netloc and ref.path.startswith("/"):
        return ref.path + (f"?{ref.query}" if ref.query else "")
    return DEFAULT_NEXT


def require_admin(request: Request, session: Session = Depends(get_session)) -> AdminUser:
    admin = current_admin(request, session)
    if admin is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, "not logged in")
        raise HTTPException(303, headers={"Location": "/login?next=" + quote(_return_path(request), safe="/")})
    request.state.admin = admin  # templating.render picks this up
    return admin


async def require_csrf(request: Request) -> None:
    submitted = request.headers.get("x-csrf-token")
    if submitted is None:
        submitted = (await request.form()).get("csrf_token")  # Starlette caches the parsed form for the endpoint
    expected = (session_data(request) or {}).get("csrf")
    if not isinstance(expected, str) or not isinstance(submitted, str) or not security.csrf_ok(expected, submitted):
        raise CsrfError(403, "CSRF token ไม่ถูกต้อง กรุณาโหลดหน้าใหม่แล้วลองอีกครั้ง")


def safe_next(next: str | None) -> str:
    """Same-site path only; anything that a browser could read as another host falls back to /admin."""
    if not next or not next.startswith("/") or next.startswith("//") or "\\" in next:
        return DEFAULT_NEXT
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in next):  # browsers strip tabs/newlines: "/\t/x" -> "//x"
        return DEFAULT_NEXT
    parts = urlsplit(next)
    return DEFAULT_NEXT if parts.scheme or parts.netloc else next
