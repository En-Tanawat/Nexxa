import base64
import json
from dataclasses import dataclass

import anthropic

from app.config import settings
from app.models import Crop


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


def _fail(note: str) -> AnalysisResult:
    return AnalysisResult(None, 0.0, note, True, settings.vision_model)


def analyze_leaf(image_bytes: bytes, crop: Crop, client=None) -> AnalysisResult:
    media_type = "image/png" if image_bytes.startswith(b"\x89PNG") else "image/jpeg"
    try:
        client = client or anthropic.Anthropic()
        # fallbacks live in the beta namespace; they re-run a safety-declined request server-side
        resp = client.beta.messages.create(
            model=settings.vision_model,
            max_tokens=4096,  # adaptive thinking counts toward this cap
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": _schema(crop)}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode()}},
                    {"type": "text", "text": build_prompt(crop)},
                ],
            }],
        )
        if resp.stop_reason == "refusal":  # check before touching content
            return _fail("Claude ปฏิเสธการวิเคราะห์ภาพนี้")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        label, confidence = data["label"], float(data["confidence"])
        note = str(data.get("note", ""))
    except anthropic.APIError as e:
        return _fail(f"เรียก Claude ไม่สำเร็จ: {type(e).__name__}")
    except (ValueError, KeyError, TypeError, AttributeError):
        return _fail("คำตอบของ Claude อ่านไม่ได้")
    if label not in crop.labels or not 0 <= confidence <= 1:
        return _fail(f"คำตอบนอกรูปแบบ: {label!r}")
    return AnalysisResult(label, confidence, note, confidence < settings.min_confidence, settings.vision_model)
