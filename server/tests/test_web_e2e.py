"""End-to-End Tests for NEXXA:

100-Day Season Demo Seed, Full Inspection Round Lifecycle, Performance,
Public API Privacy, and DEMO Banner Rendering.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta, timezone
import os
import re
import time as time_mod

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.db
from app import analyzer, main, robot, seed_demo, stats, storage
from app.analyzer import AnalysisResult
from app.create_admin import create_or_update
from app.db import get_session, init_db
from app.models import (
    AdminUser,
    Analysis,
    AppSetting,
    AuditLog,
    Batch,
    Capture,
    Crop,
    DailyStat,
    DeviceKey,
    Round,
    Station,
)
from app.web import audit, deps, routes_auth, routes_public_api, secretbox, security
from app.web.security import LoginThrottle

SESSION_SECRET = "test-session-secret-01234567890123456789"
ADMIN_PASSWORD = "super-secret-password-123"


def _png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _sharp_image() -> bytes:
    y, x = np.indices((128, 128))
    gray = np.where(((x // 8) + (y // 8)) % 2 == 0, 50, 200).astype(np.uint8)
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    return _png_bytes(bgr)


def _blurry_image() -> bytes:
    y, x = np.indices((128, 128))
    gray = np.where(((x // 8) + (y // 8)) % 2 == 0, 50, 200).astype(np.uint8)
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    blurred = cv2.GaussianBlur(bgr, (0, 0), sigmaX=15)
    return _png_bytes(blurred)


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, f"No csrf_token found in html: {html[:300]}"
    return m.group(1)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(eng)
    monkeypatch.setattr(app.db, "engine", eng)
    monkeypatch.setattr(storage, "settings", dataclasses.replace(storage.settings, image_dir=tmp_path))
    monkeypatch.setenv("NEXXA_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.delenv("NEXXA_SECRET_KEY", raising=False)
    monkeypatch.setattr(routes_public_api, "_hits", {})
    monkeypatch.setattr(routes_auth, "throttle", LoginThrottle())
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine):
    def override():
        with Session(engine) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    c = TestClient(main.app, follow_redirects=False)
    yield c
    main.app.dependency_overrides.clear()


# ==============================================================================
# (a) Repeatability of run(days=100, seed=1) on empty DB
# ==============================================================================
def test_seed_demo_run_repeatability(engine):
    """Seed demo data on empty DB; re-running with same seed produces identical results."""
    with Session(engine) as s:
        res1 = seed_demo.run(s, days=100, seed=1)
        assert res1["days"] == 100
        assert res1["rounds"] == 100
        assert res1["captures"] >= 1200
        assert res1["analyses"] == 1200
        assert res1["daily_stats"] == 300
        assert res1["batches"] >= 3

        demo_flag = s.scalar(select(AppSetting.value).where(AppSetting.key == "demo"))
        assert demo_flag == "1"

        # Count records
        n_cap1 = s.scalar(select(func.count(Capture.id)))
        n_ana1 = s.scalar(select(func.count(Analysis.id)))
        n_rnd1 = s.scalar(select(func.count(Round.id)))
        n_bat1 = s.scalar(select(func.count(Batch.id)))
        n_dst1 = s.scalar(select(func.count(DailyStat.id)))

        # Snapshot of capture paths and analysis labels
        cap_paths1 = s.scalars(select(Capture.image_path).order_by(Capture.id)).all()
        ana_labels1 = s.scalars(select(Analysis.label).order_by(Analysis.id)).all()

        # Re-run with same seed
        res2 = seed_demo.run(s, days=100, seed=1)
        assert res1 == res2

        n_cap2 = s.scalar(select(func.count(Capture.id)))
        n_ana2 = s.scalar(select(func.count(Analysis.id)))
        n_rnd2 = s.scalar(select(func.count(Round.id)))
        n_bat2 = s.scalar(select(func.count(Batch.id)))
        n_dst2 = s.scalar(select(func.count(DailyStat.id)))

        assert (n_cap1, n_ana1, n_rnd1, n_bat1, n_dst1) == (n_cap2, n_ana2, n_rnd2, n_bat2, n_dst2)

        cap_paths2 = s.scalars(select(Capture.image_path).order_by(Capture.id)).all()
        ana_labels2 = s.scalars(select(Analysis.label).order_by(Analysis.id)).all()
        assert cap_paths1 == cap_paths2
        assert ana_labels1 == ana_labels2


# ==============================================================================
# (b) Safety check with real captures & force=True
# ==============================================================================
def test_seed_demo_safety_with_real_captures(engine):
    """If real captures exist, run() raises ValueError unless force=True."""
    with Session(engine) as s:
        crop = Crop(name="กรีนคอส", labels=["ปกติ"], prompt="p", notes="n")
        s.add(crop)
        s.flush()
        st = Station(rfid_uid="REAL-RFID-1", name="สถานีจริง 1", crop_id=crop.id)
        s.add(st)
        s.flush()
        real_cap = Capture(
            station_id=st.id,
            image_path="/data/storage/real_capture.jpg",
            quality_ok=True,
            taken_at=datetime.now(timezone.utc),
        )
        s.add(real_cap)
        s.commit()

        # Calling run without force must raise ValueError and modify no data
        with pytest.raises(ValueError, match="database contains real captures; pass force=True to proceed"):
            seed_demo.run(s, days=10, seed=1, force=False)

        # Verify database was not modified
        assert s.scalar(select(func.count(Capture.id))) == 1
        assert s.scalar(select(Capture.image_path)) == "/data/storage/real_capture.jpg"
        assert s.scalar(select(AppSetting.value).where(AppSetting.key == "demo")) is None

        # Calling run with force=True must succeed without deleting the real capture
        res = seed_demo.run(s, days=10, seed=1, force=True)
        assert res["days"] == 10
        # The real capture must still exist
        real_still_there = s.scalar(select(Capture).where(Capture.image_path == "/data/storage/real_capture.jpg"))
        assert real_still_there is not None
        assert s.scalar(select(AppSetting.value).where(AppSetting.key == "demo")) == "1"


# ==============================================================================
# (c) seed_demo.clear deletes only demo data and resets demo flag
# ==============================================================================
def test_seed_demo_clear(engine):
    """clear() removes all demo data and resets demo flag, preserving non-demo data."""
    with Session(engine) as s:
        # Create non-demo station and real capture
        crop = Crop(name="กรีนคอส", labels=["ปกติ"], prompt="p", notes="n")
        s.add(crop)
        s.flush()
        st = Station(rfid_uid="REAL-RFID-99", name="สถานีจริง 99", crop_id=crop.id, description="real station")
        s.add(st)
        s.flush()
        real_cap = Capture(
            station_id=st.id,
            image_path="/data/storage/real_image.jpg",
            quality_ok=True,
            taken_at=datetime.now(timezone.utc),
        )
        s.add(real_cap)
        s.commit()

        # Seed demo data with force=True
        seed_demo.run(s, days=10, seed=1, force=True)
        assert s.scalar(select(func.count(Capture.id))) > 1
        assert s.scalar(select(AppSetting.value).where(AppSetting.key == "demo")) == "1"

        # Now clear demo data
        cleared = seed_demo.clear(s)
        assert cleared["captures"] > 0
        assert cleared["analyses"] > 0
        assert cleared["rounds"] > 0

        # Only real capture remains
        remaining_captures = s.scalars(select(Capture)).all()
        assert len(remaining_captures) == 1
        assert remaining_captures[0].image_path == "/data/storage/real_image.jpg"

        # Non-demo station is preserved
        assert s.get(Station, st.id) is not None

        # Demo flag reset to "0"
        assert s.scalar(select(AppSetting.value).where(AppSetting.key == "demo")) == "0"


# ==============================================================================
# (d) Full e2e "Inspection Round" flow starting from capturing 4 sides per station
# ==============================================================================
def test_full_inspection_round_e2e(engine, client, monkeypatch):
    """Full lifecycle from admin login, setup, robot round, 4-angle captures,

    retakes, analysis, and downstream reflection across public and admin interfaces.
    """
    today = stats.today()
    sharp_png = _sharp_image()
    blurry_png = _blurry_image()

    # Step 1: Admin user creation & login
    with Session(engine) as s:
        create_or_update(s, "admin", ADMIN_PASSWORD)

    login_page = client.get("/login")
    assert login_page.status_code == 200
    csrf = _extract_csrf(login_page.text)
    login_resp = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "csrf_token": csrf},
    )
    assert login_resp.status_code == 303

    # Step 2: Create Crop, 3 Stations (RFID-S1, RFID-S2, RFID-S3), Home station, Batches
    with Session(engine) as s:
        crop = Crop(
            name="กรีนคอส",
            labels=["ปกติ", "ใบเหลือง", "จุดน้ำตาล"],
            prompt="ดูลักษณะใบผักสลัด",
            notes="ขอบใบไหม้ = จุดน้ำตาล",
        )
        s.add(crop)
        s.flush()

        s1 = Station(rfid_uid="RFID-S1", name="สถานี 1", route_order=1, map_x=1.0, map_y=0.0, crop_id=crop.id)
        s2 = Station(rfid_uid="RFID-S2", name="สถานี 2", route_order=2, map_x=2.0, map_y=0.0, crop_id=crop.id)
        s3 = Station(rfid_uid="RFID-S3", name="สถานี 3", route_order=3, map_x=3.0, map_y=0.0, crop_id=crop.id)
        home = Station(rfid_uid="RFID-HOME", name="จุดเริ่มต้น", kind="home", route_order=0, map_x=0.0, map_y=0.0)
        s.add_all([s1, s2, s3, home])
        s.flush()

        # Batches for each station:
        # Station 1: young (10 days old)
        # Station 2: mid (25 days old)
        # Station 3: near harvest (40 days old, left 5 days <= NEAR_HARVEST_DAYS)
        b1 = Batch(station_id=s1.id, crop_id=crop.id, planted_on=today - timedelta(days=10))
        b2 = Batch(station_id=s2.id, crop_id=crop.id, planted_on=today - timedelta(days=25))
        b3 = Batch(station_id=s3.id, crop_id=crop.id, planted_on=today - timedelta(days=40))
        s.add_all([b1, b2, b3])

        # Step 3: Generate Device Key, store hash in DB and record in audit log
        raw_device_key = security.new_device_key()
        dev_row = DeviceKey(
            label="nexxa-robot-unit-1",
            key_hash=security.hash_device_key(raw_device_key),
            last4=secretbox.last4(raw_device_key),
        )
        s.add(dev_row)
        s.flush()
        audit.record(s, "admin", "device.create", f"device#{dev_row.id}", "nexxa-robot-unit-1")
        s.commit()

        s1_id, s2_id, s3_id = s1.id, s2.id, s3.id

    # Step 4: Controlled analyzer simulation
    analysis_calls = []

    def mock_analyze(image_bytes: bytes, c_crop: Crop, client=None):
        analysis_calls.append(c_crop.name)
        if getattr(mock_analyze, "force_review", False):
            return AnalysisResult("จุดน้ำตาล", 0.52, "ความมั่นใจต่ำ ต้องการตรวจทาน", True, "mock-model")
        return AnalysisResult("ปกติ", 0.96, "สมบูรณ์ดี", False, "mock-model")

    monkeypatch.setattr(analyzer, "analyze_leaf", mock_analyze)

    # Step 5: Start robot round via POST /rounds with X-Device-Key
    dev_headers = {"X-Device-Key": raw_device_key}
    round_resp = client.post("/rounds", headers=dev_headers)
    assert round_resp.status_code == 200
    round_id = round_resp.json()["round_id"]
    assert isinstance(round_id, int)

    # Check robot monitor updated
    robot_stat = client.get("/api/public/robot").json()
    assert robot_stat["round_id"] == round_id
    assert robot_stat["state"] == "moving"

    # Step 6: Test invalid RFID -> 404
    r_bad_rfid = client.post(
        "/captures",
        data={"rfid_uid": "RFID-NONEXISTENT", "round_id": round_id, "camera_angle": "front"},
        files={"image": ("shot.png", sharp_png, "image/png")},
        headers=dev_headers,
    )
    assert r_bad_rfid.status_code == 404

    # Step 7: Test home station RFID -> 422
    r_home = client.post(
        "/captures",
        data={"rfid_uid": "RFID-HOME", "round_id": round_id, "camera_angle": "front"},
        files={"image": ("shot.png", sharp_png, "image/png")},
        headers=dev_headers,
    )
    assert r_home.status_code == 422

    # Step 8: Station 1 - 4 angles
    # Angle "front": blurry -> retake -> sharp -> analyzed
    r_blur = client.post(
        "/captures",
        data={"rfid_uid": "RFID-S1", "round_id": round_id, "camera_angle": "front"},
        files={"image": ("shot_blurry.png", blurry_png, "image/png")},
        headers=dev_headers,
    )
    assert r_blur.status_code == 200
    b_json = r_blur.json()
    assert b_json["status"] == "retake"
    assert b_json["attempt"] == 1

    r_retake = client.post(
        "/captures",
        data={"rfid_uid": "RFID-S1", "round_id": round_id, "camera_angle": "front"},
        files={"image": ("shot_sharp.png", sharp_png, "image/png")},
        headers=dev_headers,
    )
    assert r_retake.status_code == 200
    r_json = r_retake.json()
    assert r_json["status"] == "analyzed"
    assert r_json["attempt"] == 2

    # Try uploading front angle again -> 409 already analyzed
    r_dup = client.post(
        "/captures",
        data={"rfid_uid": "RFID-S1", "round_id": round_id, "camera_angle": "front"},
        files={"image": ("shot_sharp.png", sharp_png, "image/png")},
        headers=dev_headers,
    )
    assert r_dup.status_code == 409

    # Angle "right": simulate low confidence -> status="needs_review"
    mock_analyze.force_review = True
    r_review = client.post(
        "/captures",
        data={"rfid_uid": "RFID-S1", "round_id": round_id, "camera_angle": "right"},
        files={"image": ("shot_right.png", sharp_png, "image/png")},
        headers=dev_headers,
    )
    assert r_review.status_code == 200
    assert r_review.json()["status"] == "needs_review"
    mock_analyze.force_review = False

    # Angles "back" and "left" for Station 1
    for angle in ["back", "left"]:
        r_angle = client.post(
            "/captures",
            data={"rfid_uid": "RFID-S1", "round_id": round_id, "camera_angle": angle},
            files={"image": (f"shot_{angle}.png", sharp_png, "image/png")},
            headers=dev_headers,
        )
        assert r_angle.status_code == 200
        assert r_angle.json()["status"] == "analyzed"

    # Step 9: Stations 2 and 3 - 4 angles each
    for rfid in ["RFID-S2", "RFID-S3"]:
        for angle in ["front", "right", "back", "left"]:
            r_ang = client.post(
                "/captures",
                data={"rfid_uid": rfid, "round_id": round_id, "camera_angle": angle},
                files={"image": (f"shot_{rfid}_{angle}.png", sharp_png, "image/png")},
                headers=dev_headers,
            )
            assert r_ang.status_code == 200
            assert r_ang.json()["status"] == "analyzed"

    # ==========================================================================
    # DOWNSTREAM VERIFICATIONS
    # ==========================================================================
    with Session(engine) as s:
        # DB captures: Station 1 had 5 captures (1 blurry + 1 sharp retake + 3 angles)
        # Station 2 had 4 captures, Station 3 had 4 captures = total 13 captures.
        total_caps = s.scalars(select(Capture)).all()
        assert len(total_caps) == 13

        # Analyses: 4 for station 1, 4 for station 2, 4 for station 3 = 12 analyses
        total_analyses = s.scalars(select(Analysis)).all()
        assert len(total_analyses) == 12

        # Verify linkage
        for ana in total_analyses:
            cap = s.get(Capture, ana.capture_id)
            assert cap is not None
            assert cap.quality_ok is True

        # Image stored in storage
        for cap in total_caps:
            assert os.path.isfile(cap.image_path)

        # stats.rollup automatically populated daily_stats
        ds1 = s.scalar(select(DailyStat).where(DailyStat.station_id == s1_id, DailyStat.day == today))
        assert ds1 is not None
        assert ds1.n_captures == 5
        assert ds1.n_quality_ok == 4
        assert ds1.n_review == 1

        ds2 = s.scalar(select(DailyStat).where(DailyStat.station_id == s2_id, DailyStat.day == today))
        assert ds2 is not None
        assert ds2.n_captures == 4
        assert ds2.n_quality_ok == 4
        assert ds2.n_normal == 4

        ds3 = s.scalar(select(DailyStat).where(DailyStat.station_id == s3_id, DailyStat.day == today))
        assert ds3 is not None
        assert ds3.n_captures == 4
        assert ds3.n_quality_ok == 4
        assert ds3.n_normal == 4

    # Anonymous public API client (no login required)
    anon_client = TestClient(main.app, follow_redirects=False)

    # 1. GET /api/public/summary
    summary_resp = anon_client.get("/api/public/summary")
    assert summary_resp.status_code == 200
    sum_data = summary_resp.json()
    assert sum_data["captures_today"] == 13
    assert sum_data["stations_active"] == 3
    assert sum_data["review_pending"] == 1
    # Check lot near harvest (Station 3 is 40 days old, 5 days to harvest)
    harvest_lots = [lot for lot in sum_data["lots_near_harvest"] if lot["station"] == "สถานี 3"]
    assert len(harvest_lots) == 1
    assert harvest_lots[0]["age_days"] == 40
    assert harvest_lots[0]["harvest_in_days"] == 5

    # 2. GET /api/public/trends
    trends_resp = anon_client.get("/api/public/trends")
    assert trends_resp.status_code == 200
    tr_data = trends_resp.json()
    assert len(tr_data["series"]) == 3

    # 3. GET /api/public/twin
    twin_resp = anon_client.get("/api/public/twin")
    assert twin_resp.status_code == 200
    twin_data = twin_resp.json()
    assert len(twin_data["stations"]) == 3

    # 4. GET /api/public/robot
    robot_resp = anon_client.get("/api/public/robot")
    assert robot_resp.status_code == 200
    r_data = robot_resp.json()
    assert r_data["round_id"] == round_id
    assert r_data["station_id"] == s3_id
    assert r_data["camera_angle"] == "left"

    # 5. Public HTML pages respond 200
    p1 = anon_client.get("/")
    assert p1.status_code == 200
    p2 = anon_client.get("/trends")
    assert p2.status_code == 200

    # 6. Admin can view overview and audit log
    adm_overview = client.get("/admin")
    assert adm_overview.status_code == 200
    adm_audit = client.get("/admin/audit")
    assert adm_audit.status_code == 200

    with Session(engine) as s:
        audit_records = s.scalars(select(AuditLog)).all()
        assert any(a.action == "device.create" for a in audit_records)

    # 7. Privacy: NO secrets, NO RFID UIDs, NO real image paths leaked in public APIs
    for endpoint, payload in [
        ("/api/public/summary", summary_resp.text),
        ("/api/public/trends", trends_resp.text),
        ("/api/public/twin", twin_resp.text),
        ("/api/public/robot", robot_resp.text),
    ]:
        assert raw_device_key not in payload, f"Secret device key leaked in {endpoint}"
        assert "RFID-S1" not in payload, f"RFID UID leaked in {endpoint}"
        assert ".png" not in payload and ".jpg" not in payload, f"Image path leaked in {endpoint}"


# ==============================================================================
# (e) Performance and payload size benchmarks
# ==============================================================================
def test_trends_performance_and_payload_size(engine, client):
    """After 100-day seed, /api/public/trends?days=100 payload <= 100 KB and latency < 1.0s."""
    with Session(engine) as s:
        seed_demo.run(s, days=100, seed=1)

    t0 = time_mod.perf_counter()
    resp_100 = client.get("/api/public/trends?days=100")
    elapsed = time_mod.perf_counter() - t0

    assert resp_100.status_code == 200
    size_100 = len(resp_100.content)

    # Verifications
    assert size_100 <= 100 * 1024, f"Payload size {size_100} exceeds 100 KB"
    assert elapsed < 1.0, f"Response time {elapsed:.3f}s exceeds 1.0s"

    # Compare response sizes for days=30, 100, 365
    resp_30 = client.get("/api/public/trends?days=30")
    resp_365 = client.get("/api/public/trends?days=365")
    assert resp_30.status_code == 200
    assert resp_365.status_code == 200

    size_30 = len(resp_30.content)
    size_365 = len(resp_365.content)

    # 365 days is bucketed weekly (WEEKLY_OVER = 100), so size does not grow unboundedly
    assert size_365 <= 100 * 1024
    # Because of weekly bucketing, 365 days points per station (52) is fewer than 100 days (100)
    assert size_365 <= size_100 * 1.5


# ==============================================================================
# (f) Dashboard and trends HTML render DEMO banner when demo=="1"
# ==============================================================================
def test_demo_banner_rendering(engine, client):
    """DEMO banner is visible when demo=='1', and has 'hidden' attribute when demo=='0'."""
    with Session(engine) as s:
        seed_demo.run(s, days=5, seed=1)

    # 1. When demo=="1": banner is visible (not hidden)
    dash_1 = client.get("/")
    assert dash_1.status_code == 200
    assert 'id="demo-banner"' in dash_1.text
    assert 'id="demo-banner" hidden' not in dash_1.text
    assert "ข้อมูลตัวอย่าง (DEMO)" in dash_1.text

    trends_1 = client.get("/trends")
    assert trends_1.status_code == 200
    assert 'id="demo-banner"' in trends_1.text
    assert 'id="demo-banner" hidden' not in trends_1.text
    assert "ข้อมูลตัวอย่าง (DEMO)" in trends_1.text

    # 2. When cleared (demo=="0"): banner has hidden attribute
    with Session(engine) as s:
        seed_demo.clear(s)

    dash_0 = client.get("/")
    assert dash_0.status_code == 200
    assert 'id="demo-banner" hidden' in dash_0.text

    trends_0 = client.get("/trends")
    assert trends_0.status_code == 200
    assert 'id="demo-banner" hidden' in trends_0.text
