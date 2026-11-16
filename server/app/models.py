from datetime import date, datetime, timezone

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String, UniqueConstraint
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
    status: Mapped[str] = mapped_column(String, default="open")


class Capture(Base):
    __tablename__ = "captures"
    __table_args__ = (Index("ix_captures_station_taken", "station_id", "taken_at"),)
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


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    crop_id: Mapped[int] = mapped_column(ForeignKey("crops.id"))
    planted_on: Mapped[date]
    harvested_on: Mapped[date | None]
    notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint("day", "station_id", "batch_id"),
        Index("ix_daily_stats_day_station", "day", "station_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date]
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    n_captures: Mapped[int]
    n_quality_ok: Mapped[int]
    n_normal: Mapped[int]
    n_yellow: Mapped[int]
    n_brown: Mapped[int]
    n_review: Mapped[int]
    mean_confidence: Mapped[float | None]


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=_now)
    last_login_at: Mapped[datetime | None]


class ProviderKey(Base):
    __tablename__ = "provider_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str]
    label: Mapped[str]
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    last4: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class DeviceKey(Base):
    __tablename__ = "device_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]
    key_hash: Mapped[str] = mapped_column(String, unique=True)
    last4: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    revoked_at: Mapped[datetime | None]


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str]


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=_now)
    actor: Mapped[str]
    action: Mapped[str]
    target: Mapped[str]
    detail: Mapped[str | None]
