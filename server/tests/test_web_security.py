import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import create_admin
from app.db import init_db
from app.models import AdminUser
from app.web import secretbox, security


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("NEXXA_SECRET_KEY", k)
    return k


@pytest.fixture
def no_secrets(monkeypatch):
    monkeypatch.delenv("NEXXA_SECRET_KEY", raising=False)
    monkeypatch.delenv("NEXXA_SESSION_SECRET", raising=False)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as s:
        yield s


# --- secretbox ---

def test_encrypt_round_trip(key):
    assert secretbox.secrets_enabled()
    token = secretbox.encrypt("sk-test-1234")
    assert token != b"sk-test-1234" and b"sk-test" not in token
    assert secretbox.decrypt(token) == "sk-test-1234"


def test_secrets_disabled_without_key(no_secrets):
    assert not secretbox.secrets_enabled()
    with pytest.raises(secretbox.SecretsDisabledError):
        secretbox.encrypt("x")
    with pytest.raises(secretbox.SecretsDisabledError):
        secretbox.decrypt(b"x")


def test_secrets_disabled_with_invalid_key(monkeypatch):
    monkeypatch.setenv("NEXXA_SECRET_KEY", "not-a-fernet-key")
    assert not secretbox.secrets_enabled()
    with pytest.raises(secretbox.SecretsDisabledError):
        secretbox.encrypt("x")


def test_decrypt_wrong_key_or_garbage(monkeypatch, key):
    token = secretbox.encrypt("hello")
    monkeypatch.setenv("NEXXA_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(secretbox.SecretDecryptError):
        secretbox.decrypt(token)
    with pytest.raises(secretbox.SecretDecryptError):
        secretbox.decrypt(b"garbage")


def test_mask_and_last4():
    assert secretbox.last4("sk-abcdef1234") == "1234"
    assert secretbox.last4("ab") == "ab"
    assert secretbox.mask("abcd") == "••••••••abcd"


# --- passwords ---

def test_hash_password_unique_and_verifies():
    a, b = security.hash_password("correct horse"), security.hash_password("correct horse")
    assert a != b
    assert security.verify_password("correct horse", a)
    assert not security.verify_password("wrong", a)


@pytest.mark.parametrize("bad", ["", "plain", "scrypt$x$y", "scrypt$a$b$c$d$e$f", "scrypt$16384$8$1$zz$zz", "md5$1$2$3$4$5"])
def test_verify_malformed_hash_is_false(bad):
    assert security.verify_password("pw", bad) is False


# --- sessions ---

@pytest.fixture
def sess_secret(monkeypatch):
    monkeypatch.delenv("NEXXA_SECRET_KEY", raising=False)
    monkeypatch.setenv("NEXXA_SESSION_SECRET", "unit-test-session-secret")


def test_session_round_trip(sess_secret):
    assert security.session_enabled()
    tok = security.sign_session({"uid": 7, "csrf": "c"}, 60, now=1000.0)
    assert security.read_session(tok, now=1030.0) == {"uid": 7, "csrf": "c"}


def test_session_tampered_every_position(sess_secret):
    tok = security.sign_session({"uid": 7}, 60, now=1000.0)
    for i in range(len(tok)):
        bad = tok[:i] + ("A" if tok[i] != "A" else "B") + tok[i + 1:]
        assert security.read_session(bad, now=1001.0) is None


def test_session_expired(sess_secret):
    tok = security.sign_session({"uid": 7}, 60, now=1000.0)
    assert security.read_session(tok, now=1059.0) == {"uid": 7}
    assert security.read_session(tok, now=1061.0) is None


@pytest.mark.parametrize("garbage", ["", "x", "a.b", "...", "é.é", "e30.e30"])
def test_session_garbage_is_none(sess_secret, garbage):
    assert security.read_session(garbage) is None


def test_session_secret_different_secret_rejects(monkeypatch, sess_secret):
    tok = security.sign_session({"uid": 1}, 60, now=1000.0)
    monkeypatch.setenv("NEXXA_SESSION_SECRET", "another")
    assert security.read_session(tok, now=1001.0) is None


def test_session_disabled_without_secret(no_secrets):
    assert not security.session_enabled()
    with pytest.raises(security.SessionSecretMissingError):
        security.sign_session({"uid": 1}, 60)


def test_session_secret_derived_from_secret_key(key):
    assert security.session_enabled()
    tok = security.sign_session({"uid": 1}, 60, now=1000.0)
    assert security.read_session(tok, now=1001.0) == {"uid": 1}


# --- csrf ---

def test_csrf():
    t = security.new_csrf_token()
    assert len(t) >= 32 and t != security.new_csrf_token()
    assert security.csrf_ok(t, t)
    assert not security.csrf_ok(t, t + "x")
    assert not security.csrf_ok(t, None)
    assert not security.csrf_ok(t, "")
    assert not security.csrf_ok("", "")


# --- throttle ---

def test_throttle():
    th = security.LoginThrottle()
    for i in range(4):
        th.record_failure("ip", 100.0 + i)
    assert th.allowed("ip", 110.0)
    th.record_failure("ip", 104.0)
    assert not th.allowed("ip", 110.0)
    assert th.allowed("other", 110.0)
    assert th.allowed("ip", 100.0 + 900 + 5)  # window passed
    th.reset("ip")
    assert th.allowed("ip", 110.0)


# --- device keys ---

def test_device_key():
    k = security.new_device_key()
    assert len(k) >= 40 and k != security.new_device_key()
    h = security.hash_device_key(k)
    assert h == security.hash_device_key(k) and len(h) == 64 and h != k


# --- create_admin ---

def test_create_or_update_rejects_short_password(session):
    with pytest.raises(ValueError):
        create_admin.create_or_update(session, "boss", "short")
    assert session.scalar(select(func.count()).select_from(AdminUser)) == 0


def test_create_or_update_updates_existing(session):
    a = create_admin.create_or_update(session, "boss", "first-password-1")
    b = create_admin.create_or_update(session, "boss", "second-password-2")
    assert a.id == b.id
    assert session.scalar(select(func.count()).select_from(AdminUser)) == 1
    assert security.verify_password("second-password-2", b.password_hash)
    assert not security.verify_password("first-password-1", b.password_hash)
