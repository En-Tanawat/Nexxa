from eval.run_eval import compute_metrics


def test_compute_metrics_none_pred_counts_wrong():
    rows = [("ปกติ", "ปกติ"), ("ปกติ", "ปกติ"), ("ใบเหลือง", "ใบเหลือง"), ("จุดน้ำตาล", None)]
    m = compute_metrics(rows)
    assert m["n"] == 4
    assert m["accuracy"] == 0.75
    # a failed prediction (None) is keyed by the string "None" in the confusion matrix
    assert m["confusion"] == {
        "ปกติ": {"ปกติ": 2},
        "ใบเหลือง": {"ใบเหลือง": 1},
        "จุดน้ำตาล": {"None": 1},
    }


def test_compute_metrics_empty():
    assert compute_metrics([]) == {"n": 0, "accuracy": 0.0, "confusion": {}}


# --- --provider flag and per-provider spend guard (no network: analyze_leaf is patched to fail) ---
import pytest  # noqa: E402

from app.providers import PROVIDERS  # noqa: E402
from eval import run_eval  # noqa: E402

ALL_KEY_ENVS = {p.key_env for p in PROVIDERS.values()}


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    """Clean env + analyzer that fails if called + an empty manifest. Returns the manifest path."""
    for k in ALL_KEY_ENVS | {"NEXXA_VISION_MODEL", "NEXXA_VISION_PROVIDER"}:
        monkeypatch.delenv(k, raising=False)

    def boom(*a, **k):
        raise AssertionError("analyze_leaf must not be called")

    monkeypatch.setattr(run_eval, "analyze_leaf", boom)
    manifest = tmp_path / "m.csv"
    manifest.write_text("filename,label\n", encoding="utf-8")
    return str(manifest)


def test_gemini_without_key_refused_naming_gemini_key(guarded, capsys):
    assert run_eval.main(["--manifest", guarded, "--provider", "gemini", "--confirm-spend"]) == 2
    err = capsys.readouterr().err
    assert "GEMINI_API_KEY" in err and "ANTHROPIC_API_KEY" not in err


def test_key_without_confirm_spend_refused(guarded, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-secret-value")
    assert run_eval.main(["--manifest", guarded, "--provider", "gemini"]) == 2
    assert "dummy-secret-value" not in capsys.readouterr().err


def test_anthropic_guard_unchanged(guarded, monkeypatch, capsys):
    assert run_eval.main(["--manifest", guarded, "--confirm-spend"]) == 2  # default provider, no key
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    assert run_eval.main(["--manifest", guarded]) == 2  # key but no confirm


def test_unknown_provider_returns_2_listing_valid_names(guarded, capsys):
    assert run_eval.main(["--manifest", guarded, "--provider", "xyz", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "xyz" in err and all(name in err for name in PROVIDERS)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_dry_run_never_calls_analyzer(guarded, monkeypatch, name):
    monkeypatch.setattr(run_eval, "seeded_crop", lambda: pytest.fail("dry-run must not build a crop/client"))
    assert run_eval.main(["--manifest", guarded, "--provider", name, "--dry-run"]) == 0


def test_non_anthropic_without_model_env_refused_before_any_call(guarded, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    assert run_eval.main(["--manifest", guarded, "--provider", "openai", "--confirm-spend"]) == 2
    assert "NEXXA_VISION_MODEL" in capsys.readouterr().err
