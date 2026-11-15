import os
from dataclasses import dataclass
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("NEXXA_DATABASE_URL", f"sqlite:///{_SERVER_DIR / 'data' / 'nexxa.db'}")
    image_dir: Path = Path(os.getenv("NEXXA_IMAGE_DIR", _SERVER_DIR / "data" / "images"))
    vision_model: str = os.getenv("NEXXA_VISION_MODEL", "claude-opus-5-5")
    max_retakes: int = 2
    min_confidence: float = 0.6


(_SERVER_DIR / "data").mkdir(exist_ok=True)  # sqlite file lives here
settings = Settings()
