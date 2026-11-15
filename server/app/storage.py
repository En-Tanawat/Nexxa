from app.config import settings


def save_image(image_bytes: bytes, round_id: int, station_id: int, attempt: int) -> str:
    folder = settings.image_dir / str(round_id) / str(station_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{attempt}.img"  # no extension: bytes are stored as received, format is not trusted
    path.write_bytes(image_bytes)
    return str(path)
