"""Admin pages: overview, stations, crops, batches (lots), users and the audit log.

Every rule is enforced here on the server; the templates only display the errors. Invalid forms re-render with
the typed values (HTTP 422); successful writes redirect (POST-redirect-GET) with a flash message.
"""
import math
import re
from datetime import date, timedelta

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.create_admin import MIN_PASSWORD_LEN
from app.db import get_session
from app.models import (AdminUser, AuditLog, Batch, Capture, Crop, DailyStat, DeviceKey, ProviderKey, Station)
from app.web import audit, deps, secretbox, security
from app.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
CSRF = [Depends(deps.require_csrf)]

NAME_MAX, TEXT_MAX, RFID_MAX, LABEL_MAX, LABELS_MAX, PASSWORD_MAX, USERNAME_MAX = 120, 2000, 64, 60, 20, 128, 64
PAGE_SIZE = 50
RFID_TAKEN = "RFID นี้ถูกใช้แล้ว"
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]  # larger ids would overflow SQLite and 500
DATE_MIN, DATE_MAX = date(2000, 1, 1), date(2100, 12, 31)

ACTION_TH = {
    "station.create": "สร้างสถานี", "station.update": "แก้ไขสถานี", "station.delete": "ลบสถานี",
    "crop.create": "สร้างชนิดผัก", "crop.update": "แก้ไขชนิดผัก", "crop.delete": "ลบชนิดผัก",
    "batch.create": "สร้างล็อตผัก", "batch.update": "แก้ไขล็อตผัก", "batch.delete": "ลบล็อตผัก",
    "user.create": "สร้างผู้ดูแล", "user.password": "เปลี่ยนรหัสผ่าน", "user.delete": "ลบผู้ดูแล",
    "key.create": "สร้างคีย์ AI", "key.replace": "เปลี่ยนคีย์ AI", "key.toggle": "เปิด/ปิดคีย์ AI", "key.delete": "ลบคีย์ AI",
    "device.create": "สร้าง device key", "device.revoke": "ยกเลิก device key", "ai.update": "แก้ไขการตั้งค่า AI",
}


# --- small helpers -------------------------------------------------------------------

def _fields(*keys: str):
    """Dependency: the named form fields as plain strings ('' when missing or not text)."""
    async def read(request: Request) -> dict[str, str]:
        form = await request.form()  # already parsed (and cached) by require_csrf
        return {k: v if isinstance(v := form.get(k), str) else "" for k in keys}
    return Depends(read)


def _text(errors: dict, key: str, value: str, label: str, max_len: int, required: bool = True) -> str:
    v = value.strip()
    if not v and required:
        errors[key] = f"กรุณากรอก{label}"
    elif len(v) > max_len:
        errors[key] = f"{label}ยาวเกินไป (ไม่เกิน {max_len} ตัวอักษร)"
    return v


def _num(errors: dict, key: str, value: str, label: str, lo: float, hi: float, integer: bool = False):
    v = value.strip()
    if not v:
        return None
    try:
        n = int(v) if integer else float(v)
    except ValueError:
        n = None
    if n is None or not (lo <= n <= hi) or (not integer and not math.isfinite(n)):
        errors[key] = f"{label}ต้องเป็นตัวเลขระหว่าง {lo:g} ถึง {hi:g}"
        return None
    return n


def _ref(errors: dict, session: Session, key: str, model, value: str, label: str, required: bool = False):
    """A foreign key chosen from a select; must exist."""
    v = value.strip()
    if not v:
        if required:
            errors[key] = f"กรุณาเลือก{label}"
        return None
    obj = session.get(model, int(v)) if v.isascii() and v.isdigit() and len(v) < 10 else None
    if obj is None:
        errors[key] = f"เลือก{label}ที่มีอยู่ในระบบ"
    return obj


def _date(errors: dict, key: str, value: str, label: str, required: bool) -> date | None:
    v = value.strip()
    if not v:
        if required:
            errors[key] = f"กรุณากรอก{label}"
        return None
    try:
        d = date.fromisoformat(v)
    except ValueError:
        d = None
    if d is None or not DATE_MIN <= d <= DATE_MAX:
        errors[key] = f"{label}ไม่ถูกต้อง (รูปแบบ ปปปป-ดด-วว ปี ค.ศ. 2000-2100)"
        return None
    return d


def _apply(obj, clean: dict) -> list[str]:
    changed = [k for k, v in clean.items() if getattr(obj, k) != v]
    for k in changed:
        setattr(obj, k, clean[k])
    return changed


def _update_detail(changed: list[str]) -> str:
    return "แก้ไข: " + (", ".join(changed) or "ไม่มีการเปลี่ยนแปลง")  # field names only, never values


def _commit(session: Session, errors: dict, key: str, message: str) -> bool:
    try:
        session.commit()
        return True
    except IntegrityError:  # lost a race against the unique constraint
        session.rollback()
        errors[key] = message
        return False


def _done(request: Request, url: str, text: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    deps.flash(request, response, text)
    return response


def _missing(request: Request):
    return render(request, "error.html", status_code=404, title="ไม่พบรายการ", message="ไม่พบรายการที่ต้องการ อาจถูกลบไปแล้ว")


def _count(session: Session, column, *where) -> int:
    return session.scalar(select(func.count(column)).where(*where)) or 0


def _like(q: str):
    return lambda column: column.contains(q, autoescape=True)  # %, _ are literal characters


def _delete_page(request: Request, *, active: str, heading: str, item: str, action: str, back: str,
                 blocked: tuple[str, str, str] | None, note: str, status_code: int = 200):
    """The separate confirmation page; blocked = (reason, href, link label) hides the delete form."""
    return render(request, "admin/confirm_delete.html", status_code=status_code, active=active, heading=heading,
                  item=item, action=action, back=back, blocked=blocked, note=note)


# --- overview (moved from routes_auth) -------------------------------------------------

@router.get("")
def admin_home(request: Request, session: Session = Depends(get_session)):
    count = session.scalar
    stats = {
        "stations": count(select(func.count(Station.id)).where(Station.kind != "home")),
        "stations_active": count(select(func.count(Station.id)).where(Station.kind != "home", Station.active)),
        "batches": count(select(func.count(Batch.id))),
        "batches_growing": count(select(func.count(Batch.id)).where(Batch.harvested_on.is_(None))),
        "ai_keys": count(select(func.count(ProviderKey.id))),
        "ai_keys_active": count(select(func.count(ProviderKey.id)).where(ProviderKey.active)),
        "devices": count(select(func.count(DeviceKey.id)).where(DeviceKey.revoked_at.is_(None))),
    }
    return render(request, "admin_home.html", stats=stats, secrets_enabled=secretbox.secrets_enabled())


# --- stations ---------------------------------------------------------------------------

STATION_KEYS = ("rfid_uid", "name", "description", "kind", "crop_id", "route_order", "map_x", "map_y", "active")
STATION_DEFAULTS = {"rfid_uid": "", "name": "", "description": "", "kind": "station", "crop_id": "", "route_order": "",
                    "map_x": "", "map_y": "", "active": "1"}
RFID_RE = re.compile(r"[A-Za-z0-9:_.\-]+")


def _station_values(st: Station) -> dict[str, str]:
    show = lambda v: "" if v is None else str(v)  # noqa: E731
    return {"rfid_uid": st.rfid_uid, "name": st.name, "description": show(st.description), "kind": st.kind,
            "crop_id": show(st.crop_id), "route_order": show(st.route_order), "map_x": show(st.map_x),
            "map_y": show(st.map_y), "active": "1" if st.active else "0"}


def _station_clean(session: Session, f: dict[str, str], st: Station | None) -> tuple[dict, dict]:
    e: dict[str, str] = {}
    rfid = _text(e, "rfid_uid", f["rfid_uid"], "RFID", RFID_MAX)
    if rfid and "rfid_uid" not in e:
        if not RFID_RE.fullmatch(rfid):
            e["rfid_uid"] = "RFID ใช้ได้เฉพาะตัวอักษร ตัวเลข และเครื่องหมาย : _ . -"
        else:
            taken = select(Station.id).where(Station.rfid_uid == rfid, Station.id != (st.id if st else 0))
            if session.scalar(taken) is not None:
                e["rfid_uid"] = RFID_TAKEN
    name = _text(e, "name", f["name"], "ชื่อสถานี", NAME_MAX)
    description = _text(e, "description", f["description"], "คำอธิบาย", TEXT_MAX, required=False)
    kind = f["kind"]
    if kind not in ("station", "home"):
        e["kind"] = "เลือกประเภทสถานีให้ถูกต้อง"
    elif kind == "home" and st is not None and _count(session, Batch.id, Batch.station_id == st.id):
        e["kind"] = "สถานีนี้มีล็อตผักอยู่ จึงเปลี่ยนเป็นจุดเริ่มต้นไม่ได้ (ลบหรือย้ายล็อตก่อน)"
    crop = _ref(e, session, "crop_id", Crop, f["crop_id"], "ชนิดผัก")
    if f["active"] not in ("0", "1"):
        e["active"] = "เลือกสถานะให้ถูกต้อง"
    clean = {
        "rfid_uid": rfid, "name": name, "description": description or None, "kind": kind,
        "crop_id": crop.id if crop else None,
        "route_order": _num(e, "route_order", f["route_order"], "ลำดับเส้นทาง", 0, 9999, integer=True),
        "map_x": _num(e, "map_x", f["map_x"], "ตำแหน่ง X", -100000, 100000),
        "map_y": _num(e, "map_y", f["map_y"], "ตำแหน่ง Y", -100000, 100000),
        "active": f["active"] == "1",
    }
    return clean, e


def _station_form(request: Request, session: Session, values: dict, errors: dict, station_id: int | None, status_code=200):
    crops = session.execute(select(Crop.id, Crop.name).order_by(Crop.name)).all()
    return render(request, "admin/station_form.html", status_code=status_code, values=values, errors=errors,
                  station_id=station_id, crops=crops)


@router.get("/stations")
def stations_list(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = q.strip()[:100]
    stmt = select(Station).order_by(Station.route_order.is_(None), Station.route_order, Station.id)
    if q:
        like = _like(q)
        stmt = stmt.where(or_(like(Station.name), like(Station.rfid_uid)))
    crops = dict(session.execute(select(Crop.id, Crop.name)).all())
    return render(request, "admin/stations_list.html", stations=session.scalars(stmt).all(), crops=crops, q=q,
                  total=_count(session, Station.id))


@router.get("/stations/new")
def station_new(request: Request, session: Session = Depends(get_session)):
    return _station_form(request, session, STATION_DEFAULTS, {}, None)


@router.get("/stations/{station_id}/edit")
def station_edit(request: Request, station_id: Id, session: Session = Depends(get_session)):
    st = session.get(Station, station_id)
    return _missing(request) if st is None else _station_form(request, session, _station_values(st), {}, st.id)


def _save_station(request: Request, session: Session, admin: AdminUser, f: dict, st: Station | None):
    station_id = st.id if st else None
    clean, errors = _station_clean(session, f, st)
    if not errors:
        if st is None:
            st = Station(**clean)
            session.add(st)
            session.flush()
            audit.record(session, admin.username, "station.create", f"station#{st.id}", st.name)
        else:
            audit.record(session, admin.username, "station.update", f"station#{st.id}", _update_detail(_apply(st, clean)))
        name = st.name
        if _commit(session, errors, "rfid_uid", RFID_TAKEN):
            return _done(request, "/admin/stations", f"{'บันทึก' if station_id else 'สร้าง'}สถานีแล้ว: {name}")
    return _station_form(request, session, f, errors, station_id, status_code=422)


@router.post("/stations", dependencies=CSRF)
def station_create(request: Request, f: dict = _fields(*STATION_KEYS), admin: AdminUser = Depends(deps.require_admin),
                   session: Session = Depends(get_session)):
    return _save_station(request, session, admin, f, None)


@router.post("/stations/{station_id}", dependencies=CSRF)
def station_update(request: Request, station_id: Id, f: dict = _fields(*STATION_KEYS),
                   admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    st = session.get(Station, station_id)
    return _missing(request) if st is None else _save_station(request, session, admin, f, st)


def _station_blocked(session: Session, st: Station):
    if _count(session, Capture.id, Capture.station_id == st.id) or _count(session, DailyStat.id, DailyStat.station_id == st.id):
        return ("สถานีนี้มีภาพที่ตรวจแล้ว จึงลบไม่ได้เพราะจะทำให้ประวัติหาย ให้ปิดใช้งานสถานีแทน "
                "(แก้ไขสถานี แล้วตั้งสถานะเป็นปิดใช้งาน)", f"/admin/stations/{st.id}/edit", "ไปแก้ไขสถานี")
    n = _count(session, Batch.id, Batch.station_id == st.id)
    if n:
        return (f"สถานีนี้มีล็อตผัก {n} ล็อต ต้องลบหรือย้ายล็อตก่อน หรือปิดใช้งานสถานีแทน", "/admin/batches", "ไปที่ล็อตผัก")
    return None


def _station_delete_page(request: Request, st: Station, blocked, status_code=200):
    return _delete_page(request, active="station", heading="ลบสถานี", item=st.name, action=f"/admin/stations/{st.id}/delete",
                        back="/admin/stations", blocked=blocked, status_code=status_code,
                        note="การลบถาวรและกู้คืนไม่ได้ ถ้าเพียงต้องการหยุดใช้งานชั่วคราว ให้ปิดใช้งานสถานีแทน")


@router.get("/stations/{station_id}/delete")
def station_delete_page(request: Request, station_id: Id, session: Session = Depends(get_session)):
    st = session.get(Station, station_id)
    return _missing(request) if st is None else _station_delete_page(request, st, _station_blocked(session, st))


@router.post("/stations/{station_id}/delete", dependencies=CSRF)
def station_delete(request: Request, station_id: Id, admin: AdminUser = Depends(deps.require_admin),
                   session: Session = Depends(get_session)):
    st = session.get(Station, station_id)
    if st is None:
        return _missing(request)
    if blocked := _station_blocked(session, st):
        return _station_delete_page(request, st, blocked, status_code=409)
    name = st.name
    audit.record(session, admin.username, "station.delete", f"station#{st.id}", name)
    session.delete(st)
    session.commit()
    return _done(request, "/admin/stations", f"ลบสถานีแล้ว: {name}")


# --- crops ------------------------------------------------------------------------------

CROP_KEYS = ("name", "labels", "prompt", "notes")


def _crop_values(crop: Crop) -> dict[str, str]:
    return {"name": crop.name, "labels": "\n".join(crop.labels), "prompt": crop.prompt, "notes": crop.notes or ""}


def _crop_clean(session: Session, f: dict[str, str], crop: Crop | None) -> tuple[dict, dict]:
    e: dict[str, str] = {}
    name = _text(e, "name", f["name"], "ชื่อชนิดผัก", NAME_MAX)
    if name and "name" not in e and session.scalar(select(Crop.id).where(Crop.name == name, Crop.id != (crop.id if crop else 0))):
        e["name"] = "ชื่อชนิดผักนี้ถูกใช้แล้ว"
    labels = [x.strip() for x in re.split(r"[\n\r,]", f["labels"]) if x.strip()]
    if not labels:
        e["labels"] = "ต้องมีป้ายกำกับอย่างน้อย 1 รายการ"
    elif len(labels) > LABELS_MAX:
        e["labels"] = f"ป้ายกำกับมากเกินไป (ไม่เกิน {LABELS_MAX} รายการ)"
    elif any(len(x) > LABEL_MAX for x in labels):
        e["labels"] = f"ป้ายกำกับแต่ละรายการยาวเกินไป (ไม่เกิน {LABEL_MAX} ตัวอักษร)"
    elif len(set(labels)) != len(labels):
        e["labels"] = "มีป้ายกำกับซ้ำกัน กรุณาใช้ชื่อที่ไม่ซ้ำ"
    prompt = _text(e, "prompt", f["prompt"], "คำสั่งสำหรับ AI", TEXT_MAX)
    notes = _text(e, "notes", f["notes"], "หมายเหตุ", TEXT_MAX, required=False)
    return {"name": name, "labels": labels, "prompt": prompt, "notes": notes or None}, e


def _crop_form(request: Request, values: dict, errors: dict, crop_id: int | None, status_code=200):
    return render(request, "admin/crop_form.html", status_code=status_code, values=values, errors=errors, crop_id=crop_id)


@router.get("/crops")
def crops_list(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = q.strip()[:100]
    stmt = select(Crop).order_by(Crop.name)
    if q:
        stmt = stmt.where(_like(q)(Crop.name))
    used = dict(session.execute(select(Station.crop_id, func.count(Station.id)).where(Station.crop_id.is_not(None))
                                .group_by(Station.crop_id)).all())
    return render(request, "admin/crops_list.html", crops=session.scalars(stmt).all(), used=used, q=q,
                  total=_count(session, Crop.id))


@router.get("/crops/new")
def crop_new(request: Request):
    return _crop_form(request, {"name": "", "labels": "", "prompt": "", "notes": ""}, {}, None)


@router.get("/crops/{crop_id}/edit")
def crop_edit(request: Request, crop_id: Id, session: Session = Depends(get_session)):
    crop = session.get(Crop, crop_id)
    return _missing(request) if crop is None else _crop_form(request, _crop_values(crop), {}, crop.id)


def _save_crop(request: Request, session: Session, admin: AdminUser, f: dict, crop: Crop | None):
    crop_id = crop.id if crop else None
    clean, errors = _crop_clean(session, f, crop)
    if not errors:
        if crop is None:
            crop = Crop(**clean)
            session.add(crop)
            session.flush()
            audit.record(session, admin.username, "crop.create", f"crop#{crop.id}", crop.name)
        else:
            audit.record(session, admin.username, "crop.update", f"crop#{crop.id}", _update_detail(_apply(crop, clean)))
        name = crop.name
        if _commit(session, errors, "name", "ชื่อชนิดผักนี้ถูกใช้แล้ว"):
            return _done(request, "/admin/crops", f"{'บันทึก' if crop_id else 'สร้าง'}ชนิดผักแล้ว: {name}")
    return _crop_form(request, f, errors, crop_id, status_code=422)


@router.post("/crops", dependencies=CSRF)
def crop_create(request: Request, f: dict = _fields(*CROP_KEYS), admin: AdminUser = Depends(deps.require_admin),
                session: Session = Depends(get_session)):
    return _save_crop(request, session, admin, f, None)


@router.post("/crops/{crop_id}", dependencies=CSRF)
def crop_update(request: Request, crop_id: Id, f: dict = _fields(*CROP_KEYS),
                admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    crop = session.get(Crop, crop_id)
    return _missing(request) if crop is None else _save_crop(request, session, admin, f, crop)


def _crop_blocked(session: Session, crop: Crop):
    parts = [f"{n} {label}" for n, label in (
        (_count(session, Station.id, Station.crop_id == crop.id), "สถานี"),
        (_count(session, Batch.id, Batch.crop_id == crop.id), "ล็อตผัก"),
        (_count(session, Capture.id, Capture.crop_id == crop.id), "ภาพที่ตรวจแล้ว")) if n]
    if parts:
        return (f"ชนิดผักนี้ถูกใช้งานอยู่ ({', '.join(parts)}) จึงลบไม่ได้ ต้องเปลี่ยนหรือลบรายการเหล่านั้นก่อน",
                "/admin/stations", "ไปที่สถานี")
    return None


def _crop_delete_page(request: Request, crop: Crop, blocked, status_code=200):
    return _delete_page(request, active="crop", heading="ลบชนิดผัก", item=crop.name, action=f"/admin/crops/{crop.id}/delete",
                        back="/admin/crops", blocked=blocked, status_code=status_code, note="การลบถาวรและกู้คืนไม่ได้")


@router.get("/crops/{crop_id}/delete")
def crop_delete_page(request: Request, crop_id: Id, session: Session = Depends(get_session)):
    crop = session.get(Crop, crop_id)
    return _missing(request) if crop is None else _crop_delete_page(request, crop, _crop_blocked(session, crop))


@router.post("/crops/{crop_id}/delete", dependencies=CSRF)
def crop_delete(request: Request, crop_id: Id, admin: AdminUser = Depends(deps.require_admin),
                session: Session = Depends(get_session)):
    crop = session.get(Crop, crop_id)
    if crop is None:
        return _missing(request)
    if blocked := _crop_blocked(session, crop):
        return _crop_delete_page(request, crop, blocked, status_code=409)
    name = crop.name
    audit.record(session, admin.username, "crop.delete", f"crop#{crop.id}", name)
    session.delete(crop)
    session.commit()
    return _done(request, "/admin/crops", f"ลบชนิดผักแล้ว: {name}")


# --- batches (lots) ------------------------------------------------------------------------

BATCH_KEYS = ("station_id", "crop_id", "planted_on", "harvested_on", "notes")


def _batch_values(b: Batch) -> dict[str, str]:
    return {"station_id": str(b.station_id), "crop_id": str(b.crop_id), "planted_on": b.planted_on.isoformat(),
            "harvested_on": b.harvested_on.isoformat() if b.harvested_on else "", "notes": b.notes or ""}


def _batch_clean(session: Session, f: dict[str, str]) -> tuple[dict, dict]:
    e: dict[str, str] = {}
    station = _ref(e, session, "station_id", Station, f["station_id"], "สถานี", required=True)
    if station is not None and station.kind == "home":
        e["station_id"] = "สถานีประเภทจุดเริ่มต้นสร้างล็อตไม่ได้ กรุณาเลือกสถานีตรวจ"
    crop = _ref(e, session, "crop_id", Crop, f["crop_id"], "ชนิดผัก", required=True)
    planted = _date(e, "planted_on", f["planted_on"], "วันที่ปลูก", required=True)
    harvested = _date(e, "harvested_on", f["harvested_on"], "วันที่เก็บเกี่ยว", required=False)
    if planted and harvested and harvested < planted:
        e["harvested_on"] = "วันที่เก็บเกี่ยวต้องไม่ก่อนวันที่ปลูก"
    notes = _text(e, "notes", f["notes"], "หมายเหตุ", TEXT_MAX, required=False)
    clean = {"station_id": station.id if station else None, "crop_id": crop.id if crop else None,
             "planted_on": planted, "harvested_on": harvested, "notes": notes or None}
    return clean, e


def _batch_form(request: Request, session: Session, values: dict, errors: dict, batch_id: int | None, status_code=200):
    rows = session.execute(select(Station.id, Station.name, Station.active).where(Station.kind != "home")
                           .order_by(Station.route_order.is_(None), Station.route_order, Station.id)).all()
    stations = [(i, name + ("" if active else " (ปิดใช้งาน)")) for i, name, active in rows]
    crops = session.execute(select(Crop.id, Crop.name).order_by(Crop.name)).all()
    return render(request, "admin/batch_form.html", status_code=status_code, values=values, errors=errors,
                  batch_id=batch_id, stations=stations, crops=crops)


@router.get("/batches")
def batches_list(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = q.strip()[:100]
    stmt = (select(Batch, Station.name, Crop.name).join(Station, Batch.station_id == Station.id)
            .join(Crop, Batch.crop_id == Crop.id).order_by(Batch.planted_on.desc(), Batch.id.desc()))
    if q:
        like = _like(q)
        stmt = stmt.where(or_(like(Station.name), like(Crop.name), like(Batch.notes)))
    rows = [(b, sname, cname) for b, sname, cname in session.execute(stmt).all()]
    return render(request, "admin/batches_list.html", rows=rows, q=q, total=_count(session, Batch.id))


@router.get("/batches/new")
def batch_new(request: Request, session: Session = Depends(get_session)):
    return _batch_form(request, session, {"station_id": "", "crop_id": "", "planted_on": date.today().isoformat(),
                                          "harvested_on": "", "notes": ""}, {}, None)


@router.get("/batches/{batch_id}/edit")
def batch_edit(request: Request, batch_id: Id, session: Session = Depends(get_session)):
    b = session.get(Batch, batch_id)
    return _missing(request) if b is None else _batch_form(request, session, _batch_values(b), {}, b.id)


def _save_batch(request: Request, session: Session, admin: AdminUser, f: dict, b: Batch | None):
    batch_id = b.id if b else None
    clean, errors = _batch_clean(session, f)
    if not errors:
        if b is None:
            b = Batch(**clean)
            session.add(b)
            session.flush()
            audit.record(session, admin.username, "batch.create", f"batch#{b.id}", f"station#{b.station_id} crop#{b.crop_id}")
        else:
            audit.record(session, admin.username, "batch.update", f"batch#{b.id}", _update_detail(_apply(b, clean)))
        session.commit()
        return _done(request, "/admin/batches", f"{'บันทึก' if batch_id else 'สร้าง'}ล็อตผักแล้ว")
    return _batch_form(request, session, f, errors, batch_id, status_code=422)


@router.post("/batches", dependencies=CSRF)
def batch_create(request: Request, f: dict = _fields(*BATCH_KEYS), admin: AdminUser = Depends(deps.require_admin),
                 session: Session = Depends(get_session)):
    return _save_batch(request, session, admin, f, None)


@router.post("/batches/{batch_id}", dependencies=CSRF)
def batch_update(request: Request, batch_id: Id, f: dict = _fields(*BATCH_KEYS),
                 admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    b = session.get(Batch, batch_id)
    return _missing(request) if b is None else _save_batch(request, session, admin, f, b)


def _batch_blocked(session: Session, b: Batch):
    if _count(session, DailyStat.id, DailyStat.batch_id == b.id):
        return ("ล็อตนี้มีสถิติรายวันอ้างอิงอยู่ จึงลบไม่ได้ ถ้าเก็บเกี่ยวแล้วให้ใส่วันที่เก็บเกี่ยวแทน",
                f"/admin/batches/{b.id}/edit", "ไปแก้ไขล็อต")
    return None


def _batch_delete_page(request: Request, b: Batch, session: Session, blocked, status_code=200):
    sname = session.scalar(select(Station.name).where(Station.id == b.station_id)) or "-"
    return _delete_page(request, active="batch", heading="ลบล็อตผัก", item=f"{sname} · ปลูกเมื่อ {b.planted_on.isoformat()}",
                        action=f"/admin/batches/{b.id}/delete", back="/admin/batches", blocked=blocked,
                        status_code=status_code, note="การลบถาวรและกู้คืนไม่ได้")


@router.get("/batches/{batch_id}/delete")
def batch_delete_page(request: Request, batch_id: Id, session: Session = Depends(get_session)):
    b = session.get(Batch, batch_id)
    return _missing(request) if b is None else _batch_delete_page(request, b, session, _batch_blocked(session, b))


@router.post("/batches/{batch_id}/delete", dependencies=CSRF)
def batch_delete(request: Request, batch_id: Id, admin: AdminUser = Depends(deps.require_admin),
                 session: Session = Depends(get_session)):
    b = session.get(Batch, batch_id)
    if b is None:
        return _missing(request)
    if blocked := _batch_blocked(session, b):
        return _batch_delete_page(request, b, session, blocked, status_code=409)
    audit.record(session, admin.username, "batch.delete", f"batch#{b.id}", f"station#{b.station_id} crop#{b.crop_id}")
    session.delete(b)
    session.commit()
    return _done(request, "/admin/batches", "ลบล็อตผักแล้ว")


# --- users ------------------------------------------------------------------------------------

def _password_errors(errors: dict, password: str, password2: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        errors["password"] = f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD_LEN} ตัวอักษร"
    elif len(password) > PASSWORD_MAX:
        errors["password"] = f"รหัสผ่านยาวเกินไป (ไม่เกิน {PASSWORD_MAX} ตัวอักษร)"
    elif password != password2:
        errors["password2"] = "รหัสผ่านทั้งสองช่องไม่ตรงกัน"


def _users_page(request: Request, session: Session, values: dict, errors: dict, status_code=200):
    users = session.scalars(select(AdminUser).order_by(AdminUser.id)).all()
    return render(request, "admin/users_list.html", status_code=status_code, users=users, values=values, errors=errors)


@router.get("/users")
def users_list(request: Request, session: Session = Depends(get_session)):
    return _users_page(request, session, {"username": ""}, {})


@router.post("/users", dependencies=CSRF)
def user_create(request: Request, f: dict = _fields("username", "password", "password2"),
                admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    e: dict[str, str] = {}
    username = _text(e, "username", f["username"], "ชื่อผู้ใช้", USERNAME_MAX)
    if username and "username" not in e:
        if len(username) < 3 or not username.isprintable():
            e["username"] = "ชื่อผู้ใช้ต้องยาวอย่างน้อย 3 ตัวอักษรและไม่มีอักขระควบคุม"
        elif session.scalar(select(AdminUser.id).where(AdminUser.username == username)):
            e["username"] = "ชื่อผู้ใช้นี้ถูกใช้แล้ว"
    _password_errors(e, f["password"], f["password2"])
    if not e:
        user = AdminUser(username=username, password_hash=security.hash_password(f["password"]))
        session.add(user)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            e["username"] = "ชื่อผู้ใช้นี้ถูกใช้แล้ว"
        else:
            audit.record(session, admin.username, "user.create", f"user#{user.id} {username}")
            session.commit()
            return _done(request, "/admin/users", f"สร้างผู้ดูแลแล้ว: {username}")
    return _users_page(request, session, {"username": f["username"]}, e, status_code=422)  # passwords are never echoed


def _password_page(request: Request, user: AdminUser, errors: dict, status_code=200):
    return render(request, "admin/user_password.html", status_code=status_code, user=user, errors=errors)


@router.get("/users/{user_id}/password")
def user_password_page(request: Request, user_id: Id, session: Session = Depends(get_session)):
    user = session.get(AdminUser, user_id)
    return _missing(request) if user is None else _password_page(request, user, {})


@router.post("/users/{user_id}/password", dependencies=CSRF)
def user_password(request: Request, user_id: Id, f: dict = _fields("password", "password2"),
                  admin: AdminUser = Depends(deps.require_admin), session: Session = Depends(get_session)):
    user = session.get(AdminUser, user_id)
    if user is None:
        return _missing(request)
    e: dict[str, str] = {}
    _password_errors(e, f["password"], f["password2"])
    if e:
        return _password_page(request, user, e, status_code=422)
    user.password_hash = security.hash_password(f["password"])
    audit.record(session, admin.username, "user.password", f"user#{user.id} {user.username}")  # no detail: never the password
    session.commit()
    return _done(request, "/admin/users", f"เปลี่ยนรหัสผ่านของ {user.username} แล้ว")


def _user_blocked(session: Session):
    if _count(session, AdminUser.id) <= 1:
        return ("นี่คือผู้ดูแลคนสุดท้ายของระบบ ลบไม่ได้ ไม่เช่นนั้นจะไม่มีใครเข้าสู่ระบบได้ กรุณาสร้างผู้ดูแลคนใหม่ก่อน",
                "/admin/users", "ไปที่ผู้ดูแล")
    return None


def _user_delete_page(request: Request, user: AdminUser, blocked, me: AdminUser, status_code=200):
    note = "นี่คือบัญชีของคุณ หลังลบแล้วคุณจะถูกออกจากระบบทันที" if user.id == me.id else "การลบถาวรและกู้คืนไม่ได้"
    return _delete_page(request, active="users", heading="ลบผู้ดูแล", item=user.username, action=f"/admin/users/{user.id}/delete",
                        back="/admin/users", blocked=blocked, status_code=status_code, note=note)


@router.get("/users/{user_id}/delete")
def user_delete_page(request: Request, user_id: Id, admin: AdminUser = Depends(deps.require_admin),
                     session: Session = Depends(get_session)):
    user = session.get(AdminUser, user_id)
    return _missing(request) if user is None else _user_delete_page(request, user, _user_blocked(session), admin)


@router.post("/users/{user_id}/delete", dependencies=CSRF)
def user_delete(request: Request, user_id: Id, admin: AdminUser = Depends(deps.require_admin),
                session: Session = Depends(get_session)):
    user = session.get(AdminUser, user_id)
    if user is None:
        return _missing(request)
    if blocked := _user_blocked(session):
        return _user_delete_page(request, user, blocked, admin, status_code=409)
    name, was_me = user.username, user.id == admin.id
    audit.record(session, admin.username, "user.delete", f"user#{user.id} {name}")
    session.delete(user)
    session.commit()
    if was_me:
        response = RedirectResponse("/login?logged_out=1", status_code=303)
        deps.clear_session(response)
        return response
    return _done(request, "/admin/users", f"ลบผู้ดูแลแล้ว: {name}")


# --- audit log -----------------------------------------------------------------------------------

def _thai_time(dt) -> str:
    # naive datetimes come back from SQLite as UTC; Thailand is UTC+7 with no DST
    return (dt + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


@router.get("/audit")
def audit_list(request: Request, page: str = "1", session: Session = Depends(get_session)):
    total = _count(session, AuditLog.id)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    try:
        number = min(max(int(page), 1), pages)
    except ValueError:
        number = 1
    logs = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(PAGE_SIZE).offset((number - 1) * PAGE_SIZE)).all()
    rows = [{"at": _thai_time(r.at), "actor": r.actor, "action": ACTION_TH.get(r.action, r.action), "target": r.target,
             "detail": r.detail or ""} for r in logs]
    return render(request, "admin/audit_list.html", rows=rows, page=number, pages=pages, total=total)
