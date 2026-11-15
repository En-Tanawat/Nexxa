import random

import pytest

from eval import stress
from eval.stress import CONDITIONS, make_condition_image, run_api_stress, run_quality_stress


@pytest.mark.parametrize("kind", CONDITIONS)
def test_make_condition_image_bytes_and_deterministic(kind):
    a = make_condition_image(kind, random.Random(7))
    b = make_condition_image(kind, random.Random(7))
    assert isinstance(a, bytes)
    assert a == b


def test_required_conditions_present():
    required = {
        "sharp", "blur_light", "blur_heavy", "dark", "very_dark", "bright", "washed_out", "noise",
        "jpeg_low_quality", "net_shadow", "rotated", "grayscale", "png_alpha", "tiny", "oversize",
        "truncated_jpeg", "random_bytes", "empty",
    }
    assert required <= set(CONDITIONS)


@pytest.fixture(scope="module")
def quality_report():
    return run_quality_stress(2000, seed=1)


def _share(report, cond, key):
    counts = report.per_condition[cond]
    return counts[key] / sum(counts.values())


def test_quality_stress_expectations(quality_report):
    r = quality_report
    assert r.unexpected_exceptions == []
    assert r.invariant_failures == []
    for cond in ("empty", "random_bytes", "oversize"):
        assert _share(r, cond, "decode_error") == 1.0
    assert _share(r, "very_dark", "too_dark") == 1.0
    assert _share(r, "washed_out", "too_bright") == 1.0
    assert _share(r, "blur_heavy", "blurry") >= 0.95
    assert _share(r, "sharp", "ok") >= 0.95


def test_quality_stress_is_deterministic():
    assert run_quality_stress(150, seed=3).per_condition == run_quality_stress(150, seed=3).per_condition


def test_api_stress_invariants_hold():
    r = run_api_stress(300, seed=1)
    assert r.unexpected_exceptions == []
    assert r.invariant_failures == []
    assert sum(sum(c.values()) for c in r.per_condition.values()) == 300


def test_cli_returns_zero_and_prints_accuracy_warning(capsys):
    assert stress.main(["--quality", "200", "--api", "30", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "does NOT measure" in out
