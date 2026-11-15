import base64
import json
import logging
from dataclasses import dataclass

import anthropic

from app.config import settings
from app.models import Crop

log = logging.getLogger(__name__)


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
    return AnalysisResult(None, 0.0, note, True, model or settings.vision_model)


def analyze_leaf(image_bytes: bytes, crop: Crop, client=None) -> AnalysisResult:
    media_type = "image/png" if image_bytes.startswith(b"\x89PNG") else "image/jpeg"
    try:
        prompt, schema = build_prompt(crop), _schema(crop)
    except AttributeError:  # crop is None / incomplete
        log.exception("analyze_leaf: no usable crop")
        return _fail("ไม่มีข้อมูลพืชสำหรับวิเคราะห์")
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
        data = json.loads(text)
        label, confidence = data["label"], float(data["confidence"])
        note = str(data.get("note", ""))
    except (ValueError, KeyError, TypeError, AttributeError):
        log.exception("Claude answer could not be parsed")
        return _fail("คำตอบของ Claude อ่านไม่ได้", model)
    if label not in crop.labels or not 0 <= confidence <= 1:
        return _fail(f"คำตอบนอกรูปแบบ: {label!r}", model)
    return AnalysisResult(label, confidence, note, confidence < settings.min_confidence, model)
