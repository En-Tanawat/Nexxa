from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import init_db
from app.models import (
    AdminUser, AppSetting, AuditLog, Batch, Crop, DailyStat, DeviceKey, ProviderKey, Station,
)


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(e)
    return e


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        s.add_all([Crop(name="c", labels=["a"], prompt="p"), Station(rfid_uid="U1", name="s")])
        s.commit()
        yield s


def make_batch(s, **kw):
    b = Batch(station_id=1, crop_id=1, planted_on=date(2026, 1, 1), **kw)
    s.add(b)
    s.flush()
    return b


def test_create_all_tables(session):
    b = make_batch(session)
    session.add_all([
        DailyStat(day=date(2026, 1, 2), station_id=1, batch_id=b.id, n_captures=3, n_quality_ok=2,
                  n_normal=1, n_yellow=1, n_brown=0, n_review=1, mean_confidence=0.8),
        AdminUser(username="admin", password_hash="h"),
        ProviderKey(provider="anthropic", label="main", secret_enc=b"\x00\x01", last4="abcd", active=True),
        DeviceKey(label="esp", key_hash="hh", last4="1234", active=True),
        AppSetting(key="k", value="v"),
        AuditLog(actor="admin", action="login", target="-"),
    ])
    session.commit()
    assert b.created_at.year >= 2026 and b.harvested_on is None and b.notes is None
    assert session.get(AppSetting, "k").value == "v"
    assert session.get(AdminUser, 1).last_login_at is None
    assert isinstance(session.get(AuditLog, 1).at, datetime) and session.get(AuditLog, 1).detail is None
    assert session.get(ProviderKey, 1).secret_enc == b"\x00\x01"
    assert session.get(DeviceKey, 1).revoked_at is None


def test_batch_harvested_on_nullable_and_settable(session):
    b = make_batch(session, harvested_on=date(2026, 2, 1))
    assert b.harvested_on == date(2026, 2, 1)


def stat(batch_id):
    return DailyStat(day=date(2026, 1, 2), station_id=1, batch_id=batch_id, n_captures=0, n_quality_ok=0,
                     n_normal=0, n_yellow=0, n_brown=0, n_review=0)


def test_daily_stat_unique(session):
    b = make_batch(session)
    session.add(stat(b.id))
    session.commit()
    session.add(stat(b.id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_daily_stat_null_batch_not_unique(session):
    # Known limitation: NULL != NULL in SQL, so the unique constraint does not protect
    # batch_id=None rows. The rollup must dedupe with an explicit lookup before inserting.
    session.add_all([stat(None), stat(None)])
    session.commit()


def test_admin_username_unique(session):
    session.add(AdminUser(username="a", password_hash="1"))
    session.commit()
    session.add(AdminUser(username="a", password_hash="2"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_device_key_hash_unique(session):
    session.add(DeviceKey(label="a", key_hash="x", last4="1", active=True))
    session.commit()
    session.add(DeviceKey(label="b", key_hash="x", last4="2", active=True))
    with pytest.raises(IntegrityError):
        session.commit()


def test_indexes_present(engine):
    insp = inspect(engine)
    cols = {tuple(i["column_names"]) for t in ("captures", "daily_stats") for i in insp.get_indexes(t)}
    assert ("station_id", "taken_at") in cols and ("day", "station_id") in cols
