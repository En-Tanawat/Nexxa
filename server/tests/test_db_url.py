import pytest

from app.config import normalize_db_url
from app.db import connect_args_for


@pytest.mark.parametrize("raw,want", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_normalize_db_url(raw, want):
    assert normalize_db_url(raw) == want


def test_connect_args_only_for_sqlite():
    assert connect_args_for("sqlite:///x.db") == {"check_same_thread": False}
    assert connect_args_for("postgresql+psycopg://u:p@h/d") == {}


def test_database_url_env_precedence(monkeypatch):
    from app.config import database_url_from_env
    monkeypatch.delenv("NEXXA_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://h/d")
    assert database_url_from_env() == "postgresql+psycopg://h/d"
    monkeypatch.setenv("NEXXA_DATABASE_URL", "sqlite:///own.db")
    assert database_url_from_env() == "sqlite:///own.db"
