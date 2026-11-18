import os
from dataclasses import dataclass
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent


def normalize_db_url(url: str) -> str:
    # SQLAlchemy 2 rejects "postgres://"; hosts hand it out. Use the psycopg 3 driver for both spellings.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def database_url_from_env() -> str:
    url = os.getenv("NEXXA_DATABASE_URL") or os.getenv("DATABASE_URL") or f"sqlite:///{_SERVER_DIR / 'data' / 'nexxa.db'}"
    return normalize_db_url(url)


@dataclass(frozen=True)
class Settings:
    database_url: str = database_url_from_env()
    image_dir: Path = Path(os.getenv("NEXXA_IMAGE_DIR", _SERVER_DIR / "data" / "images"))
    vision_model: str = os.getenv("NEXXA_VISION_MODEL", "claude-opus-5-5")
    max_retakes: int = 2
    min_confidence: float = 0.6

    @property
    def photos_per_station(self) -> int:
        """Group photos the robot takes at each station (the summary notes a station with fewer). Read per call."""
        try:
            return max(1, min(10, int(os.getenv("NEXXA_PHOTOS_PER_STATION", "3"))))
        except ValueError:
            return 3

    @property
    def vision_provider(self) -> str:
        return (os.getenv("NEXXA_VISION_PROVIDER") or "").strip().lower() or "anthropic"  # read per call

    @property
    def vision_base_url(self) -> str | None:
        return os.getenv("NEXXA_VISION_BASE_URL") or None  # overrides any provider's base url

    @property
    def device_key(self) -> str:
        return os.getenv("NEXXA_DEVICE_KEY", "")  # read per request; empty means every upload is rejected


(_SERVER_DIR / "data").mkdir(exist_ok=True)  # sqlite file lives here
settings = Settings()
