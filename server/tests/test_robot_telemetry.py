import dataclasses
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import analyzer, main, storage
from app.analyzer import AnalysisResult
from app.create_admin import create_or_update
from app.db import get_session, init_db
from app.models import AppSetting, AuditLog, Capture, Station
from app.robot import robot
from app.seed import seed
from tests.test_web_auth import _client, _csrf, _login


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _sharp() -> np.ndarray:
    y, x = np.indices((128, 128))
    gray = np.where(((x // 8) + (y // 8)) % 2 == 0, 50, 200).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


KEY = "robot-test-device-key"
GOOD = _png(_sharp())
BLURRY = _png(cv2.GaussianBlur(_sharp(), (0, 0), sigmaX=15))
SESSION_SECRET = "robot-session-secret-0123456789"
ADMIN_PASSWORD = "robot-admin-password"


@pytest.fixture
def env(tmp_path, monkeypatch):
    robot.reset()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as s:
        seed(s)
        create_or_update(s, "admin", ADMIN_PASSWORD)

    def override():
        with Session(engine) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    monkeypatch.setattr(storage, "settings", dataclasses.replace(storage.settings, image_dir=tmp_path))
    monkeypatch.setenv("NEXXA_DEVICE_KEY", KEY)
    monkeypatch.setenv("NEXXA_SESSION_SECRET", SESSION_SECRET)

    def fake(image_bytes, crop, client=None):
        return AnalysisResult("ปกติ", 0.95, "ok", False, "test-model")

    monkeypatch.setattr(analyzer, "analyze_leaf", fake)

    client = TestClient(main.app)
    yield client, engine
    main.app.dependency_overrides.clear()
    robot.reset()


def _uid(engine, kind="station"):
    with Session(engine) as s:
        return s.scalars(select(Station).where(Station.kind == kind).order_by(Station.id)).first().rfid_uid


# --- Telemetry Endpoint Tests -------------------------------------------------

def test_telemetry_requires_device_key(env):
    client, _ = env
    payload = {"state": "moving", "battery_pct": 90}

    # Missing device key
    r = client.post("/api/robot/telemetry", json=payload)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid device key"

    # Wrong device key
    r = client.post("/api/robot/telemetry", json=payload, headers={"X-Device-Key": "wrong-key"})
    assert r.status_code == 401

    # Valid device key
    r = client.post("/api/robot/telemetry", json=payload, headers={"X-Device-Key": KEY})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["updated_at"], float)
    assert data["updated_at"] > 0


def test_telemetry_payload_validation(env):
    client, _ = env
    auth = {"X-Device-Key": KEY}

    # Invalid state
    r = client.post("/api/robot/telemetry", json={"state": "jumping"}, headers=auth)
    assert r.status_code == 422

    # Invalid step_index
    r = client.post("/api/robot/telemetry", json={"step_index": 0}, headers=auth)
    assert r.status_code == 422
    r = client.post("/api/robot/telemetry", json={"step_index": 6}, headers=auth)
    assert r.status_code == 422

    # Invalid camera angle
    r = client.post("/api/robot/telemetry", json={"camera_angle": "diagonal"}, headers=auth)
    assert r.status_code == 422

    # Invalid battery percentage
    r = client.post("/api/robot/telemetry", json={"battery_pct": 105}, headers=auth)
    assert r.status_code == 422
    r = client.post("/api/robot/telemetry", json={"battery_pct": -5}, headers=auth)
    assert r.status_code == 422

    # Valid comprehensive telemetry
    valid_payload = {
        "state": "inspecting",
        "round_id": 42,
        "camera_angle": "right",
        "step": "กำลังถ่ายภาพ (มุมขวา 2/4)",
        "step_index": 3,
        "stream_url": "http://192.168.1.150:81/stream",
        "battery_pct": 82,
        "wifi_rssi": -65,
    }
    r = client.post("/api/robot/telemetry", json=valid_payload, headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Verify state in robot monitor
    status = robot.get_robot_status()
    assert status["online"] is True
    assert status["state"] == "inspecting"
    assert status["round_id"] == 42
    assert status["camera_angle"] == "right"
    assert status["camera_angle_label"] == "ขวา"
    assert status["step"] == "กำลังถ่ายภาพ (มุมขวา 2/4)"
    assert status["step_index"] == 3
    assert status["stream_url"] == "http://192.168.1.150:81/stream"
    assert status["battery_pct"] == 82
    assert status["wifi_rssi"] == -65


def test_telemetry_station_resolution(env):
    client, engine = env
    auth = {"X-Device-Key": KEY}

    with Session(engine) as s:
        station = s.scalars(select(Station).where(Station.kind == "station")).first()
        rfid = station.rfid_uid
        sid = station.id
        sname = station.name

    # Resolution by rfid_uid
    r = client.post("/api/robot/telemetry", json={"rfid_uid": rfid}, headers=auth)
    assert r.status_code == 200
    status = robot.get_robot_status()
    assert status["station_id"] == sid
    assert status["station_name"] == sname

    # Resolution by station_id directly
    r = client.post("/api/robot/telemetry", json={"station_id": sid}, headers=auth)
    assert r.status_code == 200
    status = robot.get_robot_status()
    assert status["station_id"] == sid
    assert status["station_name"] == sname


# --- Public Robot Endpoint Tests ----------------------------------------------

def test_public_robot_status_offline_timeout(env, monkeypatch):
    client, _ = env

    # Initially before any telemetry is received
    r = client.get("/api/public/robot")
    assert r.status_code == 200
    data = r.json()
    assert data["online"] is False
    assert data["state"] == "offline"
    assert data["state_label"] == "ออฟไลน์"

    # Send telemetry
    now = 1000.0
    monkeypatch.setattr(time, "time", lambda: now)
    client.post("/api/robot/telemetry", json={"state": "moving", "step_index": 2}, headers={"X-Device-Key": KEY})

    r = client.get("/api/public/robot")
    assert r.status_code == 200
    assert r.json()["online"] is True
    assert r.json()["state"] == "moving"
    assert r.json()["state_label"] == "กำลังเคลื่อนที่"

    # 40 seconds later: still online (<= 45s)
    monkeypatch.setattr(time, "time", lambda: now + 40.0)
    r = client.get("/api/public/robot")
    assert r.json()["online"] is True

    # 46 seconds later: timed out (> 45s)
    monkeypatch.setattr(time, "time", lambda: now + 46.0)
    r = client.get("/api/public/robot")
    assert r.json()["online"] is False
    assert r.json()["state"] == "offline"
    assert r.json()["state_label"] == "ออฟไลน์"


def test_public_robot_status_stream_url_from_app_setting(env):
    client, engine = env

    with Session(engine) as s:
        s.add(AppSetting(key="robot_stream_url", value="http://192.168.1.200:81/stream"))
        s.commit()

    r = client.get("/api/public/robot")
    assert r.status_code == 200
    assert r.json()["stream_url"] == "http://192.168.1.200:81/stream"


# --- Automatic Status Sync Tests ----------------------------------------------

def test_automatic_sync_on_create_round(env):
    client, _ = env
    auth = {"X-Device-Key": KEY}

    r = client.post("/rounds", headers=auth)
    assert r.status_code == 200
    rid = r.json()["round_id"]

    status = robot.get_robot_status()
    assert status["online"] is True
    assert status["round_id"] == rid
    assert status["state"] == "moving"
    assert status["step_index"] == 2
    assert "เริ่มรอบตรวจใหม่" in status["step"]


def test_automatic_sync_on_create_capture_analyzed(env):
    client, engine = env
    auth = {"X-Device-Key": KEY}

    rid = client.post("/rounds", headers=auth).json()["round_id"]
    uid = _uid(engine)

    r = client.post(
        "/captures",
        headers=auth,
        files={"image": ("leaf.png", GOOD, "image/png")},
        data={"rfid_uid": uid, "round_id": rid, "camera_angle": "right"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "analyzed"

    status = robot.get_robot_status()
    assert status["online"] is True
    assert status["round_id"] == rid
    assert status["state"] == "analyzing"
    assert status["step_index"] == 4
    assert status["camera_angle"] == "right"
    assert status["camera_angle_label"] == "ขวา"
    assert "มุมขวา" in status["step"]
    assert "วิเคราะห์ผล" in status["step"]


def test_automatic_sync_on_create_capture_retake(env):
    client, engine = env
    auth = {"X-Device-Key": KEY}

    rid = client.post("/rounds", headers=auth).json()["round_id"]
    uid = _uid(engine)

    r = client.post(
        "/captures",
        headers=auth,
        files={"image": ("leaf.png", BLURRY, "image/png")},
        data={"rfid_uid": uid, "round_id": rid, "camera_angle": "front"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "retake"

    status = robot.get_robot_status()
    assert status["online"] is True
    assert status["round_id"] == rid
    assert status["state"] == "inspecting"
    assert status["step_index"] == 3
    assert status["camera_angle"] == "front"
    assert status["camera_angle_label"] == "หน้า"
    assert "ถ่ายซ้ำ" in status["step"]


# --- Admin Stream URL Settings Tests ------------------------------------------

def test_admin_settings_unauthenticated_redirects(env):
    client, _ = env
    r = client.get("/admin/settings", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")

    r = client.post("/admin/settings", data={"robot_stream_url": "http://test"}, follow_redirects=False)
    assert r.status_code == 303


def test_admin_settings_crud(env):
    client, engine = env
    # Login as admin
    admin_client = _client()
    _login(admin_client, username="admin", password=ADMIN_PASSWORD)

    # GET settings page
    r = admin_client.get("/admin/settings")
    assert r.status_code == 200
    assert "ตั้งค่าระบบ" in r.text
    csrf = _csrf(r.text)

    # POST invalid URL (not http/https)
    r = admin_client.post(
        "/admin/settings",
        data={"csrf_token": csrf, "robot_stream_url": "ftp://bad-url"},
    )
    assert r.status_code == 422
    assert "ต้องขึ้นต้นด้วย http:// หรือ https://" in r.text

    # POST valid URL
    r = admin_client.post(
        "/admin/settings",
        data={"csrf_token": csrf, "robot_stream_url": "http://192.168.1.150:81/stream"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/settings"

    # Verify AppSetting and AuditLog
    with Session(engine) as s:
        val = s.scalar(select(AppSetting.value).where(AppSetting.key == "robot_stream_url"))
        assert val == "http://192.168.1.150:81/stream"
        audit = s.scalars(select(AuditLog).where(AuditLog.target == "robot_stream_url")).one()
        assert audit.action == "setting.update"
        assert audit.actor == "admin"

    # POST empty URL to clear/delete setting
    r = admin_client.post(
        "/admin/settings",
        data={"csrf_token": csrf, "robot_stream_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with Session(engine) as s:
        val = s.scalar(select(AppSetting.value).where(AppSetting.key == "robot_stream_url"))
        assert val is None
