import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.db
from app.db import init_db


@pytest.fixture(autouse=True)
def _hermetic_runtime_config(monkeypatch):
    # runtime_config reads app.db.engine at call time; give every test its own empty database, never the real one
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(eng)
    monkeypatch.setattr(app.db, "engine", eng)
    yield
    eng.dispose()
