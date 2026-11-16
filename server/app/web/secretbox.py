import os

from cryptography.fernet import Fernet, InvalidToken


class SecretsDisabledError(RuntimeError):
    """NEXXA_SECRET_KEY is missing or not a valid Fernet key."""


class SecretDecryptError(RuntimeError):
    """Wrong key or corrupted ciphertext."""


def _fernet() -> Fernet:
    try:  # read per call so tests can monkeypatch the env
        return Fernet(os.getenv("NEXXA_SECRET_KEY", ""))
    except (ValueError, TypeError):
        raise SecretsDisabledError("NEXXA_SECRET_KEY is not set to a valid Fernet key") from None


def secrets_enabled() -> bool:
    try:
        _fernet()
        return True
    except SecretsDisabledError:
        return False


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    f = _fernet()
    try:
        return f.decrypt(token).decode()
    except (InvalidToken, TypeError, ValueError):
        raise SecretDecryptError("cannot decrypt secret (wrong key or corrupted data)") from None


def last4(value: str) -> str:
    return value[-4:]


def mask(last4: str) -> str:
    return "••••••••" + last4
