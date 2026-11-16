import logging
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.migrate import ensure_columns
from app.models import Base

def connect_args_for(url: str) -> dict:
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


engine = create_engine(settings.database_url, connect_args=connect_args_for(settings.database_url))


def init_db(engine: Engine = engine) -> None:
    Base.metadata.create_all(engine)
    try:  # add columns models gained since the tables were created
        ensure_columns(engine)
    except Exception as e:
        logging.getLogger(__name__).warning("schema sync failed (%s)", type(e).__name__)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
