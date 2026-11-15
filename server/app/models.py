from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Crop(Base):
    __tablename__ = "crops"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    labels: Mapped[list[str]] = mapped_column(JSON)
    prompt: Mapped[str]
    notes: Mapped[str | None]


class Station(Base):
    __tablename__ = "stations"
    id: Mapped[int] = mapped_column(primary_key=True)
    rfid_uid: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str]
    description: Mapped[str | None]
    kind: Mapped[str] = mapped_column(String, default="station")  # "station" | "home"
    map_x: Mapped[float | None]
    map_y: Mapped[float | None]
    route_order: Mapped[int | None]
    active: Mapped[bool] = mapped_column(default=True)
    crop_id: Mapped[int | None] = mapped_column(ForeignKey("crops.id"))


class Round(Base):
    __tablename__ = "rounds"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(default=_now)
    status: Mapped[str] = mapped_column(String, default="running")


class Capture(Base):
    __tablename__ = "captures"
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    camera_angle: Mapped[str | None]
    attempt: Mapped[int] = mapped_column(default=1)
    image_path: Mapped[str]
    taken_at: Mapped[datetime] = mapped_column(default=_now)
    quality_ok: Mapped[bool | None]
    quality_reason: Mapped[str | None]
    crop_id: Mapped[int | None] = mapped_column(ForeignKey("crops.id"))


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    capture_id: Mapped[int] = mapped_column(ForeignKey("captures.id"))
    label: Mapped[str | None]  # None for refusals / invalid answers
    confidence: Mapped[float | None]
    note: Mapped[str | None]
    model: Mapped[str | None]
    needs_review: Mapped[bool] = mapped_column(default=False)
