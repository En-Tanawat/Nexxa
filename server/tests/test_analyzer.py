import json
import logging
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from app.analyzer import analyze_leaf, build_prompt
from app.config import settings
from app.models import Crop


@pytest.fixture
def crop():
    return Crop(
        name="กรีนคอส",
        labels=["ปกติ", "ใบเหลือง", "จุดน้ำตาล"],
        prompt="ดูใบผักและจัดประเภท",
        notes="ใบแก่เหลืองตามธรรมชาติ และ tipburn ไม่นับเป็นโรค",
    )


class FakeClient:
    """Mimics client.beta.messages.create; never touches the network."""

    def __init__(self, text=None, stop_reason="end_turn", error=None, model=None):
        self.calls = []
        self._text, self._stop, self._error, self._model = text, stop_reason, error, model
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        content = [SimpleNamespace(type="text", text=self._text)] if self._text is not None else []
        resp = SimpleNamespace(stop_reason=self._stop, content=content)
        if self._model:
            resp.model = self._model
        return resp


def answer(label, confidence, note="x"):
    return json.dumps({"label": label, "confidence": confidence, "note": note}, ensure_ascii=False)


IMG = b"\xff\xd8\xff\xe0fakejpeg"


def test_build_prompt_has_name_labels_and_notes(crop):
    p = build_prompt(crop)
    assert crop.name in p and crop.notes in p and crop.prompt in p
    assert all(label in p for label in crop.labels)


def test_confident_label_needs_no_review(crop):
    c = FakeClient(answer("ใบเหลือง", 0.9))
    r = analyze_leaf(IMG, crop, client=c)
    assert (r.label, r.confidence, r.note, r.needs_review) == ("ใบเหลือง", 0.9, "x", False)
    assert r.model == settings.vision_model
    call = c.calls[0]
    assert call["model"] == settings.vision_model
    assert call["output_config"]["format"]["schema"]["properties"]["label"]["enum"] == crop.labels
    assert call["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"


def test_png_media_type_detected(crop):
    c = FakeClient(answer("ปกติ", 0.9))
    analyze_leaf(b"\x89PNG\r\n\x1a\nrest", crop, client=c)
    assert c.calls[0]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_low_confidence_needs_review(crop):
    r = analyze_leaf(IMG, crop, client=FakeClient(answer("ใบเหลือง", 0.4)))
    assert r.label == "ใบเหลือง" and r.needs_review is True


def test_unknown_label_dropped(crop):
    r = analyze_leaf(IMG, crop, client=FakeClient(answer("โรคอื่น", 0.95)))
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True


def test_refusal_never_guesses(crop):
    r = analyze_leaf(IMG, crop, client=FakeClient(answer("ปกติ", 0.99), stop_reason="refusal"))
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True and r.note


def test_sdk_error_does_not_raise(crop):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    r = analyze_leaf(IMG, crop, client=FakeClient(error=err))
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True and r.note


def test_non_json_answer(crop):
    r = analyze_leaf(IMG, crop, client=FakeClient("ใบดูปกติดี"))
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True


def test_sdk_typeerror_is_api_failure_not_unreadable(crop, caplog):
    err = TypeError("Could not resolve authentication method")
    with caplog.at_level(logging.ERROR, logger="app.analyzer"):
        r = analyze_leaf(IMG, crop, client=FakeClient(error=err))
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True
    assert "เรียก Claude ไม่สำเร็จ" in r.note and "Could not resolve authentication" in r.note
    assert "อ่านไม่ได้" not in r.note
    assert any(rec.exc_info for rec in caplog.records)


def test_max_tokens_is_truncated_failure(crop):
    r = analyze_leaf(IMG, crop, client=FakeClient(answer("ปกติ", 0.9), stop_reason="max_tokens"))
    assert r.label is None and r.confidence == 0.0 and r.needs_review is True
    assert "ถูกตัด" in r.note


def test_max_tokens_request_is_8192(crop):
    c = FakeClient(answer("ปกติ", 0.9))
    analyze_leaf(IMG, crop, client=c)
    assert c.calls[0]["max_tokens"] == 8192


def test_unreadable_answer_is_logged(crop, caplog):
    with caplog.at_level(logging.ERROR, logger="app.analyzer"):
        r = analyze_leaf(IMG, crop, client=FakeClient("ใบดูปกติดี"))
    assert "อ่านไม่ได้" in r.note
    assert any(rec.exc_info for rec in caplog.records)


def test_crop_none_does_not_raise(crop):
    r = analyze_leaf(IMG, None, client=FakeClient(answer("ปกติ", 0.9)))
    assert r.label is None and r.needs_review is True and "อ่านไม่ได้" not in r.note


def test_records_model_that_answered(crop):
    r = analyze_leaf(IMG, crop, client=FakeClient(answer("ปกติ", 0.9), model="fallback-model"))
    assert r.model == "fallback-model"
