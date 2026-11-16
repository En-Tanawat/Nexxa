"""Settings the admin can change in the web UI, with the old environment variables as fallback.

Every function reads the database through ``app.db.engine`` looked up at call time (tests monkeypatch it) and never
raises: when the database cannot be read, the environment is used exactly as before. Key values are never logged.
"""
import hmac
import logging
import os
from contextlib import nullcontext

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db
from app.config import settings
from app.models import AppSetting, DeviceKey, ProviderKey
from app.providers import PROVIDERS
from app.web import secretbox, security

log = logging.getLogger(__name__)

VISION_PROVIDER, VISION_MODEL, VISION_BASE_URL = "vision_provider", "vision_model", "vision_base_url"


def _session(session: Session | None):
    return nullcontext(session) if session is not None else Session(app.db.engine)  # only close what we opened


def get_setting(key: str, session: Session | None = None) -> str | None:
    try:
        with _session(session) as s:
            value = s.scalar(select(AppSetting.value).where(AppSetting.key == key))
    except Exception as e:  # database down or not migrated: behave as if nothing was set in the web UI
        log.warning("app setting %s could not be read (%s); using the environment", key, type(e).__name__)
        return None
    return (value.strip() or None) if isinstance(value, str) else None


def vision_provider() -> str:
    return (get_setting(VISION_PROVIDER) or "").strip().lower() or settings.vision_provider


def vision_model() -> str | None:
    return get_setting(VISION_MODEL) or os.getenv("NEXXA_VISION_MODEL") or None


def vision_base_url() -> str | None:
    return get_setting(VISION_BASE_URL) or settings.vision_base_url


def db_provider_key(provider_name: str) -> str | None:
    """Newest active web-stored key for the provider, decrypted; None when absent or unusable."""
    try:
        with Session(app.db.engine) as s:
            rows = s.execute(select(ProviderKey.id, ProviderKey.secret_enc).where(
                ProviderKey.provider == provider_name, ProviderKey.active)
                .order_by(ProviderKey.created_at.desc(), ProviderKey.id.desc())).all()
    except Exception as e:
        log.warning("provider keys could not be read (%s); using the environment", type(e).__name__)
        return None
    for key_id, blob in rows:
        try:
            return secretbox.decrypt(blob)
        except (secretbox.SecretsDisabledError, secretbox.SecretDecryptError) as e:
            log.warning("provider key #%s (%s) cannot be decrypted (%s)", key_id, provider_name, type(e).__name__)
    return None


def provider_api_key(provider_name: str) -> str | None:
    prov = PROVIDERS.get(provider_name)
    if prov is None:
        return None
    return db_provider_key(provider_name) or os.getenv(prov.key_env) or None


def device_key_valid(candidate: str | None, session: Session | None = None) -> bool:
    """An active web-created device key (compared by SHA-256 hash) or env NEXXA_DEVICE_KEY; fail closed."""
    if not candidate:
        return False
    digest = security.hash_device_key(candidate).encode()
    try:
        with _session(session) as s:
            hashes = s.scalars(select(DeviceKey.key_hash).where(DeviceKey.active, DeviceKey.revoked_at.is_(None))).all()
    except Exception as e:
        log.warning("device keys could not be read (%s); using NEXXA_DEVICE_KEY only", type(e).__name__)
        hashes = []
    ok = False
    for h in hashes:  # check every row (no early exit) so timing says nothing about which one matched
        ok |= hmac.compare_digest(h.encode(), digest)
    env = settings.device_key
    return ok or bool(env) and hmac.compare_digest(candidate.encode(), env.encode())
