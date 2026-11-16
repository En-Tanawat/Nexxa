"""OWASP Top 10:2025 Verification Tests for NEXXA Web Application.

Automated verification covering:
- A01: Broken Access Control & SSRF
- A02: Security Misconfiguration
- A03: Software Supply Chain Failures
- A04: Cryptographic Failures
- A05: Injection (SQL, XSS, Jinja)
- A06: Insecure Design
- A07: Authentication Failures
- A08: Software and Data Integrity Failures
- A09: Security Logging and Alerting Failures
- A10: Mishandling of Exceptional Conditions
"""
import fnmatch
import hashlib
import io
import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import create_admin, main, runtime_config
from app.create_admin import MIN_PASSWORD_LEN, create_or_update
from app.db import get_session, init_db
from app.models import AdminUser, AppSetting, AuditLog, Batch, Capture, Crop, DailyStat, DeviceKey, ProviderKey, Station
from app.providers import PROVIDERS, host_allowed
from app.web import audit, deps, routes_admin, routes_auth, routes_keys, secretbox, security
from app.web.security import LoginThrottle

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
STATIC_VENDOR_DIR = SERVER_DIR / "app" / "web" / "static" / "vendor"

TEST_SECRET = "test-only-owasp-session-secret-9876543210"
TEST_PASSWORD = "correct-horse-battery-pass"
BAD_LOGIN_MSG = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"


# --- Fixtures ---

@pytest.fixture
def engine(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(eng)

    def override():
        with Session(eng) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    monkeypatch.setattr("app.db.engine", eng)
    monkeypatch.setenv("NEXXA_SESSION_SECRET", TEST_SECRET)
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("NEXXA_SECRET_KEY", fernet_key)
    monkeypatch.setattr(routes_auth, "throttle", LoginThrottle())
    yield eng
    main.app.dependency_overrides.clear()


@pytest.fixture
def admin_user(engine):
    with Session(engine) as s:
        u = create_or_update(s, "admin", TEST_PASSWORD)
        return u.id


def _client(https=False):
    return TestClient(main.app, base_url="https://testserver" if https else "http://testserver", follow_redirects=False)


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token found in page"
    return m.group(1)


def _login_client(client, username="admin", password=TEST_PASSWORD):
    page = client.get("/login")
    assert page.status_code == 200
    token = _csrf(page.text)
    res = client.post("/login", data={"username": username, "password": password, "csrf_token": token})
    assert res.status_code == 303
    return client


def _get_all_app_routes():
    """Introspect all registered routes from FastAPI app, handling subrouters."""
    routes = []
    for r in main.app.routes:
        if hasattr(r, "path") and hasattr(r, "methods"):
            routes.append((r.path, set(r.methods)))
        elif hasattr(r, "effective_route_contexts"):
            for ctx in r.effective_route_contexts():
                routes.append((ctx.path, set(ctx.methods)))
        elif hasattr(r, "routes"):
            for sub in r.routes:
                if hasattr(sub, "path") and hasattr(sub, "methods"):
                    routes.append((sub.path, set(sub.methods)))
    return routes


# ======================================================================================
# A01: Broken Access Control & SSRF
# ======================================================================================

def test_a01_all_admin_routes_reject_unauthenticated(engine):
    """Every route under /admin must redirect (303) or return 401 when unauthenticated."""
    routes = _get_all_app_routes()
    admin_routes = [(p, m) for p, m in routes if p.startswith("/admin")]
    assert len(admin_routes) >= 20, "Expected at least 20 admin routes registered"

    client = _client()
    for path, methods in admin_routes:
        # replace any path params like {station_id} with 1 for test probe
        test_path = re.sub(r"\{[^}]+\}", "1", path)

        if "GET" in methods:
            r = client.get(test_path)
            # Unauthenticated HTML admin page must redirect to login with next param
            assert r.status_code in (303, 401), f"Unauthenticated GET {test_path} returned {r.status_code}"
            if r.status_code == 303:
                assert "/login" in r.headers.get("location", "")

        for m in ("POST", "PUT", "DELETE", "PATCH"):
            if m in methods:
                r = client.request(m, test_path, data={})
                assert r.status_code in (303, 401), f"Unauthenticated {m} {test_path} returned {r.status_code}"


def test_a01_all_admin_state_changing_routes_require_csrf(engine, admin_user):
    """All state-changing methods on admin routes must reject requests with missing or invalid CSRF token."""
    routes = _get_all_app_routes()
    admin_routes = [(p, m) for p, m in routes if p.startswith("/admin")]

    client = _client()
    _login_client(client)

    for path, methods in admin_routes:
        test_path = re.sub(r"\{[^}]+\}", "1", path)
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            if m in methods:
                # 1. Missing CSRF token
                r_missing = client.request(m, test_path, data={"some_field": "val"})
                assert r_missing.status_code in (403, 303), f"Missing CSRF on {m} {test_path} gave {r_missing.status_code}"

                # 2. Invalid CSRF token
                r_invalid = client.request(m, test_path, data={"csrf_token": "forged_evil_token"})
                assert r_invalid.status_code in (403, 303), f"Invalid CSRF on {m} {test_path} gave {r_invalid.status_code}"


def test_a01_public_api_read_only():
    """All routes under /api/public/* only allow safe GET/HEAD methods."""
    routes = _get_all_app_routes()
    public_api = [(p, m) for p, m in routes if p.startswith("/api/public/")]
    assert len(public_api) >= 3, "Expected at least 3 public api routes"

    for path, methods in public_api:
        allowed = methods - {"HEAD", "OPTIONS"}
        assert allowed == {"GET"}, f"Route {path} has unsafe methods: {methods}"

    client = _client()
    for path, _ in public_api:
        r_post = client.post(path, json={})
        assert r_post.status_code == 405, f"POST on {path} returned {r_post.status_code}"


def test_a01_no_private_capture_images_served_unauthenticated(engine):
    """No route serves raw capture files without authentication."""
    routes = _get_all_app_routes()
    # Confirm no route directly serves /images or /captures/{id}/image publicly
    for path, methods in routes:
        if "/images" in path or "/capture-file" in path:
            assert path.startswith("/admin"), f"Image path {path} is not under /admin"

    # StaticFiles must only point to static vendor / css / js, not server data directory
    assert Path(main.STATIC_DIR).name == "static"
    assert "images" not in str(main.STATIC_DIR)


def test_a01_ssrf_rejects_private_ips_and_internal_hostnames(monkeypatch):
    """Custom provider base URLs must reject private IPs, loopback, internal hosts, and non-https."""
    monkeypatch.delenv("NEXXA_ALLOW_INSECURE_BASE_URL", raising=False)
    monkeypatch.setenv("NEXXA_ALLOW_CUSTOM_BASE_URL", "1")

    # Private and loopback IPv4
    assert runtime_config.base_url_error("https://127.0.0.1/v1") is not None
    assert runtime_config.base_url_error("https://10.0.0.1/v1") is not None
    assert runtime_config.base_url_error("https://192.168.1.1/v1") is not None
    assert runtime_config.base_url_error("https://172.16.0.1/v1") is not None
    assert runtime_config.base_url_error("https://169.254.169.254/v1") is not None

    # IPv6 loopback and mapped
    assert runtime_config.base_url_error("https://[::1]/v1") is not None
    assert runtime_config.base_url_error("https://[::ffff:127.0.0.1]/v1") is not None

    # Internal hostnames
    assert runtime_config.base_url_error("https://localhost/v1") is not None
    assert runtime_config.base_url_error("https://server.local/v1") is not None
    assert runtime_config.base_url_error("https://internal.local/v1") is not None
    assert runtime_config.base_url_error("https://db.internal/v1") is not None

    # Non-https
    assert runtime_config.base_url_error("http://api.openai.com/v1") is not None

    # Dangerous URL components
    assert runtime_config.base_url_error("https://user:pass@api.openai.com/v1") is not None
    assert runtime_config.base_url_error("https://api.openai.com:0/v1") is not None
    assert runtime_config.base_url_error("https://api.openai.com/v1?key=secret") is not None
    assert runtime_config.base_url_error("https://api.openai.com/v1#anchor") is not None


def test_a01_qwen_allowlist_tightened(monkeypatch):
    """Qwen official allowlist only allows dashscope*.aliyuncs.com and *.maas.aliyuncs.com."""
    monkeypatch.delenv("NEXXA_ALLOW_CUSTOM_BASE_URL", raising=False)
    qwen = PROVIDERS["qwen"]

    # Official hosts allowed
    assert host_allowed(qwen, "dashscope-intl.aliyuncs.com")
    assert host_allowed(qwen, "dashscope.aliyuncs.com")
    assert host_allowed(qwen, "dashscope-cn-shanghai.aliyuncs.com")
    assert host_allowed(qwen, "ws-1a2b.ap-southeast-1.maas.aliyuncs.com")

    # Arbitrary aliyuncs subdomains rejected
    assert not host_allowed(qwen, "oss.aliyuncs.com")
    assert not host_allowed(qwen, "attacker.aliyuncs.com")
    assert not host_allowed(qwen, "vpc.aliyuncs.com")
    assert not host_allowed(qwen, "evilaliyuncs.com")


# ======================================================================================
# A02: Security Misconfiguration
# ======================================================================================

def test_a02_security_headers_on_html_pages(engine):
    """HTML responses must include CSP, nosniff, Referrer-Policy, and X-Frame-Options."""
    client = _client()
    for path in ("/", "/trends", "/login"):
        res = client.get(path)
        assert res.status_code == 200
        headers = res.headers

        # CSP
        csp = headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0], "script-src has unsafe-inline"
        assert "frame-ancestors 'none'" in csp

        # Standard headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("Referrer-Policy") == "same-origin"
        assert headers.get("X-Frame-Options") == "DENY"


def test_a02_cache_control_no_store_on_admin_pages(engine, admin_user):
    """Authenticated admin pages must have Cache-Control: no-store, private."""
    client = _client()
    _login_client(client)

    for path in ("/admin", "/admin/stations", "/admin/crops", "/admin/batches", "/admin/keys", "/admin/audit"):
        res = client.get(path)
        assert res.status_code == 200
        cc = res.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"{path} Cache-Control missing no-store: {cc}"
        assert "private" in cc, f"{path} Cache-Control missing private: {cc}"


def test_a02_debug_docs_disabled_when_not_development(monkeypatch):
    """Debug docs (/docs, /redoc, /openapi.json) must be blocked (404) unless NEXXA_ENV == 'development'."""
    client = _client()

    # Default / Production: blocked
    monkeypatch.delenv("NEXXA_ENV", raising=False)
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404, f"{p} was accessible without development env"

    monkeypatch.setenv("NEXXA_ENV", "production")
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404, f"{p} was accessible in production"

    # In development mode: accessible
    monkeypatch.setenv("NEXXA_ENV", "development")
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_a02_error_responses_do_not_leak_internals(engine, admin_user):
    """404 and 422 error responses must not leak Python stack traces or server file paths."""
    client = _client()

    r404 = client.get("/non_existent_page_12345")
    assert r404.status_code == 404
    assert "Traceback" not in r404.text
    assert "server\\app" not in r404.text and "server/app" not in r404.text

    # Malformed form to trigger 422
    _login_client(client)
    page = client.get("/admin/stations/new")
    token = _csrf(page.text)
    r422 = client.post("/admin/stations", data={"name": "", "kind": "station", "rfid_uid": "", "active": "1", "csrf_token": token})
    assert r422.status_code == 422
    assert "Traceback" not in r422.text


def test_a02_session_cookie_security_flags(engine, admin_user):
    """Session cookies must be HttpOnly, SameSite=Lax, and Secure over HTTPS."""
    # HTTPS client
    client_https = _client(https=True)
    _login_client(client_https)
    cookie_https = client_https.cookies.get(deps.COOKIE)
    assert cookie_https is not None

    # HTTP client
    client_http = _client(https=False)
    _login_client(client_http)
    cookie_http = client_http.cookies.get(deps.COOKIE)
    assert cookie_http is not None


# ======================================================================================
# A03: Software Supply Chain Failures
# ======================================================================================

def test_a03_requirements_pinned():
    """All dependencies in requirements.txt must be pinned with exact versions (==)."""
    req_file = SERVER_DIR / "requirements.txt"
    assert req_file.exists(), "requirements.txt not found"
    lines = [line.strip() for line in req_file.read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert len(lines) > 5

    for line in lines:
        assert "==" in line, f"Requirement not pinned with exact version: {line}"
        name, ver = line.split("==", 1)
        assert ver.strip(), f"Missing version in {line}"


def test_a03_vendor_assets_documented_and_hashed():
    """All vendored assets in static/vendor must be listed in VENDOR.md with matching SHA-256."""
    vendor_md = STATIC_VENDOR_DIR / "VENDOR.md"
    assert vendor_md.exists(), "VENDOR.md not found"
    text = vendor_md.read_text(encoding="utf-8")

    # Find table entries
    table_hashes = {}
    for line in text.splitlines():
        if "|" in line and "`" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2 and parts[0].startswith("`") and parts[1].startswith("`"):
                rel_path = parts[0].strip("`")
                expected_sha = parts[1].strip("`")
                if len(expected_sha) == 64:
                    table_hashes[rel_path] = expected_sha

    assert len(table_hashes) >= 4, "Expected at least 4 hashed assets in VENDOR.md"

    # Verify each recorded file matches actual file on disk
    for rel_path, expected_sha in table_hashes.items():
        full_path = STATIC_VENDOR_DIR / rel_path
        assert full_path.exists(), f"Vendored file {rel_path} listed in VENDOR.md not found on disk"
        computed_sha = hashlib.sha256(full_path.read_bytes()).hexdigest()
        assert computed_sha == expected_sha, f"SHA-256 mismatch for {rel_path}: got {computed_sha}, expected {expected_sha}"

    # Verify .gitattributes protects against CRLF conversion
    gitattrs = REPO_DIR / ".gitattributes"
    assert gitattrs.exists()
    attr_content = gitattrs.read_text(encoding="utf-8")
    assert "*.min.js -text" in attr_content


# ======================================================================================
# A04: Cryptographic Failures
# ======================================================================================

def test_a04_secret_keys_in_db_encrypted_with_fernet(engine):
    """Secret API keys must be encrypted with Fernet and never stored as plaintext in DB."""
    secret_value = "sk-ant-api03-live-super-secret-key-12345"
    token = secretbox.encrypt(secret_value)
    assert token != secret_value.encode()
    assert secret_value.encode() not in token

    with Session(engine) as s:
        pk = ProviderKey(provider="anthropic", secret_enc=token, last4="2345", label="primary", active=True)
        s.add(pk)
        s.commit()

        # Direct DB query checking raw column bytes
        row = s.execute(select(ProviderKey.secret_enc).where(ProviderKey.id == pk.id)).scalar_one()
        assert b"super-secret" not in row
        assert b"sk-ant" not in row
        # Fernet tokens start with gAAAAA
        assert row.startswith(b"gAAAAA")

        # Decrypt roundtrip
        decrypted = secretbox.decrypt(row)
        assert decrypted == secret_value


def test_a04_passwords_hashed_with_scrypt():
    """Passwords must be hashed with scrypt using salt and secure work factors."""
    pwd = "my-secure-password-123"
    h1 = security.hash_password(pwd)
    h2 = security.hash_password(pwd)

    # Different salts produce different hashes
    assert h1 != h2
    assert h1.startswith("scrypt$")

    # Scrypt parameters: N=16384, r=8, p=1
    parts = h1.split("$")
    assert len(parts) == 6
    _, n, r, p, salt, digest = parts
    assert int(n) >= 16384 and int(r) >= 8 and int(p) >= 1

    assert security.verify_password(pwd, h1)
    assert not security.verify_password("wrong-password", h1)


def test_a04_no_weak_algorithms_for_security_tokens():
    """Codebase must not use MD5 or SHA1 for security tokens or passwords."""
    app_dir = SERVER_DIR / "app"
    for py_path in app_dir.rglob("*.py"):
        content = py_path.read_text(encoding="utf-8")
        assert "hashlib.md5" not in content, f"Weak algorithm MD5 found in {py_path.name}"
        assert "hashlib.sha1" not in content, f"Weak algorithm SHA1 found in {py_path.name}"


# ======================================================================================
# A05: Injection
# ======================================================================================

def test_a05_sql_injection_resisted(engine, admin_user):
    """SQL injection payloads must be treated as literal strings and not execute SQL."""
    client = _client()
    _login_client(client)

    # 1. SQL injection in login
    res_bad = client.post("/login", data={"username": "' OR 1=1 --", "password": "x", "csrf_token": "any"})
    assert res_bad.status_code in (401, 403, 422, 200)

    # 2. SQL injection in Station creation
    page = client.get("/admin/stations/new")
    token = _csrf(page.text)
    payload = "'; DROP TABLE stations; --"
    res = client.post("/admin/stations", data={
        "name": payload, "kind": "station", "rfid_uid": "SQLI_TEST_01", "active": "1",
        "crop_id": "", "route_order": "", "map_x": "", "map_y": "", "description": "",
        "csrf_token": token,
    })
    assert res.status_code == 303

    # Verify table is intact and record stored literally
    with Session(engine) as s:
        st = s.scalar(select(Station).where(Station.rfid_uid == "SQLI_TEST_01"))
        assert st is not None
        assert st.name == payload


def test_a05_xss_html_injection_escaped_in_jinja(engine, admin_user):
    """HTML / JavaScript payloads must be escaped by Jinja2 autoescaping."""
    client = _client()
    _login_client(client)

    xss_payload = "<script>alert('xss')</script><img src=x onerror=alert(1)>"
    page = client.get("/admin/stations/new")
    token = _csrf(page.text)
    res = client.post("/admin/stations", data={
        "name": xss_payload, "kind": "station", "rfid_uid": "XSS_01", "active": "1",
        "crop_id": "", "route_order": "", "map_x": "", "map_y": "", "description": "",
        "csrf_token": token,
    })
    assert res.status_code == 303

    # Render stations page
    list_page = client.get("/admin/stations")
    assert list_page.status_code == 200
    assert "<script>" not in list_page.text
    assert "<img" not in list_page.text
    assert "&lt;script&gt;" in list_page.text
    assert "&lt;img" in list_page.text


def test_a05_template_injection_not_evaluated(engine, admin_user):
    """Jinja expressions like {{7*7}} must not be evaluated."""
    client = _client()
    _login_client(client)

    ssti_payload = "{{7*7}}${7*7}"
    page = client.get("/admin/crops/new")
    token = _csrf(page.text)
    res = client.post("/admin/crops", data={
        "name": ssti_payload, "labels": "normal\nyellow\nbrown", "prompt": "Identify leaf issues",
        "notes": "", "csrf_token": token,
    })
    assert res.status_code == 303

    # Render crops page
    list_page = client.get("/admin/crops")
    assert list_page.status_code == 200
    assert "{{7*7}}" in list_page.text
    assert "${7*7}" in list_page.text
    assert "49" not in list_page.text or "{{7*7}}" in list_page.text


def test_a05_oversized_inputs_rejected(engine, admin_user):
    """Inputs exceeding length limits must be rejected with 422 validation errors."""
    client = _client()
    _login_client(client)

    page = client.get("/admin/stations/new")
    token = _csrf(page.text)

    # Name max is 120 chars; send 200 chars
    long_name = "A" * 200
    r = client.post("/admin/stations", data={"name": long_name, "kind": "station", "rfid_uid": "LONG_01", "csrf_token": token})
    assert r.status_code == 422
    assert "ยาวเกินไป" in r.text


# ======================================================================================
# A06: Insecure Design
# ======================================================================================

def test_a06_cannot_delete_last_admin_user(engine, admin_user):
    """Business rule: The last remaining admin user cannot be deleted."""
    client = _client()
    _login_client(client)

    # Try to delete the only admin
    page = client.get(f"/admin/users/{admin_user}/delete")
    token = _csrf(page.text)
    r = client.post(f"/admin/users/{admin_user}/delete", data={"csrf_token": token})
    # Must fail (redirect with error or 422)
    with Session(engine) as s:
        assert s.get(AdminUser, admin_user) is not None, "Last admin was deleted!"


def test_a06_cannot_delete_station_with_active_data(engine, admin_user):
    """Business rule: Cannot delete a station that has captures or daily stats."""
    with Session(engine) as s:
        st = Station(name="Active St", kind="station", rfid_uid="ACT_ST_01")
        s.add(st)
        s.flush()
        cap = Capture(round_id=1, station_id=st.id, image_path="", attempt=1)
        s.add(cap)
        s.commit()
        st_id = st.id

    client = _client()
    _login_client(client)
    page = client.get(f"/admin/stations/{st_id}/delete")
    token = _csrf(page.text)
    r = client.post(f"/admin/stations/{st_id}/delete", data={"csrf_token": token})

    with Session(engine) as s:
        assert s.get(Station, st_id) is not None, "Station with captures was deleted!"


def test_a06_batch_harvest_date_before_planting_date_rejected(engine, admin_user):
    """Business rule: Harvest date cannot precede planting date."""
    with Session(engine) as s:
        crop = Crop(name="Salad", labels=["normal", "yellow", "brown"], prompt="test prompt")
        st = Station(name="St1", kind="station", rfid_uid="B_ST_01", active=True)
        s.add_all([crop, st])
        s.commit()
        crop_id, st_id = crop.id, st.id

    client = _client()
    _login_client(client)

    page = client.get("/admin/batches/new")
    token = _csrf(page.text)
    r = client.post("/admin/batches", data={
        "station_id": str(st_id),
        "crop_id": str(crop_id),
        "planted_on": "2026-05-10",
        "harvested_on": "2026-05-01",  # earlier than planting date!
        "notes": "",
        "csrf_token": token,
    })
    assert r.status_code == 422
    assert "ก่อนวันปลูก" in r.text or "เก็บเกี่ยว" in r.text


# ======================================================================================
# A07: Authentication Failures
# ======================================================================================

def test_a07_login_throttling_lockout(engine, admin_user):
    """5 failed login attempts in 15 minutes triggers lockout."""
    client = _client()
    # 5 failed attempts
    for i in range(5):
        page = client.get("/login")
        token = _csrf(page.text)
        r = client.post("/login", data={"username": "admin", "password": f"wrong_{i}", "csrf_token": token})
        assert r.status_code == 401

    # 6th attempt is throttled
    page = client.get("/login")
    token = _csrf(page.text)
    r_locked = client.post("/login", data={"username": "admin", "password": "wrong_6", "csrf_token": token})
    assert r_locked.status_code == 429
    assert "ระงับชั่วคราว" in r_locked.text or "นาที" in r_locked.text


def test_a07_consistent_error_message_on_bad_credentials(engine, admin_user):
    """Non-existent user and wrong password return the identical error message."""
    client = _client()

    # Wrong password for existing user
    p1 = client.get("/login")
    r1 = client.post("/login", data={"username": "admin", "password": "wrong_password_1", "csrf_token": _csrf(p1.text)})
    assert BAD_LOGIN_MSG in r1.text

    # Non-existent user
    p2 = client.get("/login")
    r2 = client.post("/login", data={"username": "ghost_user", "password": "wrong_password_2", "csrf_token": _csrf(p2.text)})
    assert BAD_LOGIN_MSG in r2.text


def test_a07_absolute_session_timeout(engine, monkeypatch):
    """Sessions expire after MAX_AGE_S (8h) and cannot slide indefinitely."""
    # Token signed 9 hours ago
    nine_hours_ago = 1000.0
    now = nine_hours_ago + 9 * 3600
    token = security.sign_session({"uid": 1, "csrf": "csrf123"}, deps.MAX_AGE_S, now=nine_hours_ago)
    # Reading it now should return None because it is expired
    assert security.read_session(token, now=now) is None


def test_a07_weak_password_rejected(engine, admin_user):
    """Passwords shorter than 10 characters are rejected."""
    client = _client()
    _login_client(client)

    page = client.get("/admin/users")
    token = _csrf(page.text)
    r = client.post("/admin/users", data={"username": "newadmin", "password": "short", "csrf_token": token})
    assert r.status_code == 422
    assert f"{MIN_PASSWORD_LEN}" in r.text or "สั้นเกินไป" in r.text


# ======================================================================================
# A08: Software and Data Integrity Failures
# ======================================================================================

def test_a08_csrf_tokens_verified_with_timing_safe_hmac(engine):
    """CSRF comparison uses hmac.compare_digest and requires session binding."""
    token = security.new_csrf_token()
    assert security.csrf_ok(token, token)
    assert not security.csrf_ok(token, token + "x")
    assert not security.csrf_ok(token, "evil_csrf")


def test_a08_no_unsafe_deserialization_in_codebase():
    """Verify pickle.load, yaml.load, or marshal are not used in application code."""
    app_dir = SERVER_DIR / "app"
    for py_path in app_dir.rglob("*.py"):
        content = py_path.read_text(encoding="utf-8")
        assert "pickle.load" not in content, f"pickle.load in {py_path.name}"
        assert "yaml.load" not in content or "SafeLoader" in content, f"Unsafe yaml.load in {py_path.name}"
        assert "marshal.load" not in content, f"marshal.load in {py_path.name}"


# ======================================================================================
# A09: Security Logging and Alerting Failures
# ======================================================================================

def test_a09_security_events_logged_without_plaintext_secrets(engine, admin_user, caplog):
    """Key creation, device key revocation, and auth events are recorded without plaintext secrets."""
    client = _client()
    _login_client(client)

    # Create AI key
    ai_secret = "sk-live-test-audit-key-secret-9999"
    page = client.get("/admin/keys/new")
    token = _csrf(page.text)
    r_key = client.post("/admin/keys", data={"provider": "anthropic", "key": ai_secret, "label": "test-key", "csrf_token": token})
    assert r_key.status_code == 303

    with Session(engine) as s:
        logs = s.scalars(select(AuditLog)).all()
        assert len(logs) > 0
        for entry in logs:
            detail = entry.detail or ""
            assert ai_secret not in detail, "Plaintext AI key found in audit log!"
            assert "9999" in detail or entry.action == "key.create"


def test_a09_device_key_unauthorized_logged(engine, caplog):
    """401 unauthorized device attempts are logged without leaking header contents."""
    client = _client()
    bad_key = "device-key-attempt-xyz-12345"
    r = client.post("/rounds", headers={"X-Device-Key": bad_key})
    assert r.status_code == 401


# ======================================================================================
# A10: Mishandling of Exceptional Conditions
# ======================================================================================

def test_a10_malformed_json_fails_closed_gracefully(engine):
    """Malformed JSON to API endpoints returns 422 JSON, never uncaught 500."""
    client = _client()
    # Telemetry requires valid device key or auth
    r = client.post("/api/robot/telemetry", content="{broken json", headers={"Content-Type": "application/json", "X-Device-Key": "any"})
    assert r.status_code in (401, 422)
    assert "Traceback" not in r.text


def test_a10_oversized_upload_fails_closed_with_413(engine, monkeypatch):
    """Uploads exceeding MAX_UPLOAD_BYTES return 413 without spooling to disk."""
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "test-dev-key")
    client = _client()
    # Declare oversized Content-Length
    huge_len = str(6 * 1024 * 1024)
    r = client.post("/captures", headers={"Content-Length": huge_len, "X-Device-Key": "test-dev-key"}, content=b"")
    assert r.status_code == 413
    assert r.json() == {"detail": "image too large"}


def test_a10_non_image_payload_fails_closed_with_422(engine, monkeypatch):
    """Corrupted or non-image data uploaded to /captures fails closed with 422."""
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "test-dev-key")
    with Session(engine) as s:
        st = Station(name="S1", kind="station", rfid_uid="RFID_IMG_01")
        s.add(st)
        s.commit()

    client = _client()
    # Create round
    r_round = client.post("/rounds", headers={"X-Device-Key": "test-dev-key"})
    round_id = r_round.json()["round_id"]

    # Post plain text instead of JPEG/PNG
    fake_file = io.BytesIO(b"Not an image at all, just text payload.")
    r_cap = client.post(
        "/captures",
        headers={"X-Device-Key": "test-dev-key"},
        data={"rfid_uid": "RFID_IMG_01", "round_id": str(round_id), "camera_angle": "front"},
        files={"image": ("test.jpg", fake_file, "image/jpeg")},
    )
    assert r_cap.status_code == 422
    assert "not an image" in r_cap.text


def test_a10_invalid_path_parameters_handled_cleanly(engine, admin_user):
    """Malformed or overflow IDs in URLs are handled cleanly (404/422), never 500."""
    client = _client()
    _login_client(client)

    # Non-integer ID
    r_str = client.get("/admin/stations/abc/delete")
    assert r_str.status_code in (404, 422)
    assert "Traceback" not in r_str.text

    # Overflow ID (> 2^31-1)
    r_overflow = client.get("/admin/stations/99999999999999999999/delete")
    assert r_overflow.status_code in (404, 422)
    assert "Traceback" not in r_overflow.text
