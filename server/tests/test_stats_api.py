import json
import re
import threading
from types import SimpleNamespace
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main, stats
from app.db import get_session, init_db
from app.models import Analysis, AppSetting, AdminUser, Batch, Capture, Crop, DailyStat, ProviderKey, Station
from app.web import routes_public_api
from tests.test_api import _post, env  # noqa: F401

NORMAL, YELLOW, BROWN = "ปกติ", "ใบเหลือง", "จุดน้ำตาล"
D10, D11 = date(2026, 3, 10), date(2026, 3, 11)


@pytest.fixture
def eng():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(e)
    return e


@pytest.fixture
def client(eng, monkeypatch):
    def override():
        with Session(eng) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    monkeypatch.setattr(routes_public_api, "_hits", {})
    yield TestClient(main.app)  # no login, no device key
    main.app.dependency_overrides.clear()


def seed_basic(eng):
    with Session(eng) as s:
        crop = Crop(name="กรีนคอส", labels=[NORMAL, YELLOW, BROWN], prompt="p", notes=None)
        s.add(crop)
        s.flush()
        s1 = Station(rfid_uid="U1", name="สถานี 1", crop_id=crop.id, route_order=1, map_x=1.0, map_y=2.0)
        s2 = Station(rfid_uid="U2", name="สถานี 2", crop_id=crop.id, route_order=2, map_x=3.0, map_y=4.0)
        s.add_all([s1, s2])
        s.commit()
        return crop.id, s1.id, s2.id


def cap(s, station_id, day, hour=8, ok=True, label=..., conf=None, review=False, note=None):
    c = Capture(station_id=station_id, taken_at=datetime(day.year, day.month, day.day, hour), image_path="/x.jpg",
                quality_ok=ok)
    s.add(c)
    s.flush()
    if label is not ...:
        s.add(Analysis(capture_id=c.id, label=label, confidence=conf, needs_review=review, note=note))
    return c


def rows(eng):
    with Session(eng) as s:
        return s.scalars(select(DailyStat).order_by(DailyStat.day, DailyStat.station_id, DailyStat.id)).all()


def basic_data(eng):
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 1)))
        cap(s, s1, D10, 8, True, NORMAL, 0.9)
        cap(s, s1, D10, 9, True, YELLOW, 0.8)
        cap(s, s1, D10, 10, True, BROWN, 0.7, review=True)
        cap(s, s1, D10, 11, False)  # rejected photo, no analysis
        cap(s, s1, D10, 12, True, "อื่นๆ", 0.5)  # unknown label: counted nowhere
        cap(s, s1, D10, 13, True, None, None, review=True)  # refusal
        cap(s, s2, D10, 8, True, NORMAL, 0.6)  # no batch for station 2
        cap(s, s1, D11, 8, True, NORMAL, 1.0)
        s.commit()
    return crop, s1, s2


# ---------- rollup ----------

def test_rollup_counts_by_hand(eng):
    crop, s1, s2 = basic_data(eng)
    with Session(eng) as s:
        assert stats.rollup(s) == 3
    r = rows(eng)
    assert [(x.day, x.station_id) for x in r] == [(D10, s1), (D10, s2), (D11, s1)]  # 03-12 has no photos: no row
    a = r[0]
    assert (a.n_captures, a.n_quality_ok, a.n_normal, a.n_yellow, a.n_brown, a.n_review) == (6, 5, 1, 1, 1, 2)
    assert a.mean_confidence == pytest.approx((0.9 + 0.8 + 0.7 + 0.5) / 4)
    assert a.batch_id is not None
    b = r[1]
    assert (b.n_captures, b.n_normal, b.batch_id) == (1, 1, None)
    assert r[2].n_normal == 1 and r[2].mean_confidence == 1.0


def test_rollup_idempotent_and_null_batch_not_duplicated(eng):
    basic_data(eng)
    with Session(eng) as s:
        assert stats.rollup(s) == 3
        before = [(x.id, x.n_captures, x.batch_id) for x in rows(eng)]
        assert stats.rollup(s) == 0  # catch-up only touches days with no rows (and today)
        assert stats.rollup(s, D10) == 2 and stats.rollup(s, D10) == 2  # forced recompute: same rows, no new ones
    r = rows(eng)
    assert [(x.id, x.n_captures, x.batch_id) for x in r] == before
    assert sum(1 for x in r if x.batch_id is None) == 1


def test_rollup_picks_up_new_captures_and_batch_change(eng):
    crop, s1, s2 = basic_data(eng)
    with Session(eng) as s:
        stats.rollup(s)
        cap(s, s2, D10, 15, True, BROWN, 0.4)
        s.add(Batch(station_id=s2, crop_id=crop, planted_on=date(2026, 3, 2)))  # batch appears later
        s.commit()
        stats.rollup(s, D10)
    r = [x for x in rows(eng) if x.station_id == s2]
    assert len(r) == 1 and (r[0].n_captures, r[0].n_brown) == (2, 1) and r[0].batch_id is not None


def test_overlapping_batches_latest_planted_then_highest_id(eng):
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 1)))
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 8)))
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 8)))
        cap(s, s1, D10, 8, True, NORMAL, 0.9)
        s.commit()
        for _ in range(2):
            stats.rollup(s)
        newest = s.scalars(select(Batch.id).order_by(Batch.id.desc())).first()
    assert [x.batch_id for x in rows(eng)] == [newest]


def test_day_before_planting_and_after_harvest_have_no_batch(eng):
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        b = Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 8), harvested_on=D10)
        s.add(b)
        for d in (date(2026, 3, 7), date(2026, 3, 8), D10, D11):
            cap(s, s1, d, 8, True, NORMAL, 0.9)
        s.commit()
        stats.rollup(s)
        bid = b.id
    assert [(x.day.day, x.batch_id) for x in rows(eng)] == [(7, None), (8, bid), (10, bid), (11, None)]


def test_age_days():
    b = Batch(station_id=1, crop_id=1, planted_on=date(2026, 3, 1))
    assert stats.age_days(b, date(2026, 3, 1)) == 0
    assert stats.age_days(b, date(2026, 3, 31)) == 30
    assert stats.age_days(b, date(2026, 2, 27)) == -2  # negative = before planting; rollup never uses that batch


def test_rollup_uses_sql_aggregates(eng):
    basic_data(eng)
    sel = []
    event.listen(eng, "before_cursor_execute", lambda c, cur, st, p, ctx, m: sel.append(st.lower()))
    with Session(eng) as s:
        stats.rollup(s, D10)
    assert sum("count(" in q and "group by" in q for q in sel) >= 1
    assert not any(q.startswith("select captures.id") or q.startswith("select analyses.id,") for q in sel)  # no row dumps


# ---------- public API ----------

PUBLIC = ["/api/public/summary", "/api/public/trends", "/api/public/twin"]


def test_empty_database_gives_valid_empty_structures(client):
    s = client.get("/api/public/summary")
    assert s.status_code == 200
    body = s.json()
    assert body["stations_active"] == 0 and body["captures_today"] == 0 and body["lots_near_harvest"] == []
    assert body["harvest_cycle_days_assumed"] == 45
    assert body["review_pending"] == 0 and body["demo"] is False and body["abnormal_rate_today"] is None
    assert body["updated_at"]
    t = client.get("/api/public/trends").json()
    assert t == {"days": 30, "series": [], "same_age": []}
    w = client.get("/api/public/twin").json()
    assert w["stations"] == [] and w["demo"] is False and w["updated_at"]


def test_headers_and_no_login(client):
    for p in PUBLIC:
        r = client.get(p)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=30"
    assert client.post("/api/public/summary").status_code == 405  # GET only


@pytest.mark.parametrize("q", ["days=0", "days=9999", "days=abc", "days=-1", "station_id=abc", "station_id=0",
                               "station_id=-3", "station_id=2147483648", "station_id=1e3", "days=1.5"])
def test_bad_params_are_422(client, q):
    r = client.get(f"/api/public/trends?{q}")
    assert r.status_code == 422
    assert "traceback" not in r.text.lower()


def test_valid_param_extremes(client):
    assert client.get("/api/public/trends?days=1").status_code == 200
    assert client.get("/api/public/trends?days=365&station_id=2147483647").json()["series"] == []


def fresh_data(eng, today):
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=today - timedelta(days=40)))
        cap(s, s1, today, 8, True, NORMAL, 0.9)
        cap(s, s1, today, 9, True, YELLOW, 0.8)
        cap(s, s1, today, 10, True, BROWN, 0.7, review=True)
        cap(s, s1, today, 11, False)
        s.commit()
        stats.rollup(s)
    return crop, s1, s2


def test_summary_values(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = fresh_data(eng, today)
    with Session(eng) as s:
        s.add(AppSetting(key="demo", value="1"))
        s.commit()
    b = client.get("/api/public/summary").json()
    assert b["demo"] is True
    assert b["stations_active"] == 2
    assert b["captures_today"] == 4
    assert b["abnormal_rate_today"] == pytest.approx(2 / 3)
    assert b["review_pending"] == 1
    assert b["lots_near_harvest"] == [{"station": "สถานี 1", "crop": "กรีนคอส", "age_days": 40, "harvest_in_days": 5}]


def test_trends_values_and_station_filter(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = fresh_data(eng, today)
    with Session(eng) as s:
        cap(s, s2, today - timedelta(days=1), 8, True, NORMAL, 0.9)
        s.commit()
        stats.rollup(s, today - timedelta(days=1))
    t = client.get("/api/public/trends?days=7").json()
    assert t["days"] == 7
    assert [x["station_id"] for x in t["series"]] == [s1, s2]
    p = t["series"][0]["points"][0]
    assert p["day"] == today.isoformat() and p["n"] == 4
    assert p["yellow_rate"] == pytest.approx(1 / 3) and p["brown_rate"] == pytest.approx(1 / 3)
    assert p["review_rate"] == pytest.approx(1 / 4) and p["rejected_rate"] == pytest.approx(1 / 4)
    only = client.get(f"/api/public/trends?days=7&station_id={s2}").json()
    assert [x["station_id"] for x in only["series"]] == [s2]
    assert client.get("/api/public/trends?days=7&station_id=999").json()["series"] == []
    # window: a day outside it is not returned
    assert len(client.get("/api/public/trends?days=1").json()["series"]) == 1


def test_trends_same_age(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        for st, planted, label in ((s1, today - timedelta(days=5), YELLOW), (s2, today - timedelta(days=3), NORMAL)):
            s.add(Batch(station_id=st, crop_id=crop, planted_on=planted))
        cap(s, s1, today, 8, True, YELLOW, 0.9)               # age 5
        cap(s, s1, today - timedelta(days=3), 8, True, NORMAL, 0.9)  # age 2
        cap(s, s2, today - timedelta(days=1), 8, True, NORMAL, 0.9)  # age 2
        s.commit()
        stats.rollup(s)
    sa = client.get("/api/public/trends?days=30").json()["same_age"]
    assert [x["age_day"] for x in sa] == [2, 5]
    age2 = {x["batch_label"]: x["abnormal_rate"] for x in sa[0]["batches"]}
    assert len(age2) == 2 and set(age2.values()) == {0.0}
    assert sa[1]["batches"][0]["abnormal_rate"] == 1.0


def test_same_age_is_capped(client, eng, monkeypatch):
    today = date(2026, 6, 1)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        for i in range(8):  # 8 batches x ages 0..199 on one station: only the 6 newest batches and age < 120 come back
            planted = today - timedelta(days=300 + i)
            s.add(Batch(station_id=s1, crop_id=crop, planted_on=planted, harvested_on=planted + timedelta(days=0)))
        s.flush()
        s.commit()
    with Session(eng) as s:
        batches = s.scalars(select(Batch).order_by(Batch.id)).all()
        for i, b in enumerate(batches):
            s.add_all([DailyStat(day=b.planted_on + timedelta(days=a), station_id=s1, batch_id=b.id, n_captures=1,
                                 n_quality_ok=1, n_normal=1, n_yellow=0, n_brown=0, n_review=0, mean_confidence=1.0)
                       for a in (0, 119, 120, 200)])
        s.commit()
    sa = client.get("/api/public/trends?days=365").json()["same_age"]
    assert [x["age_day"] for x in sa] == [0, 119]
    assert all(len(x["batches"]) <= 6 for x in sa)


def test_trends_weekly_bucketing_over_100_points(client, eng, monkeypatch):
    today = date(2026, 6, 30)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        for i in range(150):
            s.add(DailyStat(day=today - timedelta(days=i), station_id=s1, batch_id=None, n_captures=2, n_quality_ok=2,
                            n_normal=1, n_yellow=1, n_brown=0, n_review=0, mean_confidence=0.9))
        s.commit()
    pts = client.get("/api/public/trends?days=150").json()["series"][0]["points"]
    assert len(pts) <= 23 and sum(p["n"] for p in pts) == 300
    assert all(date.fromisoformat(p["day"]).weekday() == 0 for p in pts)  # weekly buckets start on Monday
    assert all(p["yellow_rate"] == pytest.approx(0.5) for p in pts)
    few = client.get("/api/public/trends?days=90").json()["series"][0]["points"]
    assert len(few) == 90  # <= 100 points stay daily


def test_twin_status_rules(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        s3 = Station(rfid_uid="U3", name="สถานี 3", crop_id=crop, route_order=3)
        s4 = Station(rfid_uid="U4", name="สถานี 4", crop_id=crop, route_order=4)
        home = Station(rfid_uid="HOME", name="บ้าน", kind="home", route_order=0)
        s.add_all([s3, s4, home])
        s.flush()
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=today - timedelta(days=12)))
        cap(s, s1, today - timedelta(days=1), 8, True, NORMAL, 0.9)
        cap(s, s1, today, 8, True, YELLOW, 0.77)                 # latest label wins -> yellow
        cap(s, s2, today, 8, True, BROWN, 0.5, review=True)      # needs review -> review
        cap(s, s3.id, today, 8, True, BROWN, 0.6)
        s.commit()
        ids = (s3.id, s4.id)
    st = {x["name"]: x for x in client.get("/api/public/twin").json()["stations"]}
    assert "บ้าน" not in st
    assert st["สถานี 1"]["status"] == "yellow" and st["สถานี 1"]["age_days"] == 12
    assert st["สถานี 1"]["confidence"] == 0.77 and st["สถานี 1"]["last_seen_at"].startswith(today.isoformat())
    assert st["สถานี 1"]["map_x"] == 1.0 and st["สถานี 1"]["route_order"] == 1 and st["สถานี 1"]["crop"] == "กรีนคอส"
    assert st["สถานี 2"]["status"] == "review" and st["สถานี 2"]["age_days"] is None
    assert st["สถานี 3"]["status"] == "brown"
    assert st["สถานี 4"] == {**st["สถานี 4"], "status": "unknown", "last_seen_at": None, "confidence": None}
    assert list(client.get("/api/public/twin").json()["stations"][0]) == [
        "id", "name", "crop", "map_x", "map_y", "route_order", "status", "age_days", "last_seen_at", "confidence"]


BAD_NAMES = re.compile(r"image|path|password|key|uid|note|secret|hash|username|rfid", re.I)
FORBIDDEN = ("SECRET", "secret", "img.jpg", "4821", "SECRET-UID-123", "secret-admin-789", "Traceback", "sqlalchemy",
             "select ", "from captures", ".py", "\\")


def seed_secrets(eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        st = s.get(Station, s1)
        st.rfid_uid = "SECRET-UID-123"
        s.add(Batch(station_id=s1, crop_id=crop, planted_on=today - timedelta(days=40), notes="SECRET-NOTE-456"))
        c = cap(s, s1, today, 8, True, NORMAL, 0.9, review=True, note="SECRET-NOTE-456")
        c.image_path = "/secret/path/img.jpg"
        s.add(AdminUser(username="secret-admin-789", password_hash="SECRET-HASH-1"))
        s.add(ProviderKey(provider="anthropic", label="SECRET-LABEL", secret_enc=b"SECRET-KEY-BYTES", last4="4821"))
        s.commit()
        stats.rollup(s)
    return s1


def names(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from names(v)
    elif isinstance(o, list):
        for v in o:
            yield from names(v)


def test_nothing_secret_leaks(client, eng, monkeypatch):
    s1 = seed_secrets(eng, monkeypatch)
    for p in PUBLIC + ["/api/public/trends?days=365", f"/api/public/trends?station_id={s1}"]:
        r = client.get(p)
        assert r.status_code == 200
        dump = json.dumps(r.json(), ensure_ascii=False)
        for secret in FORBIDDEN[:6]:
            assert secret not in dump, (p, secret)
        assert not [n for n in names(r.json()) if BAD_NAMES.search(n)], p


def test_nothing_secret_leaks_in_error_bodies(client, eng, monkeypatch):
    seed_secrets(eng, monkeypatch)
    monkeypatch.setattr(routes_public_api, "RATE_LIMIT", 10)
    errors = [
        ("get", "/api/public/trends?days=0", 422), ("get", "/api/public/trends?days=abc", 422),
        ("get", "/api/public/trends?station_id=0", 422), ("get", "/api/public/trends?station_id=99999999999", 422),
        ("post", "/api/public/summary", 405), ("delete", "/api/public/twin", 405), ("get", "/api/public/x", 404),
    ]
    for method, path, code in errors:
        r = getattr(client, method)(path)
        assert r.status_code == code, (path, r.status_code)
        assert "public" not in r.headers.get("cache-control", ""), path
        low = r.text.lower()
        for bad in FORBIDDEN:
            assert bad.lower() not in low, (path, bad)
    monkeypatch.setattr(routes_public_api, "RATE_LIMIT", 1)
    client.get("/api/public/twin")
    r = client.get("/api/public/twin")
    assert r.status_code == 429
    assert "public" not in r.headers.get("cache-control", "")
    for bad in FORBIDDEN:
        assert bad.lower() not in r.text.lower()


def test_rate_limit_429_and_window_reset(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(routes_public_api, "_clock", lambda: now[0])
    monkeypatch.setattr(routes_public_api, "RATE_LIMIT", 3)
    for _ in range(3):
        assert client.get("/api/public/twin").status_code == 200
    r = client.get("/api/public/summary")
    assert r.status_code == 429 and r.json() == {"detail": "too many requests"}
    assert "max-age=30" not in r.headers.get("cache-control", "")
    assert int(r.headers["retry-after"]) >= 1
    assert client.get("/api/public/trends?days=0").status_code == 429  # bad requests are throttled too
    now[0] += 61
    assert client.get("/api/public/twin").status_code == 200


def test_rate_limit_is_per_ip_and_memory_bounded(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(routes_public_api, "_hits", {})
    monkeypatch.setattr(routes_public_api, "_clock", lambda: now[0])
    monkeypatch.setattr(routes_public_api, "MAX_TRACKED_IPS", 50)
    for i in range(200):
        assert routes_public_api.allow(f"10.0.0.{i}")
    assert len(routes_public_api._hits) <= 50
    now[0] += 61
    assert routes_public_api.allow("10.0.0.1")
    assert len(routes_public_api._hits) <= 50


# ---------- wiring ----------

def day_of(engine):
    with Session(engine) as s:
        return s.scalars(select(DailyStat)).all()


def test_capture_triggers_incremental_rollup(env):
    client, engine, calls = env
    r = _post(client, engine)
    assert r.status_code == 200 and r.json()["status"] == "analyzed"
    st = day_of(engine)
    assert len(st) == 1 and (st[0].n_captures, st[0].n_quality_ok, st[0].n_normal) == (1, 1, 1)
    _post(client, engine, angle="side")
    st = day_of(engine)
    assert len(st) == 1 and st[0].n_captures == 2  # same day/station: updated, not duplicated


def test_crashing_rollup_does_not_fail_capture(env, monkeypatch, caplog):
    client, engine, calls = env

    def boom(*a, **k):
        raise RuntimeError("SECRET-DETAIL-xyz")

    monkeypatch.setattr(stats, "rollup", boom)
    r = _post(client, engine)
    assert r.status_code == 200 and r.json()["status"] == "analyzed" and r.json()["label"] == NORMAL
    with Session(engine) as s:
        assert s.scalar(select(func.count(Capture.id))) == 1 and s.scalar(select(func.count(Analysis.id))) == 1
    assert "RuntimeError" in caplog.text and "SECRET-DETAIL-xyz" not in caplog.text


def test_startup_catchup_runs_and_never_blocks_startup(eng, monkeypatch, caplog):
    basic_data(eng)
    monkeypatch.setattr(main, "engine", eng)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main.analyzer, "check_vision_config", lambda: None)
    with TestClient(main.app):
        pass
    assert len(rows(eng)) == 3  # catch-up filled the missing days

    def boom(*a, **k):
        raise RuntimeError("SECRET-DETAIL-xyz")

    monkeypatch.setattr(stats, "rollup", boom)
    with TestClient(main.app) as c:  # must still start
        assert c.get("/api/public/twin").status_code == 200
    assert "RuntimeError" in caplog.text and "SECRET-DETAIL-xyz" not in caplog.text


# ---------- fix round 1 ----------

def test_rate_limiter_threadsafe_and_bounded(monkeypatch):
    monkeypatch.setattr(routes_public_api, "_hits", {})
    monkeypatch.setattr(routes_public_api, "MAX_TRACKED_IPS", 50)
    errors = []

    def hammer(n):
        try:
            for i in range(400):
                routes_public_api.allow(f"10.{n}.{i // 250}.{i % 250}")
        except Exception as e:  # e.g. "dictionary changed size during iteration"
            errors.append(e)

    old = __import__("sys").getswitchinterval()
    __import__("sys").setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        __import__("sys").setswitchinterval(old)
    assert errors == []
    assert len(routes_public_api._hits) <= 50


def test_rate_limit_without_client_shares_one_bucket(monkeypatch):
    monkeypatch.setattr(routes_public_api, "_hits", {})
    monkeypatch.setattr(routes_public_api, "RATE_LIMIT", 1)
    req, resp = SimpleNamespace(client=None), SimpleNamespace(headers={})
    routes_public_api.limit_and_cache(req, resp)
    with pytest.raises(Exception) as exc:
        routes_public_api.limit_and_cache(req, resp)
    assert exc.value.status_code == 429


def test_forwarded_for_header_is_ignored(client, monkeypatch):
    monkeypatch.setattr(routes_public_api, "RATE_LIMIT", 2)
    codes = [client.get("/api/public/twin", headers={"X-Forwarded-For": f"1.2.3.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_analyses_capture_index_exists(eng):
    cols = {tuple(i["column_names"]) for i in inspect(eng).get_indexes("analyses")}
    assert ("capture_id",) in cols


def test_newest_analysis_is_final(eng):
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        c = cap(s, s1, D10, 8, True, NORMAL, 0.9)
        s.add(Analysis(capture_id=c.id, label=YELLOW, confidence=0.4, needs_review=True))
        s.commit()
        stats.rollup(s, D10)
    r = rows(eng)[0]
    assert (r.n_captures, r.n_normal, r.n_yellow, r.n_review) == (1, 0, 1, 1)
    assert r.mean_confidence == pytest.approx(0.4)


def test_duplicate_null_batch_rows_are_cleaned_up(eng):
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        cap(s, s2, D10, 8, True, NORMAL, 0.9)
        for _ in range(3):  # UNIQUE(day, station, batch) does not stop these
            s.add(DailyStat(day=D10, station_id=s2, batch_id=None, n_captures=99, n_quality_ok=99, n_normal=99,
                            n_yellow=0, n_brown=0, n_review=0, mean_confidence=None))
        s.commit()
        assert stats.rollup(s, D10) == 1
    r = rows(eng)
    assert len(r) == 1 and r[0].n_captures == 1


def test_overlap_later_planted_beats_higher_id(eng):
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        later = Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 8))  # lower id, later planting
        earlier = Batch(station_id=s1, crop_id=crop, planted_on=date(2026, 3, 1))
        s.add(later)
        s.flush()
        s.add(earlier)
        cap(s, s1, D10, 8, True, NORMAL, 0.9)
        s.commit()
        assert later.id < earlier.id
        stats.rollup(s)
        assert rows(eng)[0].batch_id == later.id


def test_days_are_utc_boundaries(eng):
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        for ts in (datetime(2026, 3, 10, 23, 59, 59), datetime(2026, 3, 11, 0, 0, 0)):
            c = Capture(station_id=s1, taken_at=ts, image_path="/x", quality_ok=True)
            s.add(c)
        s.commit()
        stats.rollup(s)
    assert [(x.day, x.n_captures) for x in rows(eng)] == [(D10, 1), (D11, 1)]


def test_catchup_window_limits_old_days(eng, monkeypatch):
    today = date(2026, 6, 1)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, _ = seed_basic(eng)
    old, recent = today - timedelta(days=stats.CATCHUP_DAYS + 5), today - timedelta(days=stats.CATCHUP_DAYS - 5)
    with Session(eng) as s:
        cap(s, s1, old, 8, True, NORMAL, 0.9)
        cap(s, s1, recent, 8, True, NORMAL, 0.9)
        s.commit()
        assert stats.rollup(s) == 1
        assert stats.rollup(s, old) == 1  # an explicit day is still honoured
    assert [x.day for x in rows(eng)] == [old, recent]


def test_capture_survives_post_commit_refresh_failure(env):
    client, engine, calls = env

    def explode(target, context, attrs):
        raise RuntimeError("refresh after commit")

    event.listen(Station, "refresh", explode)
    event.listen(Capture, "refresh", explode)
    try:
        r = _post(client, engine)
    finally:
        event.remove(Station, "refresh", explode)
        event.remove(Capture, "refresh", explode)
    assert r.status_code == 200 and r.json()["status"] == "analyzed"
    assert len(day_of(engine)) == 1


def test_trends_hide_inactive_and_home_stations(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        off = Station(rfid_uid="U9", name="ปิด", crop_id=crop, active=False)
        home = Station(rfid_uid="H", name="บ้าน", kind="home")
        s.add_all([off, home])
        s.flush()
        off_id, home_id = off.id, home.id
        for sid in (s1, off_id, home_id):
            cap(s, sid, today, 8, True, NORMAL, 0.9)
        s.add(Batch(station_id=off_id, crop_id=crop, planted_on=today - timedelta(days=3)))
        s.commit()
        stats.rollup(s, today)
    t = client.get("/api/public/trends?days=7").json()
    assert [x["station_id"] for x in t["series"]] == [s1]
    assert client.get(f"/api/public/trends?station_id={off_id}").json()["series"] == []
    assert client.get(f"/api/public/trends?station_id={off_id}").json()["same_age"] == []


def test_lots_near_harvest_window_and_visibility(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, s2 = seed_basic(eng)
    with Session(eng) as s:
        s3 = Station(rfid_uid="U3", name="สถานี 3", crop_id=crop)
        s4 = Station(rfid_uid="U4", name="สถานี 4", crop_id=crop, active=False)
        home = Station(rfid_uid="H", name="บ้าน", kind="home")
        s.add_all([s3, s4, home])
        s.flush()
        ages = {s1: 38, s2: 45 + 14, s3.id: 45 + 15, s4.id: 38, home.id: 38}  # 7 left, 14 overdue, 15 overdue, hidden, hidden
        for sid, age in ages.items():
            s.add(Batch(station_id=sid, crop_id=crop, planted_on=today - timedelta(days=age)))
        s.add(Batch(station_id=s3.id, crop_id=crop, planted_on=today - timedelta(days=20)))  # 25 left: not near
        s.commit()
    b = client.get("/api/public/summary").json()
    assert [(x["station"], x["harvest_in_days"]) for x in b["lots_near_harvest"]] == [("สถานี 2", -14), ("สถานี 1", 7)]
    assert b["harvest_cycle_days_assumed"] == 45


def test_twin_last_seen_is_utc_z_without_microseconds(client, eng, monkeypatch):
    today = date(2026, 3, 20)
    monkeypatch.setattr(stats, "today", lambda: today)
    crop, s1, _ = seed_basic(eng)
    with Session(eng) as s:
        c = cap(s, s1, today, 8, True, NORMAL, 0.9)
        c.taken_at = datetime(2026, 3, 20, 8, 30, 15, 123456)
        s.commit()
    st = client.get("/api/public/twin").json()["stations"][0]
    assert st["last_seen_at"] == "2026-03-20T08:30:15Z"
