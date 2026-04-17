from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.core.config import DATABASE_URL

_engine_kwargs: dict = {"echo": False}
if DATABASE_URL.startswith("postgresql"):
    _engine_kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=10, pool_timeout=30)
engine = create_engine(DATABASE_URL, **_engine_kwargs)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
