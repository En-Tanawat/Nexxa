import dataclasses
import html
import json
import logging
import re
import secrets
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

import app.db
from app import analyzer, main, runtime_config, storage
from app.analyzer import AnalysisResult
from app.models import AppSetting, AuditLog, Crop, DeviceKey, ProviderKey, Station
from app.seed import seed
from app.web import routes_keys, secretbox, security
from tests.test_api import GOOD
from tests.test_web_auth import _A11y, _client, _csrf, _login, admin, engine  # noqa: F401

VISION_ENV = ("NEXXA_VISION_PROVIDER", "NEXXA_VISION_MODEL", "NEXXA_VISION_BASE_URL", "NEXXA_DEVICE_KEY",
              "NEXXA_ALLOW_INSECURE_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")


def fake_key(prefix="sk-FAKE-"):
    # long, odd-looking and generated per run: never a real key; includes HTML-special characters on purpose
    return prefix + secrets.token_urlsafe(40) + "~<&>'\"!" + secrets.token_hex(6)


@pytest.fixture
def secrets_on(engine, monkeypatch):
    monkeypatch.setenv("NEXXA_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(app.db, "engine", engine)  # runtime_config reads the test database
    for k in VISION_ENV:
        monkeypatch.delenv(k, raising=False)
    return engine


@pytest.fixture
def c(secrets_on, admin):
    client = _client()
    _login(client)
    client.csrf = _csrf(client.get("/admin").text)
    return client


@pytest.fixture
def c_nosecret(engine, admin, monkeypatch):
    monkeypatch.setattr(app.db, "engine", engine)
    for k in VISION_ENV:
        monkeypatch.delenv(k, raising=False)
    client = _client()
    _login(client)
    client.csrf = _csrf(client.get("/admin").text)
    return client


def post(client, path, **data):
    return client.post(path, data={"csrf_token": client.csrf, **data})


def rows(engine, model):
    with Session(engine) as s:
        return list(s.scalars(select(model).order_by(*inspect(model).primary_key)))


def count(engine, model):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(model))


def add_key(c, provider="openai", label="คีย์ทดลอง", key=None):
    key = key or fake_key()
    r = post(c, "/admin/keys", provider=provider, label=label, key=key)
    assert r.status_code == 303, r.text
    return key


def db_dump(engine) -> str:
    out = []
    with engine.connect() as conn:
        for table in app.db.Base.metadata.sorted_tables:
            for row in conn.execute(text(f"SELECT * FROM {table.name}")):
                out.append(repr(tuple(row)))
    return "\n".join(out)


def assert_no_leak(secret: str, *texts: str):
    needles = {secret, html.escape(secret), html.escape(secret, quote=False), secret[8:40]}
    for t in texts:
        for n in needles:
            assert n not in t, "secret leaked"


# --- access control -------------------------------------------------------------

GET_PATHS = ["/admin/keys", "/admin/keys/new", "/admin/keys/1/replace", "/admin/keys/1/delete",
             "/admin/devices", "/admin/ai"]
POST_PATHS = ["/admin/keys", "/admin/keys/1/toggle", "/admin/keys/1/replace", "/admin/keys/1/delete",
              "/admin/devices", "/admin/devices/1/revoke", "/admin/ai"]


@pytest.mark.parametrize("path", GET_PATHS)
def test_get_routes_need_login(secrets_on, path):
    r = _client().get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_routes_need_login(secrets_on, path):
    r = _client().post(path, data={"csrf_token": "x", "provider": "openai", "label": "x", "key": fake_key()})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert count(secrets_on, ProviderKey) == 0 and count(secrets_on, DeviceKey) == 0


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_routes_need_csrf(c, secrets_on, path):
    add_key(c)
    r = c.post(path, data={"provider": "openai", "label": "x", "key": fake_key(), "model": "m"})
    assert r.status_code == 403
    assert count(secrets_on, ProviderKey) == 1 and count(secrets_on, DeviceKey) == 0
    assert count(secrets_on, AppSetting) == 0


def test_session_for_unknown_user_cannot_see_keys(secrets_on, admin):
    client = _client()
    client.cookies.set("nexxa_session", security.sign_session({"uid": 999, "csrf": "t" * 40}, max_age_s=600))
    assert client.get("/admin/keys").status_code == 303
    r = client.post("/admin/keys", data={"csrf_token": "t" * 40, "provider": "openai", "label": "x", "key": fake_key()})
    assert r.status_code == 303 and count(secrets_on, ProviderKey) == 0


# --- provider keys ---------------------------------------------------------------

def test_create_key_encrypts_and_shows_only_last4(c, secrets_on):
    key = fake_key()
    r = post(c, "/admin/keys", provider="openai", label="บัญชีหลัก", key=key)
    assert r.status_code == 303 and r.headers["location"] == "/admin/keys"
    (row,) = rows(secrets_on, ProviderKey)
    assert row.provider == "openai" and row.label == "บัญชีหลัก" and row.active
    assert key.encode() not in row.secret_enc and secretbox.decrypt(row.secret_enc) == key
    assert row.last4 == key[-4:]
    page = c.get("/admin/keys").text
    assert "บัญชีหลัก" in page and html.escape(secretbox.mask(key[-4:])) in page and "<span>กำลังใช้</span>" in page
    assert_no_leak(key, page)
    (log,) = rows(secrets_on, AuditLog)
    assert log.action == "key.create" and log.actor == "admin"
    assert_no_leak(key, f"{log.target} {log.detail}")


def test_full_key_never_leaks_anywhere(c, secrets_on, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    key = fake_key()
    r = post(c, "/admin/keys", provider="deepseek", label="leak-check", key=key)
    cookies = [security.read_session(r.cookies.get("nexxa_session") or c.cookies.get("nexxa_session"))]
    kid = rows(secrets_on, ProviderKey)[0].id
    replaced = fake_key("sk-FAKE2-")
    pages = [r.text]
    pages.append(post(c, f"/admin/keys/{kid}/replace", key=replaced).text)
    pages.append(post(c, "/admin/keys", provider="openai", label="x", key="short").text)  # error re-render
    pages.append(post(c, "/admin/keys", provider="nope", label="x", key=key).text)  # error re-render with key typed
    post(c, f"/admin/keys/{kid}/toggle")
    post(c, f"/admin/keys/{kid}/toggle")
    post(c, "/admin/ai", provider="deepseek", model="deepseek-chat", base_url="")
    for path in ["/admin", "/admin/keys", "/admin/keys/new", f"/admin/keys/{kid}/replace", f"/admin/keys/{kid}/delete",
                 "/admin/devices", "/admin/ai", "/admin/audit"]:
        resp = c.get(path)
        assert resp.status_code == 200
        pages.append(resp.text)
        pages.append(json.dumps(dict(resp.headers)))
        cookies.append(security.read_session(c.cookies.get("nexxa_session")))
    # the analyzer path with the stored key: a failing client must not log it either
    seen = []

    def boom(**kw):
        seen.append(kw)
        raise ValueError("cannot build client")

    monkeypatch.setattr("app.analyzer.openai.OpenAI", boom)
    crop = Crop(name="x", labels=["ปกติ"], prompt="p", notes=None)
    note = analyzer.analyze_leaf(GOOD, crop).note
    assert seen and seen[0]["api_key"] == replaced
    pages.append(post(c, f"/admin/keys/{kid}/delete").text)
    blob = "\n".join(pages + [json.dumps(cookies, ensure_ascii=False), caplog.text, note, db_dump(secrets_on)])
    for k in (key, replaced):
        assert_no_leak(k, blob)


def test_key_form_errors_are_thai_and_never_echo_the_key(c, secrets_on):
    key = fake_key()
    r = post(c, "/admin/keys", provider="nope", label="", key=key)
    assert r.status_code == 422
    assert "กรุณาเลือกผู้ให้บริการ" in r.text and "กรุณากรอกชื่อเรียก" in r.text
    assert_no_leak(key, r.text)
    for bad in ("", "short", "has space inside-1234567890", "ไทย-1234567890", "x" * 513):
        r = post(c, "/admin/keys", provider="openai", label="a", key=bad)
        assert r.status_code == 422 and 'id="f-key-error"' in r.text, bad
    r = post(c, "/admin/keys", provider="openai", label="my " + key, key=key)
    assert r.status_code == 422 and "ห้ามใส่คีย์" in r.text
    assert_no_leak(key, r.text)
    assert count(secrets_on, ProviderKey) == 0 and count(secrets_on, AuditLog) == 0


def test_toggle_replace_delete_with_audit(c, secrets_on):
    key = add_key(c, "gemini", "g1")
    kid = rows(secrets_on, ProviderKey)[0].id
    assert post(c, f"/admin/keys/{kid}/toggle").status_code == 303
    assert rows(secrets_on, ProviderKey)[0].active is False
    assert post(c, f"/admin/keys/{kid}/toggle").status_code == 303
    assert rows(secrets_on, ProviderKey)[0].active is True
    page = c.get(f"/admin/keys/{kid}/replace")
    assert page.status_code == 200 and "g1" in page.text and "เปลี่ยนคีย์" in page.text
    new = fake_key("AIza-FAKE-")
    assert post(c, f"/admin/keys/{kid}/replace", key=new).status_code == 303
    row = rows(secrets_on, ProviderKey)[0]
    assert secretbox.decrypt(row.secret_enc) == new and row.last4 == new[-4:] and row.label == "g1"
    r = post(c, f"/admin/keys/{kid}/replace", key="bad key")
    assert r.status_code == 422 and secretbox.decrypt(rows(secrets_on, ProviderKey)[0].secret_enc) == new
    confirm = c.get(f"/admin/keys/{kid}/delete")
    assert confirm.status_code == 200 and f'action="/admin/keys/{kid}/delete"' in confirm.text
    assert post(c, f"/admin/keys/{kid}/delete").status_code == 303
    assert count(secrets_on, ProviderKey) == 0
    actions = [a.action for a in rows(secrets_on, AuditLog)]
    assert actions == ["key.create", "key.toggle", "key.toggle", "key.replace", "key.delete"]
    blob = " ".join(f"{a.target} {a.detail}" for a in rows(secrets_on, AuditLog))
    assert_no_leak(key, blob)
    assert_no_leak(new, blob)
    audit_page = c.get("/admin/audit").text
    assert "สร้างคีย์ AI" in audit_page and "ลบคีย์ AI" in audit_page
    for path in (f"/admin/keys/{kid}/replace", f"/admin/keys/{kid}/delete"):
        assert c.get(path).status_code == 404
    assert post(c, f"/admin/keys/{kid}/toggle").status_code == 404


def test_secrets_disabled_blocks_storing_keys(c_nosecret, engine):
    page = c_nosecret.get("/admin/keys")
    assert page.status_code == 200 and "NEXXA_SECRET_KEY" in page.text and "ปิดอยู่" in page.text
    assert 'href="/admin/keys/new"' not in page.text
    form = c_nosecret.get("/admin/keys/new")
    assert form.status_code == 200 and "<fieldset disabled" in form.text
    key = fake_key()
    r = post(c_nosecret, "/admin/keys", provider="openai", label="x", key=key)
    assert r.status_code == 409 and "NEXXA_SECRET_KEY" in r.text
    assert_no_leak(key, r.text)
    assert count(engine, ProviderKey) == 0 and count(engine, AuditLog) == 0
    with Session(engine) as s:  # a row left from when secrets were on cannot be replaced either
        s.add(ProviderKey(provider="openai", label="old", secret_enc=b"x", last4="abcd"))
        s.commit()
    listing = c_nosecret.get("/admin/keys").text  # a stored key cannot be decrypted now: never shown as in use
    assert "ใช้ไม่ได้ (การเข้ารหัสปิด)" in listing and "<span>กำลังใช้</span>" not in listing
    r = post(c_nosecret, "/admin/keys/1/replace", key=key)
    assert r.status_code == 409 and rows(engine, ProviderKey)[0].secret_enc == b"x"
    assert count(engine, AuditLog) == 0
    assert "NEXXA_SECRET_KEY" in c_nosecret.get("/admin/ai").text


# --- runtime_config ------------------------------------------------------------------

def test_provider_api_key_db_first_then_env(c, secrets_on, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    assert runtime_config.provider_api_key("openai") is None
    env_key = fake_key("sk-ENV-")
    monkeypatch.setenv("OPENAI_API_KEY", env_key)
    assert runtime_config.provider_api_key("openai") == env_key
    first = add_key(c, "openai", "one")
    assert runtime_config.provider_api_key("openai") == first
    second = add_key(c, "openai", "two")
    assert runtime_config.provider_api_key("openai") == second  # newest active key wins
    assert runtime_config.provider_api_key("gemini") is None
    ids = [k.id for k in rows(secrets_on, ProviderKey)]
    post(c, f"/admin/keys/{ids[1]}/toggle")
    assert runtime_config.provider_api_key("openai") == first
    post(c, f"/admin/keys/{ids[0]}/toggle")
    assert runtime_config.provider_api_key("openai") == env_key  # all disabled -> env
    post(c, f"/admin/keys/{ids[0]}/toggle")
    monkeypatch.setenv("NEXXA_SECRET_KEY", Fernet.generate_key().decode())  # wrong key: cannot decrypt
    assert runtime_config.provider_api_key("openai") == env_key
    monkeypatch.delenv("NEXXA_SECRET_KEY")  # secretbox off
    assert runtime_config.provider_api_key("openai") == env_key
    assert runtime_config.provider_api_key("nope") is None
    for k in (first, second, env_key):
        assert_no_leak(k, caplog.text)


@pytest.fixture
def broken_db(monkeypatch, tmp_path):
    bad = create_engine(f"sqlite:///{tmp_path / 'missing' / 'no.db'}")  # parent folder does not exist
    monkeypatch.setattr(app.db, "engine", bad)
    for k in VISION_ENV:
        monkeypatch.delenv(k, raising=False)


def test_runtime_config_falls_back_to_env_when_db_unavailable(broken_db, monkeypatch, caplog):
    monkeypatch.setenv("NEXXA_SECRET_KEY", Fernet.generate_key().decode())
    env_key = fake_key("sk-ENV-")
    monkeypatch.setenv("OPENAI_API_KEY", env_key)
    monkeypatch.setenv("NEXXA_VISION_PROVIDER", "Qwen")
    monkeypatch.setenv("NEXXA_VISION_MODEL", "env-model")
    monkeypatch.setenv("NEXXA_VISION_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "env-device-key")
    assert runtime_config.provider_api_key("openai") == env_key
    assert runtime_config.get_setting("vision_model") is None
    assert runtime_config.vision_provider() == "qwen"
    assert runtime_config.vision_model() == "env-model"
    assert runtime_config.vision_base_url() == "https://env.example.com/v1"
    assert runtime_config.device_key_valid("env-device-key") is True
    assert runtime_config.device_key_valid("other") is False
    assert_no_leak(env_key, caplog.text)


def test_runtime_config_defaults_match_old_env_behaviour(secrets_on, monkeypatch):
    assert runtime_config.vision_provider() == "anthropic"
    assert runtime_config.vision_model() is None and runtime_config.vision_base_url() is None
    monkeypatch.setenv("NEXXA_VISION_PROVIDER", " Gemini ")
    monkeypatch.setenv("NEXXA_VISION_MODEL", "gm")
    assert runtime_config.vision_provider() == "gemini" and runtime_config.vision_model() == "gm"


# --- AI settings ---------------------------------------------------------------------

class FakeOpenAI:
    def __init__(self, reply):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._reply = reply

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


@pytest.fixture
def crop():
    return Crop(name="กรีนคอส", labels=["ปกติ", "ใบเหลือง"], prompt="ดูใบ", notes=None)


def capture_openai(monkeypatch):
    built = []

    def make(**kw):
        client = FakeOpenAI(json.dumps({"label": "ปกติ", "confidence": 0.9, "note": "ok"}))
        built.append((kw, client))
        return client

    monkeypatch.setattr("app.analyzer.openai.OpenAI", make)
    return built


def test_ai_settings_drive_the_analyzer_and_clearing_returns_to_env(c, secrets_on, monkeypatch, crop):
    key = add_key(c, "deepseek", "ds")
    r = post(c, "/admin/ai", provider="deepseek", model="deepseek-vision-x", base_url="https://proxy.example.com/v1")
    assert r.status_code == 303 and r.headers["location"] == "/admin/ai"
    built = capture_openai(monkeypatch)
    result = analyzer.analyze_leaf(GOOD, crop)
    assert result.label == "ปกติ" and result.model == "deepseek-vision-x"
    kw, client = built[-1]
    assert kw["api_key"] == key and kw["base_url"] == "https://proxy.example.com/v1"
    assert client.calls[0]["model"] == "deepseek-vision-x"
    page = c.get("/admin/ai").text
    assert "deepseek-vision-x" in page and "https://proxy.example.com/v1" in page

    # empty base URL = the provider's own default
    post(c, "/admin/ai", provider="deepseek", model="deepseek-vision-x", base_url="")
    analyzer.analyze_leaf(GOOD, crop)
    assert built[-1][0]["base_url"] == "https://api.deepseek.com"

    # clearing everything returns to the environment, exactly as before
    assert post(c, "/admin/ai", provider="", model="", base_url="").status_code == 303
    assert count(secrets_on, AppSetting) == 0
    env_key = fake_key("sk-ENV-")
    monkeypatch.setenv("NEXXA_VISION_PROVIDER", "openai")
    monkeypatch.setenv("NEXXA_VISION_MODEL", "env-model")
    monkeypatch.setenv("NEXXA_VISION_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", env_key)
    result = analyzer.analyze_leaf(GOOD, crop)
    kw, client = built[-1]
    assert kw["api_key"] == env_key and kw["base_url"] == "https://env.example.com/v1"
    assert client.calls[0]["model"] == "env-model" and result.model == "env-model"
    actions = [a.action for a in rows(secrets_on, AuditLog)]
    assert actions.count("ai.update") == 3
    assert all(a.detail is None or "proxy.example.com" not in a.detail for a in rows(secrets_on, AuditLog))


def test_anthropic_uses_web_key_and_model_else_sdk_env(c, secrets_on, monkeypatch, crop):
    seen = []

    class FakeAnthropic:
        def __init__(self, **kw):
            seen.append(kw)
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

        def _create(self, **kw):
            seen.append({"model": kw["model"]})
            content = [SimpleNamespace(type="text", text=json.dumps({"label": "ปกติ", "confidence": 0.9, "note": ""}))]
            return SimpleNamespace(stop_reason="end_turn", content=content)

    monkeypatch.setattr("app.analyzer.anthropic.Anthropic", FakeAnthropic)
    analyzer.analyze_leaf(GOOD, crop)
    assert seen == [{"timeout": 60, "max_retries": 1}, {"model": analyzer.settings.vision_model}]  # unchanged
    seen.clear()
    key = add_key(c, "anthropic", "claude")
    post(c, "/admin/ai", provider="anthropic", model="claude-web-model", base_url="")
    assert analyzer.analyze_leaf(GOOD, crop).model == "claude-web-model"
    assert seen == [{"api_key": key, "timeout": 60, "max_retries": 1}, {"model": "claude-web-model"}]


def test_check_vision_config_uses_web_settings(c, secrets_on, caplog):
    post(c, "/admin/ai", provider="gemini", model="", base_url="")
    with caplog.at_level(logging.WARNING, logger="app.analyzer"):
        analyzer.check_vision_config()
    assert "NEXXA_VISION_MODEL" in caplog.text and "GEMINI_API_KEY" in caplog.text
    caplog.clear()
    add_key(c, "gemini", "g")
    post(c, "/admin/ai", provider="gemini", model="gemini-x", base_url="")
    with caplog.at_level(logging.WARNING, logger="app.analyzer"):
        analyzer.check_vision_config()
    assert caplog.records == []


def test_ai_settings_validation(c, secrets_on):
    r = post(c, "/admin/ai", provider="nope", model="bad model name!", base_url="http://10.0.0.1/v1")
    assert r.status_code == 422
    assert 'id="f-provider-error"' in r.text and 'id="f-model-error"' in r.text and 'id="f-base_url-error"' in r.text
    assert count(secrets_on, AppSetting) == 0 and count(secrets_on, AuditLog) == 0


def test_readiness_cards(c, secrets_on, monkeypatch):
    page = c.get("/admin/ai").text
    assert set(re.findall(r'data-ready="(\w+)"', page)) == {"no"} and "ยังขาด" in page
    add_key(c, "openai", "o")
    post(c, "/admin/ai", provider="openai", model="gpt-x", base_url="")
    page = c.get("/admin/ai").text
    cards = dict(re.findall(r'data-provider="(\w+)" data-ready="(\w+)"', page))
    assert cards == {"anthropic": "no", "openai": "yes", "gemini": "no", "qwen": "no", "deepseek": "no"}
    assert "พร้อมใช้งาน" in page and "ยังไม่มีคีย์" in page and "การเข้ารหัสคีย์" in page
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key("sk-ant-ENV-"))  # env key + built-in default model
    page = c.get("/admin/ai").text
    assert dict(re.findall(r'data-provider="(\w+)" data-ready="(\w+)"', page))["anthropic"] == "yes"
    post(c, "/admin/ai", provider="openai", model="", base_url="")
    page = c.get("/admin/ai").text
    assert dict(re.findall(r'data-provider="(\w+)" data-ready="(\w+)"', page))["openai"] == "no"
    assert "ยังไม่ได้ตั้งชื่อรุ่น" in page


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1", "ftp://api.example.com", "https://user:pw@api.example.com/v1",
    "https://api.example.com/v1#frag", "https://api.example.com/v1?key=abc", "https://10.1.2.3/v1",
    "https://127.0.0.1/v1", "https://[::1]/v1", "https://169.254.169.254/latest", "https://192.168.1.5",
    "https://172.16.0.1", "https://0.0.0.0", "https://[::ffff:127.0.0.1]/", "https://100.64.0.1",
    "https://localhost/v1", "https://LOCALHOST./v1", "https://printer.local/v1", "https://db.internal/v1",
    "https://a.localhost", "https://127.1/v1", "https://2130706433/", "https://0x7f000001/", "https://intranet/v1",
    "https://api.example.com:99999/v1", "https:///v1", "https://exa mple.com", "https://api.example.com/v1\\x",
    "https://ตัวอย่าง.com", "https://" + "a" * 300 + ".com", "javascript:alert(1)", "//api.example.com/v1",
    "https://-bad-.example.com", "https://api.example.com\n/v1",
])
def test_base_url_rejected(url):
    assert routes_keys.base_url_error(url) is not None


@pytest.mark.parametrize("url", ["", "https://api.example.com/v1", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                                 "https://8.8.8.8/v1", "https://api.example.com:8443/openai/"])
def test_base_url_accepted(url):
    assert routes_keys.base_url_error(url) is None


def test_insecure_localhost_only_with_flag(monkeypatch):
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1"):
        assert routes_keys.base_url_error(url) is not None
    monkeypatch.setenv("NEXXA_ALLOW_INSECURE_BASE_URL", "1")
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1"):
        assert routes_keys.base_url_error(url) is None
    for url in ("http://api.example.com/v1", "http://10.0.0.1/v1", "http://user:pw@localhost/v1"):
        assert routes_keys.base_url_error(url) is not None


# --- device keys -------------------------------------------------------------------------

@pytest.fixture
def station(secrets_on, monkeypatch, tmp_path):
    with Session(secrets_on) as s:
        seed(s)
        uid = s.scalars(select(Station.rfid_uid).where(Station.kind == "station")).first()
    monkeypatch.setattr(storage, "settings", dataclasses.replace(storage.settings, image_dir=tmp_path))
    monkeypatch.setattr(analyzer, "analyze_leaf", lambda *a, **k: AnalysisResult("ปกติ", 0.9, "ok", False, "m"))
    return uid


def capture(uid, key):
    client = _client()
    headers = {} if key is None else {"X-Device-Key": key}
    rid = client.post("/rounds", headers=headers)
    if rid.status_code != 200:
        return rid.status_code
    r = client.post("/captures", headers=headers, files={"image": ("l.png", GOOD, "image/png")},
                    data={"rfid_uid": uid, "round_id": rid.json()["round_id"], "camera_angle": "top"})
    return r.status_code


def create_device(c, label="หุ่นยนต์ 1") -> str:
    r = post(c, "/admin/devices", label=label)
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    m = re.search(r'data-device-key>([^<]+)<', r.text)
    assert m
    return html.unescape(m.group(1))


def test_device_key_shown_once_hash_only(c, secrets_on, caplog):
    caplog.set_level(logging.DEBUG)
    key = create_device(c)
    (row,) = rows(secrets_on, DeviceKey)
    assert row.key_hash == security.hash_device_key(key) and row.last4 == key[-4:] and row.active
    listing = c.get("/admin/devices").text
    assert "หุ่นยนต์ 1" in listing and secretbox.mask(key[-4:]) in listing
    later = [listing, c.get("/admin").text, c.get("/admin/audit").text, c.get("/admin/ai").text]
    assert_no_leak(key, "\n".join(later), db_dump(secrets_on), caplog.text)
    (log,) = rows(secrets_on, AuditLog)
    assert log.action == "device.create" and row.key_hash not in f"{log.target} {log.detail}"


def test_device_label_required(c, secrets_on):
    r = post(c, "/admin/devices", label="  ")
    assert r.status_code == 422 and 'id="f-label-error"' in r.text and count(secrets_on, DeviceKey) == 0


def test_device_keys_gate_uploads(c, secrets_on, station, monkeypatch):
    assert capture(station, "anything") == 401  # neither a web key nor env: fail closed
    assert capture(station, None) == 401
    key = create_device(c)
    assert capture(station, key) == 200
    assert capture(station, key[:-1]) == 401
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "env-device-key")
    assert capture(station, "env-device-key") == 200  # env fallback still works
    assert capture(station, key) == 200
    did = rows(secrets_on, DeviceKey)[0].id
    assert post(c, f"/admin/devices/{did}/revoke").status_code == 303
    row = rows(secrets_on, DeviceKey)[0]
    assert row.active is False and row.revoked_at is not None
    assert capture(station, key) == 401
    assert capture(station, "env-device-key") == 200
    page = c.get("/admin/devices").text
    assert "ยกเลิกแล้ว" in page and f'action="/admin/devices/{did}/revoke"' not in page
    assert post(c, f"/admin/devices/{did}/revoke").status_code == 303  # idempotent
    assert [a.action for a in rows(secrets_on, AuditLog)] == ["device.create", "device.revoke"]
    assert post(c, "/admin/devices/999/revoke").status_code == 404


def test_device_key_valid_unit(secrets_on, monkeypatch):
    assert runtime_config.device_key_valid(None) is False
    assert runtime_config.device_key_valid("") is False
    with Session(secrets_on) as s:
        s.add(DeviceKey(label="d", key_hash=security.hash_device_key("web-key"), last4="-key"))
        s.add(DeviceKey(label="r", key_hash=security.hash_device_key("old-key"), last4="-key", active=False))
        s.commit()
    assert runtime_config.device_key_valid("web-key") is True
    assert runtime_config.device_key_valid("old-key") is False
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "")
    assert runtime_config.device_key_valid("") is False


# --- structure -------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/admin/keys", "/admin/keys/new", "/admin/keys/1/replace", "/admin/keys/1/delete",
                                  "/admin/devices", "/admin/ai"])
def test_pages_are_thai_and_labelled(c, secrets_on, path):
    add_key(c)
    create_device(c)
    r = c.get(path)
    assert r.status_code == 200
    p = _A11y()
    p.feed(r.text)
    assert p.lang == "th"
    for inp in p.inputs:
        assert inp.get("id") in p.label_for, f"input without label: {inp}"
    assert all(p.buttons)
    assert re.search(r"[\U0001F300-\U0001FAFF☀-➿]", r.text) is None
    assert "ทดสอบ" not in r.text  # no 'test this key' button: a test call costs money


def test_device_created_page_is_labelled(c, secrets_on):
    r = post(c, "/admin/devices", label="d")
    p = _A11y()
    p.feed(r.text)
    assert p.lang == "th" and all(p.buttons) and "แสดงครั้งเดียว" in r.text
