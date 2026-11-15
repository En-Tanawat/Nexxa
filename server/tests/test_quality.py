import cv2
import numpy as np
import pytest

from app.quality import ImageDecodeError, check_quality


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _sharp() -> np.ndarray:
    # checkerboard 8px, gray 50/200 -> mean ~125 (mid brightness), strong edges
    y, x = np.indices((128, 128))
    gray = np.where(((x // 8) + (y // 8)) % 2 == 0, 50, 200).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def test_sharp_image_ok():
    r = check_quality(_png(_sharp()))
    assert r.ok is True
    assert r.reason is None
    assert r.blur_score > 60


def test_blurred_image_is_blurry():
    blurred = cv2.GaussianBlur(_sharp(), (0, 0), sigmaX=15)
    r = check_quality(_png(blurred))
    assert r.ok is False
    assert r.reason == "blurry"
    assert r.blur_score < 60


def test_black_image_too_dark():
    r = check_quality(_png(np.zeros((64, 64, 3), np.uint8)))
    assert (r.ok, r.reason) == (False, "too_dark")


def test_white_image_too_bright():
    r = check_quality(_png(np.full((64, 64, 3), 255, np.uint8)))
    assert (r.ok, r.reason) == (False, "too_bright")


@pytest.mark.parametrize("data", [b"not an image", b""])
def test_non_image_raises(data):
    with pytest.raises(ImageDecodeError):
        check_quality(data)
