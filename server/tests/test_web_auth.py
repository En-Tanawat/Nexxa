import re
from html.parser import HTMLParser

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.create_admin import create_or_update
from app.db import get_session, init_db
from app.web import deps, routes_admin, routes_auth, security
from app.web.security import LoginThrottle

SECRET = "test-only-session-secret-0123456789"  # test-only value
PASSWORD = "correct-horse-battery"  # test-only value
BAD_LOGIN = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(eng)

    def override():
        with Session(eng) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    monkeypatch.setenv("NEXXA_SESSION_SECRET", SECRET)
    monkeypatch.delenv("NEXXA_SECRET_KEY", raising=False)
    monkeypatch.setattr(routes_auth, "throttle", LoginThrottle())
    yield eng
    main.app.dependency_overrides.clear()


@pytest.fixture
def admin(engine):
    with Session(engine) as s:
        create_or_update(s, "admin", PASSWORD)
    return "admin"


def _client(https=False):
    return TestClient(main.app, base_url="https://testserver" if https else "http://testserver", follow_redirects=False)


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf field in page"
    return m.group(1)


def _login(client, username="admin", password=PASSWORD, next_=None, csrf=None):
    page = client.get("/login", params={"next": next_} if next_ else None)
    data = {"username": username, "password": password, "csrf_token": csrf if csrf is not None else _csrf(page.text)}
    if next_ is not None:
        data["next"] = next_
    return client.post("/login", data=data)


def _set_cookie(resp) -> str:
    return "; ".join(v for k, v in resp.headers.multi_items() if k == "set-cookie")


# --- access control ---------------------------------------------------------

def test_admin_without_login_redirects_to_login(engine):
    r = _client().get("/admin")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/admin"


def test_require_admin_gives_401_on_api_paths(engine):
    api = FastAPI()

    @api.get("/api/admin/ping")
    def ping(a=Depends(deps.require_admin)):
        return {"ok": True}

    api.dependency_overrides = main.app.dependency_overrides
    r = TestClient(api).get("/api/admin/ping")
    assert r.status_code == 401


def test_require_csrf_keeps_form_for_endpoint_and_flash_shows_once(engine, admin):
    from fastapi import Form, Request
    from fastapi.responses import RedirectResponse

    api = FastAPI()
    api.include_router(routes_auth.router)
    api.include_router(routes_admin.router)

    @api.post("/admin/thing", dependencies=[Depends(deps.require_csrf)])
    def thing(request: Request, name: str = Form()):
        r = RedirectResponse("/admin", status_code=303)
        deps.flash(request, r, f"บันทึก {name} แล้ว")
        return r

    api.dependency_overrides = main.app.dependency_overrides
    c = TestClient(api, follow_redirects=False)
    _login(c)
    c.get("/admin")  # consume the login flash
    token = _csrf(c.get("/admin").text)
    assert c.post("/admin/thing", data={"name": "x"}).status_code == 403
    r = c.post("/admin/thing", data={"name": "<i>ผักบุ้ง</i>", "csrf_token": token})
    assert r.status_code == 303
    page = c.get("/admin").text
    assert "บันทึก &lt;i&gt;ผักบุ้ง&lt;/i&gt; แล้ว" in page
    assert "ผักบุ้ง" not in c.get("/admin").text


# --- login page --------------------------------------------------------------

def test_login_page_has_form_and_csrf(engine, admin):
    r = _client().get("/login")
    assert r.status_code == 200
    assert '<form' in r.text and 'action="/login"' in r.text
    assert len(_csrf(r.text)) >= 32
    assert "python -m app.create_admin" not in r.text


def test_login_page_hints_create_admin_when_no_admin(engine):
    r = _client().get("/login")
    assert r.status_code == 200
    assert "python -m app.create_admin" in r.text


def test_login_page_when_logged_in_goes_to_admin(engine, admin):
    c = _client()
    _login(c)
    r = c.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/admin"


# --- login ------------------------------------------------------------------

def test_login_success_sets_hardened_cookie(engine, admin):
    c = _client()
    r = _login(c)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = _set_cookie(r).lower()
    assert "nexxa_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age=" in cookie or "expires=" in cookie
    assert "secure" not in cookie
    home = c.get("/admin")
    assert home.status_code == 200
    assert "admin" in home.text


def test_login_cookie_secure_over_https(engine, admin):
    c = _client(https=True)
    r = _login(c)
    assert r.status_code == 303
    assert "secure" in _set_cookie(r).lower()
    assert c.get("/admin").status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(engine, admin):
    c = _client()
    wrong = _login(c, password="not-the-password")
    unknown = _login(_client(), username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert BAD_LOGIN in wrong.text and BAD_LOGIN in unknown.text
    assert c.get("/admin").status_code == 303


def test_unknown_user_still_runs_password_hash(engine, admin, monkeypatch):
    calls = []
    real = security.verify_password
    monkeypatch.setattr(routes_auth.security, "verify_password", lambda pw, h: calls.append(h) or real(pw, h))
    _login(_client(), username="nobody")
    assert len(calls) == 1


def test_throttle_blocks_sixth_attempt_even_with_right_password(engine, admin, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(routes_auth, "_now", lambda: now[0])
    c = _client()
    for _ in range(5):
        assert _login(c, password="wrong-password-x").status_code == 401
    assert _login(c, username="ADMIN").status_code == 429  # throttle key is lower-cased
    blocked = _login(c)
    assert blocked.status_code == 429
    assert "รอสักครู่" in blocked.text
    assert c.get("/admin").status_code == 303
    now[0] += 15 * 60 + 1
    assert _login(c).status_code == 303


def test_success_resets_throttle(engine, admin):
    c = _client()
    for _ in range(4):
        _login(c, password="wrong-password-x")
    assert _login(c).status_code == 303
    c2 = _client()
    for _ in range(4):
        _login(c2, password="wrong-password-x")
    assert _login(c2).status_code == 303


@pytest.mark.parametrize("csrf", ["", "forged-token-value"])
def test_login_without_valid_csrf_is_403(engine, admin, csrf):
    c = _client()
    r = _login(c, csrf=csrf)
    assert r.status_code == 403
    assert c.get("/admin").status_code == 303


# --- next / open redirect --------------------------------------------------

@pytest.mark.parametrize("bad", ["//evil.com", "https://evil.com", "/\\evil.com", "/\t/evil.com", "http:/evil.com", "javascript:alert(1)", "evil.com"])
def test_login_next_rejects_external(engine, admin, bad):
    r = _login(_client(), next_=bad)
    assert r.status_code == 303 and r.headers["location"] == "/admin"


def test_login_next_keeps_internal_path(engine, admin):
    r = _login(_client(), next_="/admin/stations")
    assert r.headers["location"] == "/admin/stations"


@pytest.mark.parametrize("value,expected", [
    (None, "/admin"), ("", "/admin"), ("/admin/keys?page=2", "/admin/keys?page=2"),
    ("//evil.com", "/admin"), ("https://evil.com/x", "/admin"), ("/\\evil.com", "/admin"),
    ("/%5Cevil.com", "/%5Cevil.com"), ("\\\\evil.com", "/admin"), ("/a\nb", "/admin"), (" /admin", "/admin"),
])
def test_safe_next(value, expected):
    assert deps.safe_next(value) == expected


# --- logout -----------------------------------------------------------------

def test_logout_requires_csrf(engine, admin):
    c = _client()
    _login(c)
    assert c.post("/logout").status_code == 403
    assert c.post("/logout", data={"csrf_token": "forged"}).status_code == 403
    assert c.get("/admin").status_code == 200


def test_logout_with_csrf_clears_session(engine, admin):
    c = _client()
    _login(c)
    token = _csrf(c.get("/admin").text)
    r = c.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "nexxa_session=" in _set_cookie(r) and "max-age=0" in _set_cookie(r).lower()
    assert c.get("/admin").status_code == 303


def test_csrf_header_is_accepted(engine, admin):
    c = _client()
    _login(c)
    token = _csrf(c.get("/admin").text)
    assert c.post("/logout", headers={"X-CSRF-Token": token}).status_code == 303


def test_csrf_rotates_on_login(engine, admin):
    c = _client()
    before = _csrf(c.get("/login").text)
    _login(c)
    assert _csrf(c.get("/admin").text) != before


# --- session secret / cookie integrity ----------------------------------------

def test_missing_secret_disables_login_without_500(engine, admin, monkeypatch):
    monkeypatch.delenv("NEXXA_SESSION_SECRET")
    c = _client()
    page = c.get("/login")
    assert page.status_code == 200
    assert "NEXXA_SESSION_SECRET" in page.text and "ยังไม่ได้ตั้ง" in page.text
    r = c.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": "x"})
    assert r.status_code == 503
    assert "nexxa_session" not in _set_cookie(r)
    assert c.get("/admin").status_code == 303


def test_secret_removed_after_login_means_logged_out(engine, admin, monkeypatch):
    c = _client()
    _login(c)
    monkeypatch.delenv("NEXXA_SESSION_SECRET")
    assert c.get("/admin").status_code == 303
    assert c.post("/logout", data={"csrf_token": "x"}).status_code == 403


def test_tampered_cookie_is_logged_out(engine, admin):
    c = _client()
    _login(c)
    value = c.cookies.get("nexxa_session")
    c.cookies.clear()
    c.cookies.set("nexxa_session", value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
    assert c.get("/admin").status_code == 303


def test_expired_cookie_is_logged_out(engine, admin):
    with Session(engine) as s:
        from app.models import AdminUser
        uid = s.query(AdminUser).one().id
    c = _client()
    c.cookies.set("nexxa_session", security.sign_session({"uid": uid, "csrf": "t" * 40}, max_age_s=10, now=0))
    assert c.get("/admin").status_code == 303


def test_cookie_for_deleted_admin_is_logged_out(engine, admin):
    c = _client()
    c.cookies.set("nexxa_session", security.sign_session({"uid": 999, "csrf": "t" * 40}, max_age_s=600))
    assert c.get("/admin").status_code == 303


def test_non_dict_session_payload_is_logged_out(engine, admin):
    c = _client()
    c.cookies.set("nexxa_session", security.sign_session(["uid", 1], max_age_s=600))
    assert c.get("/admin").status_code == 303
    assert c.get("/login").status_code == 200


# --- pages, flash, escaping ---------------------------------------------------

def test_admin_home_shows_counts_and_flash_once(engine, admin):
    c = _client()
    _login(c)
    first = c.get("/admin")
    assert "ยินดีต้อนรับ" in first.text
    for label in ("สถานี", "ล็อตผัก", "คีย์ AI", "อุปกรณ์"):
        assert label in first.text
    values = re.findall(r'class="stat__value">([^<]*)<', first.text)
    assert values == ["0", "0", "0", "0"]  # empty in-memory DB (no seed); guards against dict-method lookups like stats.keys
    assert "ยินดีต้อนรับ" not in c.get("/admin").text


def test_username_is_escaped(engine):
    with Session(engine) as s:
        create_or_update(s, "<b>x</b>", PASSWORD)
    c = _client()
    _login(c, username="<b>x</b>")
    html = c.get("/admin").text
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_static_css_served(engine):
    r = _client().get("/static/css/nexxa.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    assert "--color-accent" in r.text and "prefers-reduced-motion" in r.text
    assert _client().get("/static/js/app.js").status_code == 200


def test_security_headers_on_pages_not_on_device_api(engine, admin, monkeypatch):
    r = _client().get("/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "fonts.googleapis.com" in csp and "fonts.gstatic.com" in csp
    assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "referrer-policy" in r.headers
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "test-device-key")
    rounds = _client().post("/rounds", headers={"X-Device-Key": "test-device-key"})
    assert rounds.status_code == 200 and "content-security-policy" not in rounds.headers


class _A11y(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = None
        self.label_for, self.inputs, self.buttons = set(), [], []
        self._button = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        elif tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.inputs.append(a)
        elif tag == "button":
            self._button = [a.get("aria-label") or ""]

    def handle_data(self, data):
        if self._button is not None:
            self._button.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None:
            self.buttons.append("".join(self._button))
            self._button = None


@pytest.mark.parametrize("path", ["/login", "/admin"])
def test_pages_are_thai_and_accessible(engine, admin, path):
    c = _client()
    if path == "/admin":
        _login(c)
    r = c.get(path)
    assert r.status_code == 200
    p = _A11y()
    p.feed(r.text)
    assert p.lang == "th"
    for inp in p.inputs:
        assert inp.get("id") in p.label_for, f"input without label: {inp}"
    assert p.buttons and all(p.buttons), f"button without text: {p.buttons}"
    assert re.search(r"[\U0001F300-\U0001FAFF☀-➿]", r.text) is None  # no emoji icons
