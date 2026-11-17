from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from immo_agent.core.config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    # Naive UTC everywhere: SQLite drops tz info, so mixing aware/naive would break comparisons.
    return datetime.now(UTC).replace(tzinfo=None)


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def configure(url: str | None = None) -> None:
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(_engine, expire_on_commit=False)


def engine():
    if _engine is None:
        configure()
    return _engine


def session() -> Session:
    if _SessionLocal is None:
        configure()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = session()
    try:
        yield db
    finally:
        db.close()
