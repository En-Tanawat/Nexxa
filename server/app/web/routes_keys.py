"""Admin pages for AI provider keys, device keys (for the robot) and the active AI settings.

Secrets never leave this module in clear except once: a new device key is shown on the response to its POST.
Provider keys are Fernet-encrypted (secretbox) and shown only as a mask + last 4; when secretbox is off, nothing that
would store a key is accepted. Device keys are stored as SHA-256 hashes. Audit rows never carry a secret.
"""
import math
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import runtime_config
from app.config import settings
from app.db import get_session
from app.models import AdminUser, AppSetting, DeviceKey, ProviderKey
from app.providers import PROVIDERS
from app.web import audit, deps, secretbox, security
from app.web.routes_admin import CSRF, Id, _delete_page, _done, _fields, _missing, _text, _thai_time, _update_detail
from app.web.templating import render

base_url_error = runtime_config.base_url_error  # kept here for callers/tests

router = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])

PROVIDER_TH = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI", "gemini": "Google Gemini",
               "qwen": "Qwen (Alibaba Cloud)", "deepseek": "DeepSeek"}
KEY_MIN, KEY_MAX, LABEL_MAX, MODEL_MAX = 8, 512, 60, 100
DEVICE_NONCE = "device_nonce"
KEY_RE = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no spaces: every provider's keys look like this
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- validation ---------------------------------------------------------------------

def _key_error(key: str) -> str | None:
    if not key:
        return "กรุณาวางคีย์"
    if not KEY_MIN <= len(key) <= KEY_MAX:
        return f"คีย์ต้องยาว {KEY_MIN}-{KEY_MAX} ตัวอักษร"
    if not KEY_RE.fullmatch(key):
        return "คีย์ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษ ตัวเลข และเครื่องหมาย ห้ามมีช่องว่างหรือภาษาไทย"
    return None


# --- provider keys ----------------------------------------------------------------------

def _provider_options():
    return [(name, PROVIDER_TH.get(name, name)) for name in PROVIDERS]


def _key_form(request: Request, values: dict, errors: dict, key: ProviderKey | None = None, status_code=200):
    return render(request, "admin/key_form.html", status_code=status_code, values=values, errors=errors, key=key,
                  providers=_provider_options(), provider_th=PROVIDER_TH, secrets_on=secretbox.secrets_enabled(),
                  mask=secretbox.mask, added=_thai_time(key.created_at)[:10] if key else "")


@router.get("/keys")
def keys_list(request: Request, session: Session = Depends(get_session)):
    keys = session.scalars(select(ProviderKey).order_by(ProviderKey.provider, ProviderKey.created_at.desc(),
                                                        ProviderKey.id.desc())).all()
    broken = {k.id for k in keys if not secretbox.can_decrypt(k.secret_enc)}
    in_use: dict[str, int] = {}
    for k in keys:  # same rule as runtime_config: the newest active key that decrypts is the one used
        if k.active and k.id not in broken:
            in_use.setdefault(k.provider, k.id)
    return render(request, "admin/keys_list.html", keys=keys, in_use=set(in_use.values()), broken=broken,
                  provider_th=PROVIDER_TH,
                  secrets_on=secretbox.secrets_enabled(), mask=secretbox.mask, added=lambda k: _thai_time(k.created_at)[:10])


@router.get("/keys/new")
def key_new(request: Request):
    return _key_form(request, {"provider": "", "label": ""}, {})


@router.post("/keys", dependencies=CSRF)
def key_create(request: Request, f: dict = _fields("provider", "label", "key"),
               admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    values = {"provider": f["provider"], "label": f["label"]}  # the key itself is never echoed back
    if not secretbox.secrets_enabled():
        return _key_form(request, values, {}, status_code=409)
    e: dict[str, str] = {}
    provider = f["provider"].strip()
    if provider not in PROVIDERS:
        e["provider"] = "กรุณาเลือกผู้ให้บริการจากรายการ"
    label = _text(e, "label", f["label"], "ชื่อเรียก", LABEL_MAX)
    key = f["key"].strip()
    if err := _key_error(key):
        e["key"] = err
    elif key in f["label"]:
        e["label"] = "ห้ามใส่คีย์ในชื่อเรียก ชื่อเรียกแสดงบนหน้าเว็บและในบันทึกการเปลี่ยนแปลง"
        values["label"] = ""
    if e:
        return _key_form(request, values, e, status_code=422)
    row = ProviderKey(provider=provider, label=label, secret_enc=secretbox.encrypt(key), last4=secretbox.last4(key))
    session.add(row)
    session.flush()
    audit.record(session, admin.username, "key.create", f"key#{row.id} {provider}", label)
    session.commit()
    return _done(request, "/admin/keys", f"เพิ่มคีย์ {PROVIDER_TH[provider]} แล้ว: {label}")


@router.get("/keys/{key_id}/replace")
def key_replace_page(request: Request, key_id: Id, session: Session = Depends(get_session)):
    row = session.get(ProviderKey, key_id)
    return _missing(request) if row is None else _key_form(request, {}, {}, row)


@router.post("/keys/{key_id}/replace", dependencies=CSRF)
def key_replace(request: Request, key_id: Id, f: dict = _fields("key"), admin: AdminUser = Depends(deps.require_admin),
                session: Session = Depends(get_session)):
    row = session.get(ProviderKey, key_id)
    if row is None:
        return _missing(request)
    if not secretbox.secrets_enabled():
        return _key_form(request, {}, {}, row, status_code=409)
    key = f["key"].strip()
    if err := _key_error(key):
        return _key_form(request, {}, {"key": err}, row, status_code=422)
    row.secret_enc, row.last4, row.created_at = secretbox.encrypt(key), secretbox.last4(key), _now()
    audit.record(session, admin.username, "key.replace", f"key#{row.id} {row.provider}", row.label)
    session.commit()
    return _done(request, "/admin/keys", f"เปลี่ยนคีย์แล้ว: {row.label}")


@router.post("/keys/{key_id}/toggle", dependencies=CSRF)
def key_toggle(request: Request, key_id: Id, admin: AdminUser = Depends(deps.require_admin),
               session: Session = Depends(get_session)):
    row = session.get(ProviderKey, key_id)
    if row is None:
        return _missing(request)
    row.active = not row.active
    state = "เปิดใช้งาน" if row.active else "ปิดใช้งาน"
    audit.record(session, admin.username, "key.toggle", f"key#{row.id} {row.provider}", state)
    session.commit()
    return _done(request, "/admin/keys", f"{state}คีย์แล้ว: {row.label}")


def _key_delete_page(request: Request, row: ProviderKey):
    item = f"{PROVIDER_TH.get(row.provider, row.provider)} · {row.label} · {secretbox.mask(row.last4)}"
    return _delete_page(request, active="key", heading="ลบคีย์ AI", item=item, action=f"/admin/keys/{row.id}/delete",
                        back="/admin/keys", blocked=None,
                        note="คีย์ที่ลบแล้วกู้คืนไม่ได้ ถ้าต้องการหยุดใช้ชั่วคราว ให้กดปิดใช้งานแทน")


@router.get("/keys/{key_id}/delete")
def key_delete_page(request: Request, key_id: Id, session: Session = Depends(get_session)):
    row = session.get(ProviderKey, key_id)
    return _missing(request) if row is None else _key_delete_page(request, row)


@router.post("/keys/{key_id}/delete", dependencies=CSRF)
def key_delete(request: Request, key_id: Id, admin: AdminUser = Depends(deps.require_admin),
               session: Session = Depends(get_session)):
    row = session.get(ProviderKey, key_id)
    if row is None:
        return _missing(request)
    label = row.label
    audit.record(session, admin.username, "key.delete", f"key#{row.id} {row.provider}", label)
    session.delete(row)
    session.commit()
    return _done(request, "/admin/keys", f"ลบคีย์แล้ว: {label}")


# --- device keys ------------------------------------------------------------------------------

PAGE_SIZE = 50


def _devices_page(request: Request, session: Session, values: dict, errors: dict, page: str = "1", status_code=200):
    total = session.scalar(select(func.count(DeviceKey.id))) or 0
    pages = max(1, math.ceil(total / PAGE_SIZE))
    try:
        page_num = min(max(int(page), 1), pages)
    except ValueError:
        page_num = 1
    devices = session.scalars(
        select(DeviceKey)
        .order_by(DeviceKey.revoked_at.is_not(None), DeviceKey.id.desc())
        .limit(PAGE_SIZE)
        .offset((page_num - 1) * PAGE_SIZE)
    ).all()
    nonce = security.new_csrf_token()  # one-time: a refreshed/replayed create form must not mint a second key
    return render(request, "admin/devices_list.html", status_code=status_code, devices=devices, values=values,
                  errors=errors, mask=secretbox.mask, when=_thai_time, env_key=bool(os.getenv("NEXXA_DEVICE_KEY")),
                  nonce=nonce, session_update={DEVICE_NONCE: nonce}, page=page_num, pages=pages, total=total)


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"  # the only page that can hold a key in clear: never cache it
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/devices")
def devices_list(request: Request, page: str = "1", session: Session = Depends(get_session)):
    return _devices_page(request, session, {"label": ""}, {}, page=page)


@router.post("/devices", dependencies=CSRF)
def device_create(request: Request, f: dict = _fields("label", "nonce"), admin: AdminUser = Depends(deps.require_admin),
                  session: Session = Depends(get_session)):
    e: dict[str, str] = {}
    label = _text(e, "label", f["label"], "ชื่ออุปกรณ์", LABEL_MAX)
    if e:
        return _devices_page(request, session, {"label": f["label"]}, e, status_code=422)
    expected = (deps.session_data(request) or {}).get(DEVICE_NONCE)
    if not isinstance(expected, str) or not security.csrf_ok(expected, f["nonce"]):
        # shortcut: the nonce lives in the signed cookie, so two truly parallel POSTs could both pass; a refresh cannot
        return _no_store(render(request, "admin/device_created.html", status_code=409, device=None, device_key=None))
    key = security.new_device_key()
    row = DeviceKey(label=label, key_hash=security.hash_device_key(key), last4=secretbox.last4(key))
    session.add(row)
    session.flush()
    audit.record(session, admin.username, "device.create", f"device#{row.id}", label)
    session.commit()
    return _no_store(render(request, "admin/device_created.html", device=row, device_key=key,
                            session_update={DEVICE_NONCE: None}))


@router.post("/devices/{device_id}/revoke", dependencies=CSRF)
def device_revoke(request: Request, device_id: Id, admin: AdminUser = Depends(deps.require_admin),
                  session: Session = Depends(get_session)):
    row = session.get(DeviceKey, device_id)
    if row is None:
        return _missing(request)
    if row.revoked_at is None or row.active:
        row.active, row.revoked_at = False, row.revoked_at or _now()
        audit.record(session, admin.username, "device.revoke", f"device#{row.id}", row.label)
        session.commit()
    return _done(request, "/admin/devices", f"ยกเลิก device key แล้ว: {row.label} (อุปกรณ์ที่ใช้คีย์นี้จะส่งภาพไม่ได้อีก)")


# --- AI settings ------------------------------------------------------------------------------

AI_FIELDS = {"provider": runtime_config.VISION_PROVIDER, "model": runtime_config.VISION_MODEL,
             "base_url": runtime_config.VISION_BASE_URL}


# Model-name prefixes that clearly belong to one provider; used only to warn about a leftover model name.
MODEL_FAMILY = {"anthropic": "claude", "openai": "gpt", "gemini": "gemini", "qwen": "qwen", "deepseek": "deepseek"}


def _model_family(model: str) -> str | None:
    return next((p for p, prefix in MODEL_FAMILY.items() if model.lower().startswith(prefix)), None)


def _readiness() -> list[dict]:
    """One tile per provider. Only the selected provider can be 'ready': model and base URL are shared settings
    (one value, like the env vars), so for the others we only say whether a key is waiting for them."""
    selected, setting = runtime_config.vision_provider(), runtime_config.vision_model()
    cards = []
    for name, prov in PROVIDERS.items():
        source = "web" if runtime_config.db_provider_key(name) else "env" if os.getenv(prov.key_env) else None
        card = {"name": name, "label": PROVIDER_TH.get(name, name), "selected": name == selected, "source": source,
                "key_env": prov.key_env, "model": None, "missing": [] if source else ["ยังไม่มีคีย์"]}
        if card["selected"]:
            card["model"] = setting or (settings.vision_model if prov.kind == "anthropic" else None)
            if not card["model"]:
                card["missing"].append("ยังไม่ได้ตั้งชื่อรุ่น")
            elif _model_family(card["model"]) not in (None, name):
                card["missing"].append("รุ่นที่ตั้งไว้ไม่ใช่ของผู้ให้บริการนี้")
            card["state"] = "missing" if card["missing"] else "ready"
        else:
            card["state"] = "standby" if source else "missing"
        cards.append(card)
    return cards


def _ai_page(request: Request, values: dict, errors: dict, status_code=200):
    env = {"provider": os.getenv("NEXXA_VISION_PROVIDER", "").strip().lower() or "anthropic",
           "model": os.getenv("NEXXA_VISION_MODEL") or "", "base_url": bool(os.getenv("NEXXA_VISION_BASE_URL"))}
    return render(request, "admin/ai_settings.html", status_code=status_code, values=values, errors=errors,
                  providers=_provider_options(), cards=_readiness(), env=env, secrets_on=secretbox.secrets_enabled(),
                  model_now=runtime_config.vision_model())


@router.get("/ai")
def ai_settings(request: Request, session: Session = Depends(get_session)):
    values = {field: runtime_config.get_setting(key, session) or "" for field, key in AI_FIELDS.items()}
    return _ai_page(request, values, {})


@router.post("/ai", dependencies=CSRF)
def ai_settings_save(request: Request, f: dict = _fields(*AI_FIELDS), admin: AdminUser = Depends(deps.require_admin),
                     session: Session = Depends(get_session)):
    clean = {k: v.strip() for k, v in f.items()}
    e: dict[str, str] = {}
    if clean["provider"] and clean["provider"] not in PROVIDERS:
        e["provider"] = "กรุณาเลือกผู้ให้บริการจากรายการ"
    if clean["model"] and (len(clean["model"]) > MODEL_MAX or not MODEL_RE.fullmatch(clean["model"])):
        e["model"] = f"ชื่อรุ่นใช้ได้เฉพาะตัวอักษรภาษาอังกฤษ ตัวเลข และ . _ - : / @ + (ไม่เกิน {MODEL_MAX} ตัวอักษร)"
    # checked against the provider that will actually use it (empty field = the server's NEXXA_VISION_PROVIDER)
    if err := base_url_error(clean["base_url"], clean["provider"] or settings.vision_provider):
        e["base_url"] = err
    if e:
        return _ai_page(request, clean, e, status_code=422)
    changed = []
    for field, key in AI_FIELDS.items():
        row = session.get(AppSetting, key)
        if (row.value if row else "") == clean[field]:
            continue
        changed.append(f"{key}={clean[field] or '(ค่าเริ่มต้น)'}")  # no secrets here: the URL has no userinfo/query
        if not clean[field]:
            session.delete(row)  # empty = back to the environment / provider default
        elif row is None:
            session.add(AppSetting(key=key, value=clean[field]))
        else:
            row.value = clean[field]
    # values are kept so it is traceable where keys were sent; none of these fields can hold a secret
    audit.record(session, admin.username, "ai.update", "ai_settings", _update_detail(changed))
    session.commit()
    return _done(request, "/admin/ai", "บันทึกการตั้งค่า AI แล้ว")
