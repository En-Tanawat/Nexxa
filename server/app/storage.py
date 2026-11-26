from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Capture, CaptureImage

DB_PREFIX = "db:"  # Capture.image_path for photos stored in capture_images


def save_image(session: Session, image_bytes: bytes, capture_id: int) -> str:
    """Store the photo in the database (bytes as received, format not trusted); returns the value for Capture.image_path."""
    session.add(CaptureImage(capture_id=capture_id, data=image_bytes))
    return f"{DB_PREFIX}{capture_id}"


def load_image(session: Session, capture: Capture) -> bytes | None:
    """The photo bytes, or None when gone. Older rows point at a file, which is read only from inside the image directory."""
    row = session.get(CaptureImage, capture.id)
    if row is not None:
        return row.data
    if not capture.image_path or capture.image_path.startswith(DB_PREFIX):
        return None
    path = Path(capture.image_path).resolve()
    if settings.image_dir.resolve() not in path.parents or not path.is_file():
        return None
    return path.read_bytes()
