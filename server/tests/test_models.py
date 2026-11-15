import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import init_db
from app.models import Crop, Station
from app.seed import seed


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    with Session(engine) as s:
        seed(s)
        yield s


def count(s, model, *where):
    return s.scalar(select(func.count()).select_from(model).where(*where))


def test_seed_crop(session):
    crop = session.scalar(select(Crop).where(Crop.name == "กรีนคอส"))
    assert crop.labels == ["ปกติ", "ใบเหลือง", "จุดน้ำตาล"]
    assert "tipburn" in crop.prompt and "tipburn" in crop.notes


def test_seed_stations(session):
    assert count(session, Station, Station.kind == "station", Station.active.is_(True)) == 3
    assert count(session, Station, Station.kind == "home") == 1


def test_seed_idempotent(session):
    before = (count(session, Crop), count(session, Station))
    seed(session)
    assert (count(session, Crop), count(session, Station)) == before


def test_rfid_unique(session):
    uid = session.scalar(select(Station.rfid_uid))
    session.add(Station(rfid_uid=uid, name="dup", kind="station"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_reseed_after_uid_replacement(session):
    for i, st in enumerate(session.scalars(select(Station)).all()):
        st.rfid_uid = f"04A1B2C3D{i}"
    session.commit()
    seed(session)
    assert count(session, Station, Station.kind == "station", Station.active.is_(True)) == 3
    assert count(session, Station, Station.kind == "home") == 1
    assert count(session, Station, Station.rfid_uid.like("TODO-REPLACE%")) == 0
