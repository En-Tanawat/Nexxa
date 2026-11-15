import os
from dataclasses import dataclass

# Must be set BEFORE `import cv2`: OpenCV reads it once and then refuses to decode huge images
# (a tiny PNG can claim 16000x16000 px = 256 MB decoded). setdefault so an operator can override.
MAX_SIDE = 4096  # OV2640 tops out at 1600x1200
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(MAX_SIDE * MAX_SIDE))

import cv2  # noqa: E402
import numpy as np

# shortcut: เกณฑ์เริ่มต้น ปรับด้วยภาพจริงจาก ESP32-CAM
BLUR_THRESHOLD = 60.0  # Laplacian variance ต่ำกว่านี้ = เบลอ
# shortcut: เกณฑ์เริ่มต้น ปรับด้วยภาพจริงจาก ESP32-CAM
DARK_THRESHOLD = 40.0  # ค่าเฉลี่ยภาพเทาต่ำกว่านี้ = มืดเกิน
# shortcut: เกณฑ์เริ่มต้น ปรับด้วยภาพจริงจาก ESP32-CAM
BRIGHT_THRESHOLD = 220.0  # ค่าเฉลี่ยภาพเทาสูงกว่านี้ = สว่างเกิน


class ImageDecodeError(Exception):
    """bytes ที่ได้รับไม่ใช่ภาพที่ถอดรหัสได้"""


@dataclass
class QualityResult:
    ok: bool
    reason: str | None
    blur_score: float
    brightness: float


def check_quality(image_bytes: bytes) -> QualityResult:
    # imdecode asserts on empty buffers, so treat empty as non-image
    try:
        img = (
            cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
            if image_bytes
            else None
        )
    except cv2.error as e:  # raised when the OPENCV_IO_MAX_IMAGE_PIXELS cap is exceeded
        raise ImageDecodeError("cannot decode image bytes") from e
    if img is None:
        raise ImageDecodeError("cannot decode image bytes")
    if max(img.shape) > MAX_SIDE:
        raise ImageDecodeError("image too large")
    brightness = float(img.mean())
    blur_score = float(cv2.Laplacian(img, cv2.CV_64F).var())
    # เช็กแสงก่อน: ภาพดำ/ขาวล้วนมี Laplacian variance ~0 จะถูกจัดเป็นเบลอผิด
    if brightness < DARK_THRESHOLD:
        reason = "too_dark"
    elif brightness > BRIGHT_THRESHOLD:
        reason = "too_bright"
    elif blur_score < BLUR_THRESHOLD:
        reason = "blurry"
    else:
        reason = None
    return QualityResult(reason is None, reason, blur_score, brightness)
