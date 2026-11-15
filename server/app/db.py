from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Base

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


def init_db(engine: Engine = engine) -> None:
    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
