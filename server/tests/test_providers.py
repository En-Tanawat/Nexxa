import json
import logging
from types import SimpleNamespace

import httpx2 as httpx
import openai
import pytest

from app.analyzer import analyze_leaf
from app.models import Crop
from app.providers import PROVIDERS, UnknownProviderError, get_provider

IMG = b"\xff\xd8\xff\xe0fakejpeg"
SECRET = "sk-test-secret-value"


@pytest.fixture
def crop():
    return Crop(name="กรีนคอส", labels=["ปกติ", "ใบเหลือง", "จุดน้ำตาล"], prompt="ดูใบผัก", notes="-")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("NEXXA_VISION_PROVIDER", "NEXXA_VISION_MODEL", "NEXXA_VISION_BASE_URL",
              "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXXA_VISION_MODEL", "some-model")


class FakeOpenAI:
    """Mimics client.chat.completions.create; never touches the network."""

    def __init__(self, content=None, finish="stop", error=None, model=None, choices=None):
        self.calls = []
        self._content, self._finish, self._error, self._model, self._choices = content, finish, error, model, choices
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self._error:
            raise self._error
        choices = self._choices if self._choices is not None else [SimpleNamespace(
            message=SimpleNamespace(content=self._content), finish_reason=self._finish)]
        resp = SimpleNamespace(choices=choices)
        if self._model:
            resp.model = self._model
        return resp


def answer(label, confidence, note="x"):
    return json.dumps({"label": label, "confidence": confidence, "note": note}, ensure_ascii=False)


def run(crop, client, provider="openai"):
    return analyze_leaf(IMG, crop, client=client, provider=provider)


def assert_failed(r):
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True and r.note


def test_provider_table():
    assert get_provider("gemini").base_url.endswith("/v1beta/openai/")
    assert get_provider("deepseek").base_url == "https://api.deepseek.com"
    assert get_provider("openai").base_url is None
    assert get_provider("qwen").base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert {n: p.key_env for n, p in PROVIDERS.items()} == {
        "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY"}
    with pytest.raises(UnknownProviderError):
        get_provider("xyz")


def test_openai_request_shape_and_result(crop):
    c = FakeOpenAI(answer("ใบเหลือง", 0.9))
    r = run(crop, c)
    assert (r.label, r.confidence, r.note, r.needs_review) == ("ใบเหลือง", 0.9, "x", False)
    assert r.model == "some-model"
    kw = c.calls[0]
    assert kw["model"] == "some-model" and kw["max_tokens"] == 8192
    assert "temperature" not in kw and "response_format" not in kw
    parts = kw["messages"][0]["content"]
    assert kw["messages"][0]["role"] == "user" and [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "JSON" in parts[0]["text"]


def test_records_answering_model(crop):
    assert run(crop, FakeOpenAI(answer("ปกติ", 0.9), model="served-model")).model == "served-model"


def test_fenced_json_with_leading_prose(crop):
    text = "นี่คือผลวิเคราะห์:\n```json\n" + answer("จุดน้ำตาล", 0.8) + "\n```\n"
    r = run(crop, FakeOpenAI(text))
    assert r.label == "จุดน้ำตาล" and r.needs_review is False


@pytest.mark.parametrize("client", [
    FakeOpenAI(answer("ปกติ", 0.9), finish="content_filter"),
    FakeOpenAI(choices=[]),
    FakeOpenAI(content=None),
])
def test_odd_replies_need_review(crop, client):
    assert_failed(run(crop, client))


def test_length_finish_says_truncated(crop):
    r = run(crop, FakeOpenAI(answer("ปกติ", 0.9), finish="length"))
    assert_failed(r)
    assert "ถูกตัด" in r.note


def test_model_unset_needs_review(crop, monkeypatch):
    monkeypatch.delenv("NEXXA_VISION_MODEL")
    c = FakeOpenAI(answer("ปกติ", 0.9))
    r = run(crop, c)
    assert_failed(r)
    assert "NEXXA_VISION_MODEL" in r.note and c.calls == []


def test_missing_key_names_env_var_only(crop, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)  # a different provider's key must not leak either
    r = run(crop, None, "gemini")
    assert_failed(r)
    assert "GEMINI_API_KEY" in r.note and SECRET not in r.note


def test_client_built_with_provider_base_url_and_override(crop, monkeypatch):
    seen = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setattr("app.analyzer.openai.OpenAI",
                        lambda **kw: seen.append(kw) or FakeOpenAI(answer("ปกติ", 0.9)))
    assert run(crop, None, "deepseek").label == "ปกติ"
    monkeypatch.setenv("NEXXA_VISION_BASE_URL", "http://localhost:9/v1")
    run(crop, None, "deepseek")
    assert [kw["base_url"] for kw in seen] == ["https://api.deepseek.com", "http://localhost:9/v1"]
    assert all(kw["timeout"] == 60 for kw in seen)


def test_provider_from_env(crop, monkeypatch):
    monkeypatch.setenv("NEXXA_VISION_PROVIDER", "qwen")
    c = FakeOpenAI(answer("ปกติ", 0.9))
    assert analyze_leaf(IMG, crop, client=c).label == "ปกติ" and c.calls


def test_unknown_provider_does_not_raise(crop):
    assert_failed(run(crop, FakeOpenAI(answer("ปกติ", 0.9)), "xyz"))


@pytest.mark.parametrize("error", [
    openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")),
    TypeError("boom"),
])
def test_sdk_failure_logged_not_raised(crop, caplog, error):
    with caplog.at_level(logging.ERROR, logger="app.analyzer"):
        r = run(crop, FakeOpenAI(error=error))
    assert_failed(r)
    assert any(rec.exc_info for rec in caplog.records)


def test_unparseable_reply_logged(crop, caplog):
    with caplog.at_level(logging.ERROR, logger="app.analyzer"):
        r = run(crop, FakeOpenAI("ใบดูปกติดี"))
    assert_failed(r)
    assert any(rec.exc_info for rec in caplog.records)


def test_shared_validation(crop):
    r = run(crop, FakeOpenAI(answer("โรคอื่น", 0.95)))
    assert_failed(r)
    r = run(crop, FakeOpenAI(answer("ปกติ", 1.5)))
    assert_failed(r)
    r = run(crop, FakeOpenAI(answer("ใบเหลือง", 0.4)))
    assert r.label == "ใบเหลือง" and r.needs_review is True
