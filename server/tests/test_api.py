import dataclasses
import inspect
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
from app.db import get_session, init_db
from app.models import Analysis, Capture, Station
from app.seed import seed


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _sharp() -> np.ndarray:
    y, x = np.indices((128, 128))
    gray = np.where(((x // 8) + (y // 8)) % 2 == 0, 50, 200).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


KEY = "test-device-key"  # test-only value
GOOD = _png(_sharp())
BLURRY = _png(cv2.GaussianBlur(_sharp(), (0, 0), sigmaX=15))


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as s:
        seed(s)

    def override():
        with Session(engine) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    monkeypatch.setattr(storage, "settings", dataclasses.replace(storage.settings, image_dir=tmp_path))
    calls = []

    def fake(image_bytes, crop, client=None):
        calls.append(crop.name)
        return AnalysisResult("ปกติ", 0.9, "ok", False, "m")

    monkeypatch.setattr(analyzer, "analyze_leaf", fake)
    monkeypatch.setenv("NEXXA_DEVICE_KEY", KEY)
    client = TestClient(main.app, headers={"X-Device-Key": KEY})
    yield client, engine, calls
    main.app.dependency_overrides.clear()


def _uid(engine, kind="station"):
    with Session(engine) as s:
        return s.scalars(select(Station).where(Station.kind == kind).order_by(Station.id)).first().rfid_uid


def _post(client, engine, image=GOOD, uid=None, round_id=None, kind="station", angle="top"):
    if round_id is None:
        round_id = client.post("/rounds").json()["round_id"]
    return client.post(
        "/captures",
        files={"image": ("leaf.png", image, "image/png")},
        data={"rfid_uid": uid or _uid(engine, kind), "round_id": round_id, "camera_angle": angle},
    )


def test_good_image_analyzed(env):
    client, engine, calls = env
    rid = client.post("/rounds").json()["round_id"]
    r = _post(client, engine, round_id=rid)
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["label"], body["confidence"], body["attempt"]) == ("analyzed", "ปกติ", 0.9, 1)
    with Session(engine) as s:
        cap = s.get(Capture, body["capture_id"])
        station = s.scalars(select(Station).where(Station.rfid_uid == _uid(engine))).one()
        assert (cap.station_id, cap.round_id, cap.crop_id) == (station.id, rid, station.crop_id)
        assert cap.quality_ok is True
        assert storage.settings.image_dir in Path(cap.image_path).parents
        assert s.scalars(select(Analysis).where(Analysis.capture_id == cap.id)).one().label == "ปกติ"
    assert len(calls) == 1


def test_blurry_image_retake(env):
    client, engine, calls = env
    r = _post(client, engine, image=BLURRY)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["label"], r.json()["confidence"]) == ("retake", None, None)
    with Session(engine) as s:
        cap = s.scalars(select(Capture)).one()
        assert (cap.quality_ok, cap.quality_reason) == (False, "blurry")
        assert s.scalars(select(Analysis)).first() is None
    assert calls == []


def test_three_blurry_uploads_need_review(env):
    client, engine, calls = env
    rid = client.post("/rounds").json()["round_id"]
    results = [_post(client, engine, image=BLURRY, round_id=rid).json() for _ in range(3)]
    assert [r["status"] for r in results] == ["retake", "retake", "needs_review"]
    assert [r["attempt"] for r in results] == [1, 2, 3]
    with Session(engine) as s:
        assert s.scalars(select(Analysis)).first() is None
    assert calls == []


def test_unknown_rfid_404(env):
    client, engine, _ = env
    assert _post(client, engine, uid="nope").status_code == 404


def test_home_rfid_422(env):
    client, engine, _ = env
    assert _post(client, engine, kind="home").status_code == 422


def test_non_image_422(env):
    client, engine, calls = env
    assert _post(client, engine, image=b"not an image").status_code == 422
    assert calls == []


def test_analysis_needs_review(env, monkeypatch):
    client, engine, _ = env
    monkeypatch.setattr(analyzer, "analyze_leaf", lambda b, c, client=None: AnalysisResult(None, 0.0, "refused", True, "m"))
    r = _post(client, engine)
    assert (r.status_code, r.json()["status"], r.json()["label"]) == (200, "needs_review", None)
    with Session(engine) as s:
        a = s.scalars(select(Analysis)).one()
        assert a.needs_review is True and a.note == "refused"


def test_oversized_upload_413(env):
    client, engine, calls = env
    assert _post(client, engine, image=b"\0" * (5 * 1024 * 1024 + 1)).status_code == 413
    assert calls == []


def test_unknown_round_404(env):
    client, engine, _ = env
    assert _post(client, engine, round_id=9999).status_code == 404


# --- device key ---

@pytest.mark.parametrize("headers", [{}, {"X-Device-Key": "wrong"}, {"X-Device-Key": b"caf" + bytes([233])}])
def test_bad_key_401_on_both_routes(env, headers):
    client, engine, calls = env
    c = TestClient(main.app)  # no default key header
    assert c.post("/rounds", headers=headers).status_code == 401
    r = c.post(
        "/captures", headers=headers,
        files={"image": ("l.png", GOOD, "image/png")},
        data={"rfid_uid": _uid(engine), "round_id": 1},
    )
    assert r.status_code == 401
    assert calls == []


def test_unset_key_rejects_everything(env, monkeypatch):
    client, engine, calls = env
    monkeypatch.delenv("NEXXA_DEVICE_KEY")
    assert client.post("/rounds").status_code == 401
    assert client.post("/rounds", headers={"X-Device-Key": ""}).status_code == 401
    monkeypatch.setenv("NEXXA_DEVICE_KEY", "")
    assert client.post("/rounds", headers={"X-Device-Key": ""}).status_code == 401


# --- upload size / threading ---

def test_content_length_rejected_before_form_is_parsed(env):
    client, _, calls = env
    # not valid multipart: without the early check this would be a 422 after parsing
    r = client.post(
        "/captures", content=b"x" * (6 * 1024 * 1024),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    assert r.status_code == 413
    assert calls == []


def test_capture_route_is_sync_so_it_runs_in_threadpool():
    assert not inspect.iscoroutinefunction(main.create_capture)


# --- retake / duplicate accounting ---

def test_each_angle_has_own_retake_budget(env):
    client, engine, calls = env
    rid = client.post("/rounds").json()["round_id"]
    top = [_post(client, engine, image=BLURRY, round_id=rid, angle="top").json() for _ in range(3)]
    side = [_post(client, engine, image=BLURRY, round_id=rid, angle="side").json() for _ in range(3)]
    assert [r["status"] for r in top] == ["retake", "retake", "needs_review"]
    assert [r["status"] for r in side] == ["retake", "retake", "needs_review"]
    with Session(engine) as s:
        paths = [c.image_path for c in s.scalars(select(Capture))]
    assert len(set(paths)) == 6 and all(Path(p).exists() for p in paths)


def test_good_shots_do_not_eat_retake_budget(env):
    client, engine, calls = env
    rid = client.post("/rounds").json()["round_id"]
    assert _post(client, engine, round_id=rid, angle="top").json()["status"] == "analyzed"
    # a different angle: its first blurry shot is still attempt 1 / "retake"
    r = _post(client, engine, image=BLURRY, round_id=rid, angle="side").json()
    assert (r["status"], r["attempt"]) == ("retake", 1)


def test_good_then_reupload_same_angle_409_no_second_analysis(env):
    client, engine, calls = env
    rid = client.post("/rounds").json()["round_id"]
    assert _post(client, engine, round_id=rid).status_code == 200
    assert _post(client, engine, round_id=rid).status_code == 409
    assert _post(client, engine, image=BLURRY, round_id=rid).status_code == 409
    assert len(calls) == 1
    with Session(engine) as s:
        assert len(s.scalars(select(Capture)).all()) == 1


def test_retake_then_good_is_analyzed_once(env):
    client, engine, calls = env
    rid = client.post("/rounds").json()["round_id"]
    assert _post(client, engine, image=BLURRY, round_id=rid).json()["status"] == "retake"
    r = _post(client, engine, round_id=rid).json()
    assert (r["status"], r["attempt"]) == ("analyzed", 2)
    assert len(calls) == 1


def test_counting_is_per_station_and_per_round(env):
    client, engine, calls = env
    with Session(engine) as s:
        uids = [u.rfid_uid for u in s.scalars(select(Station).where(Station.kind == "station").order_by(Station.id))]
    r1, r2 = (client.post("/rounds").json()["round_id"] for _ in range(2))
    for _ in range(3):
        _post(client, engine, image=BLURRY, uid=uids[0], round_id=r1)
    # other station, same round; same station, other round: both start fresh
    a = _post(client, engine, image=BLURRY, uid=uids[1], round_id=r1).json()
    b = _post(client, engine, image=BLURRY, uid=uids[0], round_id=r2).json()
    assert (a["status"], a["attempt"]) == ("retake", 1)
    assert (b["status"], b["attempt"]) == ("retake", 1)
    # and a good capture at station 0 round 2 is not blocked by round 1 history
    assert _post(client, engine, uid=uids[0], round_id=r2).status_code == 200
