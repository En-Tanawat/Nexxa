import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.db
from app.db import init_db


@pytest.fixture(scope="session")
def _empty_engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(eng)
    return eng


@pytest.fixture(autouse=True)
def _hermetic_runtime_config(monkeypatch, _empty_engine):
    # runtime_config reads app.db.engine at call time; never let a test see the developer's real database.
    monkeypatch.setattr(app.db, "engine", _empty_engine)
