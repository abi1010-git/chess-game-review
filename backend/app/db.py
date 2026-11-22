"""Database engine and session setup. SQLite locally, PostgreSQL via DATABASE_URL."""
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


engine: Engine | None = None
SessionLocal = sessionmaker(expire_on_commit=False)

DEFAULT_PROFILE_ID = 1


def init_db(url: str | None = None) -> Engine:
    """Create the engine, tables and the default profile. Safe to call repeatedly."""
    global engine
    url = url or config.DATABASE_URL
    kwargs: dict = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        path = url.split(":///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    if engine is not None:
        engine.dispose()
    engine = create_engine(url, **kwargs)

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    SessionLocal.configure(bind=engine)

    from . import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        if s.scalar(select(models.Profile).where(models.Profile.id == DEFAULT_PROFILE_ID)) is None:
            s.add(models.Profile(id=DEFAULT_PROFILE_ID, name="Local player"))
            s.commit()
    return engine


def get_session():
    with SessionLocal() as s:
        yield s


def get_profile_id() -> int:
    """Single local profile until authentication arrives (phase 5)."""
    return DEFAULT_PROFILE_ID
