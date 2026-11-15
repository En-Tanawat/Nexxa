import base64
import json
import logging
import os
from dataclasses import dataclass

import anthropic
import openai

from app.config import settings
from app.models import Crop
from app.providers import UnknownProviderError, get_provider

log = logging.getLogger(__name__)
PNG_MAGIC = bytes.fromhex("89504e47")


@dataclass
class AnalysisResult:
    label: str | None
    confidence: float
    note: str
    needs_review: bool
    model: str


def build_prompt(crop: Crop) -> str:
    return (
        f"ผัก: {crop.name}\n{crop.prompt}\n"
        f"จัดประเภทใบในภาพเป็นหนึ่งใน: {', '.join(crop.labels)}\n"
        f"หมายเหตุ: {crop.notes or '-'}\n"
        "ตอบเป็น JSON: label, confidence (0-1), note (สั้น ๆ)"
    )


def _schema(crop: Crop) -> dict:
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(crop.labels)},
            "confidence": {"type": "number"},  # 0..1 is checked in code; schema mode rejects min/max
            "note": {"type": "string"},
        },
        "required": ["label", "confidence", "note"],
        "additionalProperties": False,
    }


def _fail(note: str, model: str | None = None) -> AnalysisResult:
    return AnalysisResult(None, 0.0, note, True, settings.vision_model if model is None else model)


def _first_json_object(text: str) -> dict:
    """First JSON object in a reply, tolerant of ```json fences and leading prose."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON object in answer")


def _finalize(text: str, crop: Crop, model: str, who: str = "Claude", parse=json.loads) -> AnalysisResult:
    """Parse + validate a model's text answer; shared by every provider path."""
    try:
        data = parse(text)
        label, confidence = data["label"], float(data["confidence"])
        note = str(data.get("note", ""))
    except (ValueError, KeyError, TypeError, AttributeError):
        log.exception("%s answer could not be parsed", who)
        return _fail(f"คำตอบของ {who} อ่านไม่ได้", model)
    if label not in crop.labels or not 0 <= confidence <= 1:
        return _fail(f"คำตอบนอกรูปแบบ: {label!r}", model)
    return AnalysisResult(label, confidence, note, confidence < settings.min_confidence, model)


def _analyze_anthropic(image_bytes: bytes, media_type: str, crop: Crop, prompt: str, client) -> AnalysisResult:
    schema = _schema(crop)
    try:
        client = client or anthropic.Anthropic(timeout=60)
        # fallbacks live in the beta namespace; they re-run a safety-declined request server-side
        resp = client.beta.messages.create(
            model=settings.vision_model,
            max_tokens=8192,  # adaptive thinking counts toward this cap
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode()}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except (anthropic.AnthropicError, TypeError) as e:  # TypeError: SDK raises it for a missing API key
        log.exception("Claude API call failed")
        return _fail(f"เรียก Claude ไม่สำเร็จ: {e}")
    model = getattr(resp, "model", None) or settings.vision_model  # may differ after a server-side fallback
    if resp.stop_reason == "refusal":  # check before touching content
        return _fail("Claude ปฏิเสธการวิเคราะห์ภาพนี้", model)
    if resp.stop_reason == "max_tokens":
        log.warning("Claude answer truncated at max_tokens")
        return _fail("คำตอบของ Claude ถูกตัดเพราะยาวเกินกำหนด", model)
    try:
        text = next((b.text for b in resp.content if b.type == "text"), "")
    except (TypeError, AttributeError):
        log.exception("Claude answer could not be parsed")
        return _fail("คำตอบของ Claude อ่านไม่ได้", model)
    return _finalize(text, crop, model)


def _analyze_openai_compat(image_bytes, media_type, crop, prompt, client, provider) -> AnalysisResult:
    # non-anthropic providers have no default model: guessing a name would silently call the wrong thing
    model = os.getenv("NEXXA_VISION_MODEL")
    if not model:
        return _fail(f"ยังไม่ได้ตั้งค่า NEXXA_VISION_MODEL สำหรับผู้ให้บริการ {provider.name}", provider.name)
    instruction = ('\nตอบเป็น JSON object เดียวเท่านั้น รูปแบบ {"label": ..., "confidence": 0-1, "note": ...} '
                   "ห้ามมีข้อความอื่น")
    data_uri = f"data:{media_type};base64,{base64.standard_b64encode(image_bytes).decode()}"
    try:
        if client is None:
            key = os.getenv(provider.key_env)
            if not key:
                return _fail(f"ยังไม่ได้ตั้งค่า {provider.key_env} สำหรับผู้ให้บริการ {provider.name}", model)
            client = openai.OpenAI(api_key=key, base_url=settings.vision_base_url or provider.base_url, timeout=60)
        # no temperature / response_format: support varies by provider
        resp = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            timeout=60,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt + instruction},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]}],
        )
    except (openai.OpenAIError, TypeError) as e:
        log.exception("%s API call failed", provider.name)
        return _fail(f"เรียก {provider.name} ไม่สำเร็จ: {e}", model)
    model = getattr(resp, "model", None) or model
    try:
        choice = resp.choices[0]
        text, finish = choice.message.content, choice.finish_reason
    except (IndexError, TypeError, AttributeError):
        log.exception("%s answer had no choices", provider.name)
        return _fail(f"คำตอบของ {provider.name} อ่านไม่ได้", model)
    if finish == "length":
        log.warning("%s answer truncated at max_tokens", provider.name)
        return _fail(f"คำตอบของ {provider.name} ถูกตัดเพราะยาวเกินกำหนด", model)
    if finish == "content_filter":
        return _fail(f"{provider.name} บล็อกการวิเคราะห์ภาพนี้", model)
    return _finalize(text, crop, model, provider.name, _first_json_object)


def analyze_leaf(image_bytes: bytes, crop: Crop, client=None, provider: str | None = None) -> AnalysisResult:
    media_type = "image/png" if image_bytes.startswith(PNG_MAGIC) else "image/jpeg"
    try:
        prompt = build_prompt(crop)
    except AttributeError:  # crop is None / incomplete
        log.exception("analyze_leaf: no usable crop")
        return _fail("ไม่มีข้อมูลพืชสำหรับวิเคราะห์")
    try:
        prov = get_provider(provider or settings.vision_provider)
    except UnknownProviderError as e:
        log.exception("analyze_leaf: bad provider")
        return _fail(str(e))
    if prov.kind == "anthropic":
        return _analyze_anthropic(image_bytes, media_type, crop, prompt, client)
    return _analyze_openai_compat(image_bytes, media_type, crop, prompt, client, prov)
