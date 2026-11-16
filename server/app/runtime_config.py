"""Settings the admin can change in the web UI, with the old environment variables as fallback.

Every function reads the database through ``app.db.engine`` looked up at call time (tests monkeypatch it) and never
raises: when the database cannot be read, the environment is used exactly as before. Key values are never logged.
"""
import hmac
import ipaddress
import logging
import os
import re
from contextlib import nullcontext
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db
from app.config import settings
from app.models import AppSetting, DeviceKey, ProviderKey
from app.providers import PROVIDERS, host_allowed
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


# --- base URL policy ---------------------------------------------------------------------

HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")
URL_MAX = 300
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def custom_base_url_allowed() -> bool:
    return os.getenv("NEXXA_ALLOW_CUSTOM_BASE_URL") == "1"


def _provider_host_error(provider: str, host: str) -> str | None:
    prov = PROVIDERS.get(provider)
    if prov is None:
        return "ผู้ให้บริการไม่ถูกต้อง"
    if prov.kind == "anthropic":
        return "Anthropic ไม่ใช้ base URL กรุณาเว้นว่าง"
    if host_allowed(prov, host) or custom_base_url_allowed():
        return None
    official = ", ".join("*" + h if h.startswith(".") else h for h in prov.api_hosts)
    return (f"โดเมนนี้ไม่ใช่ API ทางการของ {provider} ({official}) คีย์จะถูกส่งไปยัง URL นี้ จึงต้องให้ผู้ดูแลเซิร์ฟเวอร์"
            " ตั้ง NEXXA_ALLOW_CUSTOM_BASE_URL=1 ก่อน")


def _with_embedded_ipv4(ip) -> list:
    """The address plus every IPv4 address an IPv6 form carries (mapped, 6to4, Teredo, NAT64)."""
    out = [ip]
    if ip.version == 6:
        if ip.ipv4_mapped:
            out.append(ip.ipv4_mapped)
        if ip.sixtofour:
            out.append(ip.sixtofour)
        if ip.teredo:
            out.extend(ip.teredo)
        if ip in NAT64:
            out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def base_url_error(value: str, provider: str | None = None) -> str | None:
    """Validate a base URL set in the web UI; '' means the provider's default. Used at write time AND at use time.

    SSRF rules always apply. With a provider, the host must also be one of that provider's official API hosts
    (providers.api_hosts) unless the operator set NEXXA_ALLOW_CUSTOM_BASE_URL=1: the active key is sent to this URL.

    shortcut: a hostname that RESOLVES to a private/loopback address is not blocked (no DNS lookup here, and DNS can
    change after the check); the OWASP task should pin/check the resolved address at connect time.
    """
    if not value:
        return None
    if len(value) > URL_MAX:
        return f"URL ยาวเกินไป (ไม่เกิน {URL_MAX} ตัวอักษร)"
    if not value.isascii() or not value.isprintable() or " " in value or "\\" in value:
        return "URL มีอักขระที่ใช้ไม่ได้ (ช่องว่าง เครื่องหมาย \\ หรือตัวอักษรที่ไม่ใช่ภาษาอังกฤษ)"
    if "?" in value or "#" in value:
        return "URL ต้องไม่มีส่วน ? หรือ # (ห้ามใส่คีย์หรือพารามิเตอร์ใน URL)"
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port  # raises ValueError for a port that is not a number in 0-65535
    except ValueError:
        return "รูปแบบ URL ไม่ถูกต้อง"
    if port == 0:
        return "พอร์ต 0 ใช้ไม่ได้"
    if "@" in parts.netloc:
        return "ห้ามใส่ชื่อผู้ใช้หรือรหัสผ่านใน URL"
    if not host:
        return "รูปแบบ URL ไม่ถูกต้อง ตัวอย่าง https://api.example.com/v1"
    if provider is not None and (err := _provider_host_error(provider, host)):
        return err
    if os.getenv("NEXXA_ALLOW_INSECURE_BASE_URL") == "1" and host in ("localhost", "127.0.0.1") \
            and parts.scheme in ("http", "https"):
        return None  # local testing only, opt-in from the server environment
    if parts.scheme != "https":
        return "URL ต้องขึ้นต้นด้วย https://"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if any(not a.is_global or a.is_multicast for a in _with_embedded_ipv4(ip)):
            return "ห้ามใช้ที่อยู่ IP ภายในหรือที่สงวนไว้ (เช่น 10.x, 192.168.x, 127.x, 169.254.x)"
        return None
    labels = host.split(".")
    # every label a DNS label and a TLD that starts with a letter: rejects "127.1", "0x7f000001", "intranet"
    if len(labels) < 2 or not all(HOST_LABEL_RE.fullmatch(x) for x in labels) or not labels[-1][0].isalpha():
        return "ชื่อโดเมนไม่ถูกต้อง ต้องเป็นโดเมนสาธารณะ เช่น api.example.com"
    if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        return "ห้ามใช้ชื่อเครื่องภายใน (localhost, .local, .internal)"
    return None
