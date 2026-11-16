import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if not 2 <= n <= 2**20 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16:
            return False  # stored params come from the DB; refuse values that could exhaust memory
        want = bytes.fromhex(dk)
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=n, r=r, p=p, dklen=len(want))
        return bool(want) and hmac.compare_digest(got, want)
    except (ValueError, TypeError, AttributeError):
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _session_secret() -> bytes | None:
    explicit = os.getenv("NEXXA_SESSION_SECRET")
    if explicit:
        return explicit.encode()
    base = os.getenv("NEXXA_SECRET_KEY")
    if base:
        return hmac.new(base.encode(), b"nexxa-session-v1", hashlib.sha256).digest()
    return None


class SessionSecretMissingError(RuntimeError):
    """Neither NEXXA_SESSION_SECRET nor NEXXA_SECRET_KEY is set."""


def session_enabled() -> bool:
    return _session_secret() is not None


def _sig(secret: bytes, body: str) -> bytes:
    return _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest()).encode()


def sign_session(data: dict, max_age_s: int, now: float | None = None) -> str:
    secret = _session_secret()
    if secret is None:
        raise SessionSecretMissingError("set NEXXA_SESSION_SECRET or NEXXA_SECRET_KEY")
    exp = (time.time() if now is None else now) + max_age_s
    body = _b64(json.dumps({"d": data, "exp": exp}, separators=(",", ":")).encode())
    return f"{body}.{_sig(secret, body).decode()}"


def read_session(token: str, now: float | None = None) -> dict | None:
    secret = _session_secret()
    if secret is None:
        raise SessionSecretMissingError("set NEXXA_SESSION_SECRET or NEXXA_SECRET_KEY")
    try:
        body, sig = token.split(".")
        if not hmac.compare_digest(_sig(secret, body), sig.encode()):
            return None
        payload = json.loads(_unb64(body))
        if (time.time() if now is None else now) >= payload["exp"]:
            return None
        return payload["d"]
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(session_token: str, submitted: str | None) -> bool:
    if not session_token or not submitted:
        return False
    return hmac.compare_digest(session_token.encode(), submitted.encode())


class LoginThrottle:
    # shortcut: in-memory counter, use the database when running multiple processes
    def __init__(self, max_failures: int = 5, window_s: int = 900):
        self.max_failures, self.window_s = max_failures, window_s
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def allowed(self, key: str, now: float) -> bool:
        return len(self._recent(key, now)) < self.max_failures

    def record_failure(self, key: str, now: float) -> None:
        self._fails[key] = [*self._recent(key, now), now]

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


def new_device_key() -> str:
    return secrets.token_urlsafe(32)


def hash_device_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
