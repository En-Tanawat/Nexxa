"""Offline stress harness (no network, no API key): python -m eval.stress --quality N --api N --seed S

Checks robustness and invariants of the quality gate and the /captures route under many synthetic
bad/good images. It does NOT measure the AI's disease-detection accuracy.
"""
import argparse
import dataclasses
import os
import random
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import analyzer, storage
from app import main as app_main
from app.analyzer import AnalysisResult
from app.db import get_session, init_db
from app.models import Analysis, Capture, Station
from app.quality import ImageDecodeError, check_quality
from app.seed import seed as seed_db

CONDITIONS: tuple[str, ...] = (
    "sharp", "blur_light", "blur_heavy", "dark", "very_dark", "bright", "washed_out", "noise",
    "jpeg_low_quality", "net_shadow", "rotated", "grayscale", "png_alpha", "tiny", "oversize",
    "truncated_jpeg", "random_bytes", "empty",
)
OUTCOMES = ("ok", "blurry", "too_dark", "too_bright", "decode_error")
WARNING = "WARNING: this harness measures robustness/invariants only; it does NOT measure the AI's disease-detection accuracy."
API_INVARIANTS = (
    "status_allowed", "expected_status", "analyzer_gate", "no_analysis_on_failed_quality",
    "capture_count", "analysis_count", "one_analysis_per_key",
)
QUALITY_INVARIANTS = ("known_outcome",)
MANIFEST = Path(__file__).parent / "manifest.csv"
IMAGES_DIR = Path(__file__).parent / "images"
MAX_PRINT = 20


@dataclass
class StressReport:
    n: int
    per_condition: dict[str, dict[str, int]]  # quality: OUTCOMES counts; api: HTTP status (as str) counts
    unexpected_exceptions: list[str] = field(default_factory=list)
    invariant_failures: list[str] = field(default_factory=list)  # each starts with "[invariant_name]"
    seconds: float = 0.0
    real_per_condition: dict[str, dict[str, int]] = field(default_factory=dict)  # quality only, report-only


# ---------- image generation ----------

def _png(img: np.ndarray, level: int = 1) -> bytes:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, level])
    assert ok
    return buf.tobytes()


def _jpeg(img: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def _enc(img: np.ndarray) -> bytes:
    return _jpeg(img, 95)  # JPEG like the ESP32-CAM sends; ~20x cheaper to encode than noisy PNG


def _clip(a: np.ndarray) -> np.ndarray:
    return np.clip(a, 0, 255).astype(np.uint8)


def _leaf(g: np.random.Generator) -> np.ndarray:
    """320x240 leaf-like texture: green ground, blobs, veins, fine grain (so sharp = high Laplacian variance)."""
    h, w = 240, 320
    base = np.array([g.integers(30, 70), g.integers(110, 160), g.integers(40, 90)], dtype=int)
    img = np.tile(base.astype(np.uint8), (h, w, 1))
    for _ in range(int(g.integers(10, 25))):
        color = tuple(int(c) for c in np.clip(base + g.integers(-40, 41, 3), 0, 255))
        cv2.ellipse(img, (int(g.integers(0, w)), int(g.integers(0, h))), (int(g.integers(5, 50)), int(g.integers(5, 40))),
                    float(g.integers(0, 180)), 0, 360, color, -1)
    for _ in range(int(g.integers(8, 20))):
        color = tuple(int(c) for c in np.clip(base + g.integers(-60, 61, 3), 0, 255))
        cv2.line(img, (int(g.integers(0, w)), int(g.integers(0, h))), (int(g.integers(0, w)), int(g.integers(0, h))), color, int(g.integers(1, 4)))
    return _clip(img + g.normal(0, 10, img.shape))


@lru_cache(maxsize=1)
def synthetic_pool() -> tuple[np.ndarray, ...]:
    return tuple(_leaf(np.random.default_rng(1000 + i)) for i in range(8))


@lru_cache(maxsize=1)
def real_pool() -> tuple[np.ndarray, ...]:
    """eval/manifest.csv photos, downscaled so 100k derived images stay cheap. Empty if absent."""
    if not MANIFEST.exists():
        return ()
    import csv
    out = []
    with open(MANIFEST, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            img = cv2.imread(str(IMAGES_DIR / row["filename"]), cv2.IMREAD_COLOR)
            if img is not None:
                s = 480 / max(img.shape[:2])
                out.append(cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img)
    return tuple(out)


@lru_cache(maxsize=1)
def _oversize_png() -> bytes:
    return _png(np.full((5000, 5000), 128, np.uint8), level=9)  # flat: a few KB, but over the 4096 pixel cap


def make_condition_image(kind: str, rng: random.Random, bases: tuple[np.ndarray, ...] | None = None) -> bytes:
    if kind == "empty":
        return b""
    if kind == "random_bytes":
        return rng.randbytes(rng.randint(1, 2000))
    if kind == "oversize":
        return _oversize_png()
    pool = bases or synthetic_pool()
    img = pool[rng.randrange(len(pool))]
    g = np.random.default_rng(rng.getrandbits(64))
    f = img.astype(np.float32)
    if kind == "sharp":
        return _enc(img)
    if kind == "blur_light":
        return _enc(cv2.GaussianBlur(img, (0, 0), rng.uniform(0.8, 1.6)))
    if kind == "blur_heavy":
        return _enc(cv2.GaussianBlur(img, (0, 0), rng.uniform(8, 14)))
    if kind == "dark":
        return _enc(_clip(f * rng.uniform(0.25, 0.5)))
    if kind == "very_dark":
        return _enc(_clip(f * rng.uniform(0.03, 0.15)))
    if kind == "bright":
        return _enc(_clip(f * rng.uniform(1.3, 1.8) + rng.uniform(20, 60)))
    if kind == "washed_out":
        return _enc(_clip(255 - (255 - f) * rng.uniform(0.03, 0.12)))
    if kind == "noise":
        return _enc(_clip(f + g.normal(0, rng.uniform(20, 50), f.shape)))
    if kind == "jpeg_low_quality":
        return _jpeg(img, rng.randint(2, 15))
    if kind == "net_shadow":
        step, thick = rng.randint(5, 10), rng.randint(1, 2)
        mask = np.zeros(img.shape[:2], np.float32)
        for k in range(thick):
            mask[k::step, :] = 1
            mask[:, k::step] = 1
        return _enc(_clip(f * (1 - rng.uniform(0.35, 0.65) * mask[..., None])))
    if kind == "rotated":
        h, w = img.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), rng.uniform(-45, 45), 1.0)
        return _enc(cv2.warpAffine(img, m, (w, h)))
    if kind == "grayscale":
        return _enc(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    if kind == "png_alpha":
        alpha = g.integers(80, 256, img.shape[:2], dtype=np.uint8)
        return _png(np.dstack([img, alpha]), level=0)
    if kind == "tiny":
        return _png(cv2.resize(img, (8, 8), interpolation=cv2.INTER_AREA))
    if kind == "truncated_jpeg":
        data = _jpeg(img, 90)
        return data[: max(1, int(len(data) * rng.uniform(0.05, 0.9)))]
    raise ValueError(f"unknown condition {kind!r}")


# ---------- quality stress ----------

def _outcome(data: bytes) -> str:
    try:
        return check_quality(data).reason or "ok"
    except ImageDecodeError:
        return "decode_error"


def _quality_loop(n: int, rng: random.Random, bases, tag: str, report_failures: list[str], exceptions: list[str]):
    table = {c: dict.fromkeys(OUTCOMES, 0) for c in CONDITIONS}
    for i in range(n):
        cond = CONDITIONS[i % len(CONDITIONS)]
        try:
            outcome = _outcome(make_condition_image(cond, rng, bases))
        except Exception as e:  # noqa: BLE001 - anything but ImageDecodeError is a finding
            exceptions.append(f"{tag}{cond}#{i}: {type(e).__name__}: {e}")
            continue
        if outcome not in OUTCOMES:
            report_failures.append(f"[known_outcome] {tag}{cond}#{i}: {outcome}")
        table[cond][outcome] = table[cond].get(outcome, 0) + 1
    return table


def run_quality_stress(n: int, seed: int) -> StressReport:
    t0 = time.perf_counter()
    report = StressReport(n=n, per_condition={})
    report.per_condition = _quality_loop(n, random.Random(seed), None, "", report.invariant_failures, report.unexpected_exceptions)
    bases = real_pool()
    if bases:  # report-only table over the real photos; failures here still count as unexpected exceptions
        report.real_per_condition = _quality_loop(
            min(n // 10, 20 * len(CONDITIONS)), random.Random(f"{seed}-real"), bases, "real:",
            report.invariant_failures, report.unexpected_exceptions)
    report.seconds = time.perf_counter() - t0
    return report


# ---------- api stress ----------

KEY = "stress-device-key"  # test-only value
OVERSIZE_BODY = b"x" * (6 * 1024 * 1024)
ALLOWED = {200, 404, 409, 413, 422}


def _fail(failures: list[str], name: str, msg: str) -> None:
    failures.append(f"[{name}] {msg}")


def run_api_stress(n: int, seed: int) -> StressReport:
    t0 = time.perf_counter()
    rng = random.Random(seed)
    report = StressReport(n=n, per_condition={})
    failures = report.invariant_failures
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as s:
        seed_db(s)
        station_uids = [u for u in s.scalars(select(Station.rfid_uid).where(Station.kind == "station").order_by(Station.id))]
        home_uid = s.scalars(select(Station.rfid_uid).where(Station.kind == "home")).one()

    def override():
        with Session(engine) as s:
            yield s

    received: list[bool] = []  # per analyze_leaf call: did that image pass check_quality?

    def fake(image_bytes, crop, client=None):
        try:
            passed = check_quality(image_bytes).ok
        except ImageDecodeError:
            passed = False
        received.append(passed)
        if rng.random() < 0.1:
            return AnalysisResult(None, 0.0, "fake: needs review", True, "fake")
        return AnalysisResult("ปกติ", 0.9, "fake", False, "fake")

    tmp = None
    old_settings, old_analyze, old_key = storage.settings, analyzer.analyze_leaf, os.environ.get("NEXXA_DEVICE_KEY")
    ok200 = 0
    try:
        tmp = Path(tempfile.mkdtemp(prefix="nexxa-stress-"))
        app_main.app.dependency_overrides[get_session] = override
        storage.settings = dataclasses.replace(old_settings, image_dir=tmp)
        analyzer.analyze_leaf = fake
        os.environ["NEXXA_DEVICE_KEY"] = KEY
        client = TestClient(app_main.app, raise_server_exceptions=False)  # a route crash comes back as a 500, not an exception
        good = {"X-Device-Key": KEY}
        round_id = client.post("/rounds", headers=good).json()["round_id"]
        for i in range(n):
            cond = "?"
            try:
                if i and i % 25 == 0:
                    round_id = client.post("/rounds", headers=good).json()["round_id"]
                roll = rng.random()
                before = len(received)
                if roll < 0.02:  # deliberately wrong / missing key
                    cond = "(bad_key)"
                    headers = rng.choice([{}, {"X-Device-Key": "wrong"}, {"X-Device-Key": ""}])
                    r = client.post("/captures", headers=headers, files={"image": ("l.png", b"x", "image/png")},
                                    data={"rfid_uid": station_uids[0], "round_id": round_id})
                    expected = {401}
                elif roll < 0.025:  # Content-Length over the cap: rejected before the form is parsed
                    cond = "(oversize_body)"
                    r = client.post("/captures", headers={**good, "Content-Type": "multipart/form-data; boundary=x"}, content=OVERSIZE_BODY)
                    expected = {413}
                else:
                    cond = CONDITIONS[rng.randrange(len(CONDITIONS))]
                    data = make_condition_image(cond, rng)
                    u = rng.random()
                    uid = rng.choice(station_uids) if u < 0.85 else home_uid if u < 0.92 else f"UNKNOWN-{rng.randrange(1000)}"
                    rid = 999999 if rng.random() < 0.01 else round_id
                    form = {"rfid_uid": uid, "round_id": rid}
                    angle = rng.choice(["top", "side", "bottom", None])
                    if angle is not None:
                        form["camera_angle"] = angle
                    try:
                        q = check_quality(data)
                    except ImageDecodeError:
                        q = None
                    r = client.post("/captures", headers=good, files={"image": ("l.png", data, "image/png")}, data=form)
                    if uid == home_uid:
                        expected = {422}
                    elif uid not in station_uids:
                        expected = {404}
                    elif rid == 999999:
                        expected = {404}
                    elif q is None:
                        expected = {422}  # undecodable / over the pixel cap
                    else:
                        expected = {200, 409}
                    if r.status_code == 200:
                        status = r.json()["status"]
                        want = {"analyzed", "needs_review"} if q and q.ok else {"retake", "needs_review"}
                        if status not in want:
                            _fail(failures, "expected_status", f"{cond}#{i} seed={seed}: 200 with status {status!r}, wanted one of {sorted(want)}")
                code = r.status_code
                report.per_condition.setdefault(cond, {}).setdefault(str(code), 0)
                report.per_condition[cond][str(code)] += 1
                ok200 += code == 200
                if code not in ALLOWED | {401}:
                    _fail(failures, "status_allowed", f"{cond}#{i} seed={seed}: status {code}")
                elif code not in expected:
                    _fail(failures, "expected_status", f"{cond}#{i} seed={seed}: status {code}, wanted one of {sorted(expected)}")
                if len(received) > before and not received[-1]:
                    _fail(failures, "analyzer_gate", f"{cond}#{i} seed={seed}: analyze_leaf received an image that fails check_quality")
            except Exception as e:  # noqa: BLE001 - anything unexpected is reported with its condition and iteration
                report.unexpected_exceptions.append(f"{cond}#{i} seed={seed}: {type(e).__name__}: {e}")

        with Session(engine) as s:
            if s.scalar(select(func.count(Analysis.id)).join(Capture, Analysis.capture_id == Capture.id).where(Capture.quality_ok.is_(False))):
                _fail(failures, "no_analysis_on_failed_quality", "Analysis row bound to a capture with quality_ok False")
            captures = s.scalar(select(func.count(Capture.id)))
            if captures != ok200:
                _fail(failures, "capture_count", f"{captures} Capture rows but {ok200} requests returned 200 (seed={seed})")
            analyses = s.scalar(select(func.count(Analysis.id)))
            if analyses != len(received):
                _fail(failures, "analysis_count", f"{analyses} Analysis rows but analyze_leaf was called {len(received)} times (seed={seed})")
            keys = Counter(s.execute(
                select(Capture.round_id, Capture.station_id, Capture.camera_angle).join(Analysis, Analysis.capture_id == Capture.id)).all())
            for key, count in keys.items():
                if count > 1:
                    _fail(failures, "one_analysis_per_key", f"(round, station, angle)={tuple(key)} has {count} analyses (seed={seed})")
    except Exception as e:  # noqa: BLE001 - setup or post-run check failed
        report.unexpected_exceptions.append(f"api setup or post-run check (seed={seed}): {type(e).__name__}: {e}")
    finally:
        app_main.app.dependency_overrides.pop(get_session, None)
        storage.settings = old_settings
        analyzer.analyze_leaf = old_analyze
        if old_key is None:
            os.environ.pop("NEXXA_DEVICE_KEY", None)
        else:
            os.environ["NEXXA_DEVICE_KEY"] = old_key
        engine.dispose()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    report.seconds = time.perf_counter() - t0
    return report


# ---------- CLI ----------

def _print_table(title: str, table: dict[str, dict[str, int]], cols: list[str]) -> None:
    print(f"\n{title}")
    print(f"  {'condition':<18}" + "".join(f"{c:>14}" for c in cols))
    for cond, counts in table.items():
        print(f"  {cond:<18}" + "".join(f"{counts.get(c, 0):>14}" for c in cols))


def _print_invariants(report: StressReport, names: tuple[str, ...]) -> None:
    print("invariants:")
    for name in names:
        bad = [f for f in report.invariant_failures if f.startswith(f"[{name}]")]
        print(f"  {'FAIL' if bad else 'PASS'}  {name}" + (f" ({len(bad)})" if bad else ""))
    print(f"  {'FAIL' if report.unexpected_exceptions else 'PASS'}  no_unexpected_exceptions"
          + (f" ({len(report.unexpected_exceptions)})" if report.unexpected_exceptions else ""))
    for line in (report.invariant_failures + report.unexpected_exceptions)[:MAX_PRINT]:
        print("    " + line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline stress harness (no API key).")
    ap.add_argument("--quality", type=int, default=100000)
    ap.add_argument("--api", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    print(WARNING)
    bad = False
    if args.quality > 0:
        r = run_quality_stress(args.quality, args.seed)
        _print_table(f"quality gate: {r.n} images, seed {args.seed}", r.per_condition, list(OUTCOMES))
        if r.real_per_condition:
            _print_table("same conditions over real photos (eval/manifest.csv, downscaled to <=480 px long side, report-only)", r.real_per_condition, list(OUTCOMES))
        _print_invariants(r, QUALITY_INVARIANTS)
        print(f"elapsed: {r.seconds:.1f}s")
        bad |= bool(r.invariant_failures or r.unexpected_exceptions)
    if args.api > 0:
        r = run_api_stress(args.api, args.seed)
        codes = sorted({c for row in r.per_condition.values() for c in row})
        _print_table(f"POST /captures: {r.n} requests, seed {args.seed} (columns = HTTP status)", r.per_condition, codes)
        _print_invariants(r, API_INVARIANTS)
        print(f"elapsed: {r.seconds:.1f}s")
        bad |= bool(r.invariant_failures or r.unexpected_exceptions)
    print(WARNING)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
