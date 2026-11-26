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
    __table_args__ = (
        Index("ix_stations_route_order", "route_order", "id"),
        Index("ix_stations_active", "active"),
    )
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
    # existing DBs: CREATE INDEX ix_analyses_capture ON analyses(capture_id)
    __table_args__ = (Index("ix_analyses_capture", "capture_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    capture_id: Mapped[int] = mapped_column(ForeignKey("captures.id"))
    label: Mapped[str | None]  # None for refusals / invalid answers
    confidence: Mapped[float | None]
    note: Mapped[str | None]
    model: Mapped[str | None]
    needs_review: Mapped[bool] = mapped_column(default=False)


class StationAnalysis(Base):
    """One vision call over all of a station's photos in a round: plants seen and plants per disease class.
    Counts are NULL when the call failed or its answer was invalid (needs_review is then set)."""
    __tablename__ = "station_analyses"
    __table_args__ = (
        Index("ix_station_analyses_round_station", "round_id", "station_id"),
        Index("ix_station_analyses_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    n_photos: Mapped[int]
    n_total: Mapped[int | None]
    n_yellow: Mapped[int | None]
    n_brown: Mapped[int | None]
    confidence: Mapped[float | None]
    note: Mapped[str | None]
    model: Mapped[str | None]
    needs_review: Mapped[bool] = mapped_column(default=False)
    # the photos this call was given: [{"capture_id", "angle", "path"}], path relative to the image directory
    photos: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        Index("ix_batches_planted_on", "planted_on", "id"),
        Index("ix_batches_harvested_on", "harvested_on"),
        Index("ix_batches_station_crop", "station_id", "crop_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    crop_id: Mapped[int] = mapped_column(ForeignKey("crops.id"))
    planted_on: Mapped[date]
    harvested_on: Mapped[date | None]
    notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)


class DailyStat(Base):
    # batch_id NULL rows are NOT unique-protected (SQL NULL != NULL); upsert code must dedupe itself.
    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint("day", "station_id", "batch_id"),
        Index("ix_daily_stats_day_station", "day", "station_id"),
        Index("ix_daily_stats_batch_id", "batch_id"),
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


class Summary(Base):
    # (round_id, station_id) is NOT unique-protected for station_id NULL (whole round); summaries.py upserts itself.
    __tablename__ = "summaries"
    __table_args__ = (Index("ix_summaries_round_station", "round_id", "station_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"))
    scope: Mapped[str] = mapped_column(String)  # "station" | "round"
    text: Mapped[str]
    facts: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String)  # "template" | "ai"
    model: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now)
    notified_at: Mapped[datetime | None]  # set once when the Telegram notification is claimed (telegram.py)


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(String, default="admin")
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
    __table_args__ = (
        Index("ix_device_keys_revoked_id", "revoked_at", "id"),
    )
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
    __table_args__ = (
        Index("ix_audit_log_at", "at", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=_now)
    actor: Mapped[str]
    action: Mapped[str]
    target: Mapped[str]
    detail: Mapped[str | None]


class CaptureImage(Base):
    """The photo bytes, kept in the database so they survive host restarts (Render's disk is wiped on every deploy).
    A separate table so listing captures never loads the images."""
    __tablename__ = "capture_images"
    capture_id: Mapped[int] = mapped_column(ForeignKey("captures.id"), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
