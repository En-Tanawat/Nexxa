import dataclasses
import re
from datetime import date

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import analyzer, main, storage
from app.analyzer import AnalysisResult
from app.models import AdminUser, AuditLog, Batch, Capture, Crop, Round, Station
from app.web import audit, deps, security
from app.web.security import verify_password
from tests.test_api import GOOD
from tests.test_web_auth import PASSWORD, _A11y, _client, _csrf, _login, admin, engine  # noqa: F401

RFID_TAKEN = "RFID นี้ถูกใช้แล้ว"
XSS = "<script>alert(1)</script>"
ESCAPED_XSS = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture
def c(engine, admin):
    client = _client()
    _login(client)
    client.csrf = _csrf(client.get("/admin").text)
    return client


def post(client, path, **data):
    return client.post(path, data={"csrf_token": client.csrf, **data})


def db(engine):
    return Session(engine)


def make_crop(engine, name="กรีนคอส", labels=("ปกติ", "ใบเหลือง")):
    with db(engine) as s:
        crop = Crop(name=name, labels=list(labels), prompt="ดูภาพใบผัก", notes=None)
        s.add(crop)
        s.commit()
        return crop.id


def make_station(engine, rfid="UID-1", name="สถานี 1", kind="station", crop_id=None, active=True):
    with db(engine) as s:
        st = Station(rfid_uid=rfid, name=name, kind=kind, crop_id=crop_id, active=active)
        s.add(st)
        s.commit()
        return st.id


def make_batch(engine, station_id, crop_id, planted=date(2026, 1, 5), harvested=None):
    with db(engine) as s:
        b = Batch(station_id=station_id, crop_id=crop_id, planted_on=planted, harvested_on=harvested)
        s.add(b)
        s.commit()
        return b.id


def audits(engine):
    with db(engine) as s:
        return list(s.scalars(select(AuditLog).order_by(AuditLog.id)))


def count(engine, model):
    with db(engine) as s:
        return s.scalar(select(func.count()).select_from(model))


STATION_FORM = dict(rfid_uid="04A1B2C3", name="สถานี A", description="", kind="station", crop_id="",
                    route_order="1", map_x="1.5", map_y="2", active="1")


# --- access control ------------------------------------------------------------

GET_PATHS = [
    "/admin", "/admin/stations", "/admin/stations/new", "/admin/stations/1/edit", "/admin/stations/1/delete",
    "/admin/crops", "/admin/crops/new", "/admin/crops/1/edit", "/admin/crops/1/delete",
    "/admin/batches", "/admin/batches/new", "/admin/batches/1/edit", "/admin/batches/1/delete",
    "/admin/users", "/admin/users/1/password", "/admin/users/1/delete", "/admin/audit",
]
POST_PATHS = [
    "/admin/stations", "/admin/stations/1", "/admin/stations/1/delete",
    "/admin/crops", "/admin/crops/1", "/admin/crops/1/delete",
    "/admin/batches", "/admin/batches/1", "/admin/batches/1/delete",
    "/admin/users", "/admin/users/1/password", "/admin/users/1/delete",
]


@pytest.mark.parametrize("path", GET_PATHS)
def test_get_routes_redirect_to_login(engine, path):
    r = _client().get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_routes_redirect_without_login(engine, path):
    r = _client().post(path, data={"csrf_token": "x"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_without_csrf_is_403_html(c, path):
    r = c.post(path, data={"name": "x"})
    assert r.status_code == 403
    assert "text/html" in r.headers["content-type"] and "โหลดหน้าใหม่" in r.text


def test_csrf_failure_on_api_path_stays_json(engine):
    api = FastAPI()

    @api.post("/api/admin/x", dependencies=[Depends(deps.require_csrf)])
    def x():
        return {}

    api.dependency_overrides = main.app.dependency_overrides
    r = TestClient(api).post("/api/admin/x", data={"a": "b"})
    assert r.status_code == 403 and r.headers["content-type"].startswith("application/json")


def test_expired_session_on_post_redirects_back_to_the_form_page(c):
    c.cookies.clear()
    c.cookies.set("nexxa_session", security.sign_session({"uid": 1, "csrf": "t" * 40}, max_age_s=10, now=0))
    r = c.post("/admin/stations", data={"csrf_token": "t" * 40, **STATION_FORM},
               headers={"referer": "http://testserver/admin/stations/new"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin/stations/new"
    r = c.post("/admin/stations", data=STATION_FORM, headers={"referer": "http://evil.example/admin/x"})
    assert r.headers["location"] == "/login?next=/admin"  # foreign referer is ignored


def test_flash_text_is_truncated_to_fit_the_cookie(engine):
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "scheme": "http",
             "server": ("testserver", 80)}
    resp = Response()
    deps.flash(Request(scope), resp, "ก" * 5000)
    token = resp.headers["set-cookie"].split("nexxa_session=")[1].split(";")[0].strip('"')
    text = security.read_session(token)["flash"]["text"]
    assert len(text) <= 200 and text.endswith("…")


def test_overview_handler_lives_in_routes_admin():
    from app.web import routes_auth
    assert not hasattr(routes_auth, "admin_home")


def test_audit_record_adds_row_without_committing(engine):
    with db(engine) as s:
        audit.record(s, "admin", "x.create", "x#1", "d")
        s.rollback()
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 0
        audit.record(s, "admin", "x.create", "x#1")
        s.commit()
        row = s.scalar(select(AuditLog))
        assert (row.actor, row.action, row.target, row.detail) == ("admin", "x.create", "x#1", None) and row.at


# --- stations ------------------------------------------------------------------

def test_station_create_edit_delete_roundtrip(c, engine):
    crop_id = make_crop(engine)
    r = post(c, "/admin/stations", **{**STATION_FORM, "crop_id": str(crop_id), "description": "โรงเรือน 1"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/stations"
    with db(engine) as s:
        st = s.scalar(select(Station))
        assert (st.rfid_uid, st.name, st.crop_id, st.route_order, st.map_x, st.map_y, st.active) == \
            ("04A1B2C3", "สถานี A", crop_id, 1, 1.5, 2.0, True)
        sid = st.id
    listing = c.get("/admin/stations")
    assert "สถานี A" in listing.text and "04A1B2C3" in listing.text and "สร้างสถานีแล้ว" in listing.text
    assert "สร้างสถานีแล้ว" not in c.get("/admin/stations").text  # flash shows once

    page = c.get(f"/admin/stations/{sid}/edit")
    assert page.status_code == 200 and 'value="04A1B2C3"' in page.text and 'value="สถานี A"' in page.text
    r = post(c, f"/admin/stations/{sid}", **{**STATION_FORM, "name": "สถานี B", "active": "0"})
    assert r.status_code == 303
    with db(engine) as s:
        st = s.get(Station, sid)
        assert st.name == "สถานี B" and st.active is False

    confirm = c.get(f"/admin/stations/{sid}/delete")
    assert confirm.status_code == 200 and "สถานี B" in confirm.text and f'action="/admin/stations/{sid}/delete"' in confirm.text
    assert "confirm(" not in confirm.text
    assert post(c, f"/admin/stations/{sid}/delete").status_code == 303
    assert count(engine, Station) == 0
    assert [a.action for a in audits(engine)] == ["station.create", "station.update", "station.delete"]


def test_station_duplicate_rfid_is_rejected_with_typed_values_kept(c, engine):
    make_station(engine, rfid="04A1B2C3")
    r = post(c, "/admin/stations", **{**STATION_FORM, "name": "ชื่อที่พิมพ์ไว้"})
    assert r.status_code == 422 and RFID_TAKEN in r.text and "ชื่อที่พิมพ์ไว้" in r.text
    assert 'aria-invalid="true"' in r.text and "f-rfid_uid-error" in r.text  # error sits next to the field
    assert count(engine, Station) == 1 and audits(engine) == []


def test_station_update_cannot_steal_another_rfid_but_may_keep_its_own(c, engine):
    make_station(engine, rfid="A-1")
    sid = make_station(engine, rfid="B-2", name="สอง")
    assert RFID_TAKEN in post(c, f"/admin/stations/{sid}", **{**STATION_FORM, "rfid_uid": "A-1"}).text
    assert post(c, f"/admin/stations/{sid}", **{**STATION_FORM, "rfid_uid": "B-2"}).status_code == 303


@pytest.mark.parametrize("field,value,message", [
    ("name", "", "กรุณากรอก"), ("name", "ก" * 121, "ไม่เกิน 120"), ("rfid_uid", "", "กรุณากรอก"),
    ("rfid_uid", "A" * 65, "ไม่เกิน 64"), ("rfid_uid", "มี ช่องว่าง", "RFID ใช้ได้เฉพาะ"),
    ("description", "ก" * 2001, "ไม่เกิน 2000"), ("kind", "x", "ประเภท"), ("route_order", "abc", "ตัวเลข"),
    ("route_order", "-1", "ตัวเลข"), ("map_x", "nan", "ตัวเลข"), ("map_y", "inf", "ตัวเลข"), ("crop_id", "999", "ชนิดผัก"),
    ("active", "maybe", "สถานะ"),
])
def test_station_field_validation_is_server_side(c, engine, field, value, message):
    r = post(c, "/admin/stations", **{**STATION_FORM, field: value})
    assert r.status_code == 422 and message in r.text
    assert count(engine, Station) == 0


def test_station_cannot_become_home_while_it_has_batches(c, engine):
    crop_id = make_crop(engine)
    sid = make_station(engine, crop_id=crop_id)
    make_batch(engine, sid, crop_id)
    r = post(c, f"/admin/stations/{sid}", **{**STATION_FORM, "kind": "home"})
    assert r.status_code == 422 and "ล็อต" in r.text
    with db(engine) as s:
        assert s.get(Station, sid).kind == "station"


def test_delete_station_with_captures_is_refused_and_suggests_deactivating(c, engine):
    sid = make_station(engine)
    with db(engine) as s:
        s.add(Capture(station_id=sid, image_path="x.png"))
        s.commit()
    page = c.get(f"/admin/stations/{sid}/delete")
    assert "ปิดใช้งาน" in page.text and f'action="/admin/stations/{sid}/delete"' not in page.text
    r = post(c, f"/admin/stations/{sid}/delete")
    assert r.status_code == 409 and "ปิดใช้งาน" in r.text
    assert count(engine, Station) == 1 and audits(engine) == []


def test_delete_station_with_batches_is_refused(c, engine):
    crop_id = make_crop(engine)
    sid = make_station(engine, crop_id=crop_id)
    make_batch(engine, sid, crop_id)
    assert post(c, f"/admin/stations/{sid}/delete").status_code == 409
    assert count(engine, Station) == 1


def test_station_search_filters_list(c, engine):
    make_station(engine, rfid="AAA", name="แปลงเหนือ")
    make_station(engine, rfid="BBB", name="แปลงใต้")
    html = c.get("/admin/stations", params={"q": "เหนือ"}).text
    assert "แปลงเหนือ" in html and "แปลงใต้" not in html
    assert "BBB" in c.get("/admin/stations", params={"q": "bb"}).text
    assert "ไม่พบ" in c.get("/admin/stations", params={"q": "%"}).text  # LIKE wildcards are literal


def test_script_in_station_name_is_escaped_everywhere(c, engine):
    r = post(c, "/admin/stations", **{**STATION_FORM, "name": XSS})
    assert r.status_code == 303
    assert count(engine, Station) == 1
    for path in ("/admin/stations", "/admin/stations/1/edit", "/admin/stations/1/delete", "/admin/audit"):
        html = c.get(path).text
        assert "<script>alert(1)" not in html and ESCAPED_XSS in html, path
    r = post(c, "/admin/stations", **{**STATION_FORM, "name": XSS, "rfid_uid": "04A1B2C3"})  # re-rendered error form
    assert "<script>alert(1)" not in r.text and ESCAPED_XSS in r.text


def test_missing_ids_give_a_friendly_404(c):
    for path in ("/admin/stations/9/edit", "/admin/crops/9/delete", "/admin/batches/9/edit", "/admin/users/9/password"):
        r = c.get(path)
        assert r.status_code == 404 and "text/html" in r.headers["content-type"] and "ไม่พบ" in r.text
    assert post(c, "/admin/stations/9", **STATION_FORM).status_code == 404


def test_new_station_accepts_captures_from_the_robot(c, engine, tmp_path, monkeypatch):
    make_crop(engine)
    post(c, "/admin/stations", **{**STATION_FORM, "rfid_uid": "FRESH-TAG", "crop_id": "1"})
    monkeypatch.setattr(storage, "settings", dataclasses.replace(storage.settings, image_dir=tmp_path))
    monkeypatch.setattr(analyzer, "analyze_leaf", lambda image_bytes, crop, client=None: AnalysisResult("ปกติ", 0.9, "ok", False, "m"))
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "test-device-key")  # test-only value
    robot = TestClient(main.app, headers={"X-Device-Key": "test-device-key"})
    rid = robot.post("/rounds").json()["round_id"]
    r = robot.post("/captures", data={"rfid_uid": "FRESH-TAG", "round_id": rid}, files={"image": ("a.png", GOOD, "image/png")})
    assert r.status_code == 200 and r.json()["status"] == "analyzed"
    assert robot.post("/captures", data={"rfid_uid": "NOPE", "round_id": rid}, files={"image": ("a.png", GOOD, "image/png")}).status_code == 404
    assert count(engine, Round) == 1


# --- crops -----------------------------------------------------------------------

CROP_FORM = dict(name="เรดโอ๊ค", labels="ปกติ, ใบเหลือง\nจุดน้ำตาล", prompt="ดูภาพใบผัก", notes="")


def test_crop_create_edit_delete_roundtrip(c, engine):
    r = post(c, "/admin/crops", **CROP_FORM)
    assert r.status_code == 303 and r.headers["location"] == "/admin/crops"
    with db(engine) as s:
        crop = s.scalar(select(Crop))
        assert crop.name == "เรดโอ๊ค" and crop.labels == ["ปกติ", "ใบเหลือง", "จุดน้ำตาล"] and crop.notes is None
        cid = crop.id
    assert "เรดโอ๊ค" in c.get("/admin/crops").text
    edit = c.get(f"/admin/crops/{cid}/edit").text
    assert "ปกติ\nใบเหลือง\nจุดน้ำตาล" in edit  # one label per line when editing
    assert post(c, f"/admin/crops/{cid}", **{**CROP_FORM, "labels": "ปกติ\nเน่า", "notes": "หมายเหตุ"}).status_code == 303
    with db(engine) as s:
        crop = s.get(Crop, cid)
        assert crop.labels == ["ปกติ", "เน่า"] and crop.notes == "หมายเหตุ"
    assert c.get(f"/admin/crops/{cid}/delete").status_code == 200
    assert post(c, f"/admin/crops/{cid}/delete").status_code == 303
    assert count(engine, Crop) == 0
    assert [a.action for a in audits(engine)] == ["crop.create", "crop.update", "crop.delete"]


@pytest.mark.parametrize("labels,message", [
    ("", "อย่างน้อย 1"), (" , \n ,", "อย่างน้อย 1"), ("ปกติ\nปกติ", "ซ้ำ"), ("ปกติ, ปกติ ", "ซ้ำ"),
    ("ก" * 61, "ไม่เกิน 60"), ("\n".join(f"L{i}" for i in range(21)), "ไม่เกิน 20"),
])
def test_crop_labels_validation(c, engine, labels, message):
    r = post(c, "/admin/crops", **{**CROP_FORM, "labels": labels})
    assert r.status_code == 422 and message in r.text
    assert count(engine, Crop) == 0


@pytest.mark.parametrize("field,value,message", [
    ("name", "", "กรุณากรอก"), ("name", "ก" * 121, "ไม่เกิน 120"), ("prompt", "", "กรุณากรอก"),
    ("prompt", "ก" * 2001, "ไม่เกิน 2000"), ("notes", "ก" * 2001, "ไม่เกิน 2000"),
])
def test_crop_field_validation(c, engine, field, value, message):
    r = post(c, "/admin/crops", **{**CROP_FORM, field: value})
    assert r.status_code == 422 and message in r.text and count(engine, Crop) == 0


def test_crop_duplicate_name_rejected_and_values_kept(c, engine):
    make_crop(engine, name="เรดโอ๊ค")
    r = post(c, "/admin/crops", **{**CROP_FORM, "labels": "พิมพ์ไว้1\nพิมพ์ไว้2"})
    assert r.status_code == 422 and "ชื่อชนิดผักนี้ถูกใช้แล้ว" in r.text and "พิมพ์ไว้1\nพิมพ์ไว้2" in r.text
    assert count(engine, Crop) == 1


def test_delete_crop_still_in_use_is_refused(c, engine):
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    assert post(c, f"/admin/crops/{cid}/delete").status_code == 409
    with db(engine) as s:
        s.get(Station, sid).crop_id = None
        s.commit()
    make_batch(engine, make_station(engine, rfid="U2"), cid)
    r = post(c, f"/admin/crops/{cid}/delete")
    assert r.status_code == 409 and "ถูกใช้งานอยู่" in r.text and count(engine, Crop) == 1
    assert audits(engine) == []


# --- batches ---------------------------------------------------------------------

def batch_form(sid, cid, **over):
    return {"station_id": str(sid), "crop_id": str(cid), "planted_on": "2026-02-01",
            "harvested_on": "", "notes": "", **over}


def test_batch_create_edit_delete_roundtrip(c, engine):
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    r = post(c, "/admin/batches", **batch_form(sid, cid))
    assert r.status_code == 303 and r.headers["location"] == "/admin/batches"
    with db(engine) as s:
        b = s.scalar(select(Batch))
        assert (b.station_id, b.crop_id, b.planted_on, b.harvested_on, b.notes) == (sid, cid, date(2026, 2, 1), None, None)
        bid = b.id
    html = c.get("/admin/batches").text
    assert "สถานี 1" in html and "กรีนคอส" in html and "2026-02-01" in html and "กำลังปลูก" in html
    assert post(c, f"/admin/batches/{bid}", **batch_form(sid, cid, harvested_on="2026-02-01", notes="เก็บแล้ว")).status_code == 303
    with db(engine) as s:  # harvested the same day as planting is allowed (>=)
        b = s.get(Batch, bid)
        assert b.harvested_on == date(2026, 2, 1) and b.notes == "เก็บแล้ว"
    assert "เก็บเกี่ยวแล้ว" in c.get("/admin/batches").text
    assert c.get(f"/admin/batches/{bid}/edit").status_code == 200
    assert c.get(f"/admin/batches/{bid}/delete").status_code == 200
    assert post(c, f"/admin/batches/{bid}/delete").status_code == 303 and count(engine, Batch) == 0
    assert [a.action for a in audits(engine)] == ["batch.create", "batch.update", "batch.delete"]


def test_batch_harvest_before_planting_is_rejected(c, engine):
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    r = post(c, "/admin/batches", **batch_form(sid, cid, harvested_on="2026-01-31"))
    assert r.status_code == 422 and "ก่อนวันที่ปลูก" in r.text and "f-harvested_on-error" in r.text
    assert 'value="2026-02-01"' in r.text and count(engine, Batch) == 0


def test_batch_on_home_station_is_rejected(c, engine):
    cid = make_crop(engine)
    home = make_station(engine, rfid="HOME", name="โฮมเบส-X", kind="home")
    r = post(c, "/admin/batches", **batch_form(home, cid))
    assert r.status_code == 422 and "สถานีตรวจ" in r.text and count(engine, Batch) == 0
    assert "โฮมเบส-X" not in c.get("/admin/batches/new").text  # not even offered


@pytest.mark.parametrize("over,message", [
    ({"planted_on": ""}, "วันที่ปลูก"), ({"planted_on": "2026-02-30"}, "วันที่ปลูก"),
    ({"harvested_on": "ไม่ใช่วันที่"}, "วันที่เก็บเกี่ยว"), ({"station_id": "999"}, "สถานี"),
    ({"crop_id": "999"}, "ชนิดผัก"), ({"notes": "ก" * 2001}, "ไม่เกิน 2000"), ({"planted_on": "0001-01-01"}, "วันที่ปลูก"),
])
def test_batch_field_validation(c, engine, over, message):
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    r = post(c, "/admin/batches", **batch_form(sid, cid, **over))
    assert r.status_code == 422 and message in r.text and count(engine, Batch) == 0


def test_batch_update_cannot_move_to_home_station(c, engine):
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    home = make_station(engine, rfid="H", kind="home")
    bid = make_batch(engine, sid, cid)
    assert post(c, f"/admin/batches/{bid}", **batch_form(home, cid)).status_code == 422


def test_batch_with_daily_stats_cannot_be_deleted(c, engine):
    from app.models import DailyStat
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    bid = make_batch(engine, sid, cid)
    with db(engine) as s:
        s.add(DailyStat(day=date(2026, 2, 2), station_id=sid, batch_id=bid, n_captures=1, n_quality_ok=1,
                        n_normal=1, n_yellow=0, n_brown=0, n_review=0, mean_confidence=0.9))
        s.commit()
    assert post(c, f"/admin/batches/{bid}/delete").status_code == 409 and count(engine, Batch) == 1


# --- users -----------------------------------------------------------------------

NEW_PASSWORD = "new-test-password-1"  # test-only value


def test_users_page_lists_admins_and_creates_one(c, engine):
    assert "admin" in c.get("/admin/users").text
    r = post(c, "/admin/users", username="somchai", password=NEW_PASSWORD, password2=NEW_PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/admin/users"
    with db(engine) as s:
        u = s.scalar(select(AdminUser).where(AdminUser.username == "somchai"))
        assert verify_password(NEW_PASSWORD, u.password_hash) and NEW_PASSWORD not in u.password_hash
    other = _client()  # the new account can sign in
    assert _login(other, "somchai", NEW_PASSWORD).headers["location"] == "/admin"


@pytest.mark.parametrize("data,message", [
    (dict(username="", password=NEW_PASSWORD, password2=NEW_PASSWORD), "กรุณากรอก"),
    (dict(username="x" * 65, password=NEW_PASSWORD, password2=NEW_PASSWORD), "ไม่เกิน 64"),
    (dict(username="admin", password=NEW_PASSWORD, password2=NEW_PASSWORD), "ถูกใช้แล้ว"),
    (dict(username="ok-user", password="short", password2="short"), "อย่างน้อย 10"),
    (dict(username="ok-user", password=NEW_PASSWORD, password2="different-pass-1"), "ไม่ตรงกัน"),
    (dict(username="ok-user", password="p" * 129, password2="p" * 129), "ไม่เกิน 128"),
])
def test_user_create_validation(c, engine, data, message):
    before = count(engine, AdminUser)
    r = post(c, "/admin/users", **data)
    assert r.status_code == 422 and message in r.text
    assert count(engine, AdminUser) == before
    assert data["password"] not in r.text  # never echoed back


def test_change_password_then_login_with_it(c, engine):
    with db(engine) as s:
        uid = s.scalar(select(AdminUser.id))
    assert c.get(f"/admin/users/{uid}/password").status_code == 200
    r = post(c, f"/admin/users/{uid}/password", password="short", password2="short")
    assert r.status_code == 422 and "อย่างน้อย 10" in r.text
    r = post(c, f"/admin/users/{uid}/password", password=NEW_PASSWORD, password2=NEW_PASSWORD)
    assert r.status_code == 303
    assert _login(_client(), password=PASSWORD).status_code == 401
    assert _login(_client(), password=NEW_PASSWORD).status_code == 303


def test_audit_never_contains_passwords(c, engine):
    post(c, "/admin/users", username="somchai", password=NEW_PASSWORD, password2=NEW_PASSWORD)
    with db(engine) as s:
        uid = s.scalar(select(AdminUser.id).where(AdminUser.username == "somchai"))
    post(c, f"/admin/users/{uid}/password", password="another-test-pass-2", password2="another-test-pass-2")
    post(c, f"/admin/users/{uid}/delete")
    rows = audits(engine)
    assert [a.action for a in rows] == ["user.create", "user.password", "user.delete"]
    blob = " ".join(f"{a.actor} {a.action} {a.target} {a.detail}" for a in rows)
    for secret in (NEW_PASSWORD, "another-test-pass-2", PASSWORD):
        assert secret not in blob
    page = c.get("/admin/audit").text
    assert NEW_PASSWORD not in page and "somchai" in page


def test_delete_other_admin_and_self(c, engine):
    post(c, "/admin/users", username="second", password=NEW_PASSWORD, password2=NEW_PASSWORD)
    with db(engine) as s:
        me = s.scalar(select(AdminUser.id).where(AdminUser.username == "admin"))
        other = s.scalar(select(AdminUser.id).where(AdminUser.username == "second"))
    assert c.get(f"/admin/users/{other}/delete").status_code == 200
    assert post(c, f"/admin/users/{other}/delete").status_code == 303
    assert count(engine, AdminUser) == 1


def test_cannot_delete_the_last_admin_even_yourself(c, engine):
    with db(engine) as s:
        me = s.scalar(select(AdminUser.id))
    page = c.get(f"/admin/users/{me}/delete")
    assert "ผู้ดูแลคนสุดท้าย" in page.text and f'action="/admin/users/{me}/delete"' not in page.text
    r = post(c, f"/admin/users/{me}/delete")
    assert r.status_code == 409 and "ผู้ดูแลคนสุดท้าย" in r.text
    assert count(engine, AdminUser) == 1 and audits(engine) == []
    assert c.get("/admin").status_code == 200


def test_deleting_yourself_when_not_last_logs_you_out(c, engine):
    post(c, "/admin/users", username="second", password=NEW_PASSWORD, password2=NEW_PASSWORD)
    with db(engine) as s:
        me = s.scalar(select(AdminUser.id).where(AdminUser.username == "admin"))
    r = post(c, f"/admin/users/{me}/delete")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/admin").status_code == 303


# --- audit -----------------------------------------------------------------------

def test_audit_pages_newest_first_fifty_per_page(c, engine):
    with db(engine) as s:
        for i in range(120):
            audit.record(s, "admin", "station.update", f"station#{i}", None)
        s.commit()
    p1 = c.get("/admin/audit").text
    assert p1.count("<tr>") == 51 and "station#119" in p1 and "station#70" in p1 and "station#69" not in p1
    assert 'href="/admin/audit?page=2"' in p1 and "หน้า 1 จาก 3" in p1
    p3 = c.get("/admin/audit", params={"page": 3}).text
    assert p3.count("<tr>") == 21 and "station#0<" in p3 and "page=4" not in p3
    for bad in ("abc", "0", "-5", "99999999999999999999"):
        assert c.get("/admin/audit", params={"page": bad}).status_code == 200
    assert "หน้า 3 จาก 3" in c.get("/admin/audit", params={"page": 99}).text


def test_audit_empty_state(c):
    assert "ยังไม่มีบันทึก" in c.get("/admin/audit").text


# --- structure -------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/admin/stations", "/admin/stations/new", "/admin/crops", "/admin/crops/new", "/admin/batches",
    "/admin/batches/new", "/admin/users", "/admin/users/1/password", "/admin/users/1/delete", "/admin/audit",
    "/admin/stations/1/delete", "/admin/crops/1/delete", "/admin/batches/1/delete",
])
def test_crud_pages_are_thai_and_labelled(c, engine, path):
    cid = make_crop(engine)
    sid = make_station(engine, crop_id=cid)
    make_batch(engine, sid, cid)
    r = c.get(path)
    assert r.status_code == 200
    p = _A11y()
    p.feed(r.text)
    assert p.lang == "th"
    for inp in p.inputs:
        assert inp.get("id") in p.label_for, f"input without label: {inp}"
    assert all(p.buttons)
    assert re.search(r"[\U0001F300-\U0001FAFF☀-➿]", r.text) is None
