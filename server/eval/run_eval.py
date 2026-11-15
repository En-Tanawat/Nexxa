"""Eval harness: python -m eval.run_eval --manifest eval/manifest.csv [--dry-run | --confirm-spend]

The paid step needs BOTH env ANTHROPIC_API_KEY and --confirm-spend. The key is never printed.
"""
import argparse
import csv
import os
import sys
import time
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.analyzer import analyze_leaf
from app.config import settings
from app.db import init_db
from app.models import Crop
from app.quality import ImageDecodeError, check_quality
from app.seed import CROP_NAME, seed

IMAGES_DIR = Path(__file__).parent / "images"


def compute_metrics(rows: list[tuple[str, str | None]]) -> dict:
    """rows = (true_label, predicted_label). pred None = failed analysis, counted wrong, keyed "None"."""
    confusion: dict[str, dict[str, int]] = {}
    for true, pred in rows:
        cell = confusion.setdefault(true, {})
        cell[str(pred)] = cell.get(str(pred), 0) + 1
    correct = sum(1 for t, p in rows if t == p)
    return {"n": len(rows), "accuracy": correct / len(rows) if rows else 0.0, "confusion": confusion}


def load_manifest(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def seeded_crop() -> Crop:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as s:
        seed(s)
        crop = s.scalar(select(Crop).where(Crop.name == CROP_NAME))
        s.expunge(crop)
        return crop


def print_confusion(confusion: dict) -> None:
    preds = sorted({p for row in confusion.values() for p in row})
    print("confusion (rows=true, cols=pred):")
    print("  " + "\t".join(["true/pred", *preds]))
    for true, row in sorted(confusion.items()):
        print("  " + "\t".join([true, *(str(row.get(p, 0)) for p in preds)]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="eval/manifest.csv")
    ap.add_argument("--dry-run", action="store_true", help="quality gate + label distribution only, no API call")
    ap.add_argument("--confirm-spend", action="store_true", help="acknowledge that the Claude calls cost real money")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a non-UTF-8 codepage
        stream.reconfigure(encoding="utf-8")

    if not args.dry_run and not (os.environ.get("ANTHROPIC_API_KEY") and args.confirm_spend):
        print(
            "ปฏิเสธการรัน: การประเมินเรียก Claude API ซึ่งเสียเงินจริง ต้องตั้ง ANTHROPIC_API_KEY และใส่ --confirm-spend ทั้งคู่\n"
            "REFUSED: this calls the paid Claude API. Set env ANTHROPIC_API_KEY AND pass --confirm-spend "
            "(or use --dry-run for the quality gate only).",
            file=sys.stderr,
        )
        return 2

    manifest = load_manifest(args.manifest)
    print(f"manifest: {len(manifest)} rows; label distribution: {dict(Counter(r['label'] for r in manifest))}")

    passed, rejected, missing = [], [], []
    for r in manifest:
        path = IMAGES_DIR / r["filename"]
        if not path.is_file():
            missing.append(r["filename"])
            continue
        data = path.read_bytes()
        try:
            q = check_quality(data)
        except ImageDecodeError:
            rejected.append((r["filename"], "undecodable"))
            continue
        if q.ok:
            passed.append((r, data))
        else:
            rejected.append((r["filename"], f"{q.reason} blur={q.blur_score:.0f} brightness={q.brightness:.0f}"))
    print(f"quality gate: {len(passed)} passed, {len(rejected)} rejected, {len(missing)} missing files")
    for name, why in rejected:
        print(f"  rejected: {name} ({why})")
    for name in missing:
        print(f"  missing: {name}")
    if args.dry_run:
        print("dry-run: no API call made")
        return 0

    crop = seeded_crop()
    print(f"model: {settings.vision_model}")
    rows, wrong = [], []
    start = time.perf_counter()
    for r, data in passed:
        res = analyze_leaf(data, crop)
        rows.append((r["label"], res.label))
        if res.label != r["label"]:
            wrong.append((r["filename"], r["label"], res.label, res.confidence, res.note))
    secs = (time.perf_counter() - start) / len(passed) if passed else 0.0

    m = compute_metrics(rows)
    print(f"n={m['n']} accuracy={m['accuracy']:.3f} (None = failed analysis, counted wrong)")
    print_confusion(m["confusion"])
    print("wrong answers (filename, true, pred, confidence, note):")
    for w in wrong:
        print("  " + " | ".join(map(str, w)))
    print(f"seconds/image: {secs:.2f}")
    print("caveat: web images are not ESP32-CAM greenhouse shots; accuracy is optimistic. "
          "Token usage is not reported (analyze_leaf does not expose it).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
