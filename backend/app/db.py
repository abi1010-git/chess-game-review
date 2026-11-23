"""Database engine and session setup. SQLite locally, PostgreSQL via DATABASE_URL."""
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy import create_engine, event, select, text
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

    if is_sqlite and not config.MIGRATE_SQLITE:
        Base.metadata.create_all(engine)  # local development: no migration step to remember
        _sqlite_add_missing_columns()
    else:
        migrate(url)  # PostgreSQL (and SQLite with MIGRATE_SQLITE=1): versioned migrations
    with SessionLocal() as s:
        if s.scalar(select(models.Profile).where(models.Profile.id == DEFAULT_PROFILE_ID)) is None:
            s.add(models.Profile(id=DEFAULT_PROFILE_ID, name="Local player"))
            try:
                s.commit()
            except IntegrityError:  # another process (API or worker) created it first
                s.rollback()
        if engine.dialect.name == "postgresql":
            # The default profile was inserted with an explicit id; move the sequence past it.
            s.execute(text("SELECT setval(pg_get_serial_sequence('profiles', 'id'), "
                           "GREATEST((SELECT MAX(id) FROM profiles), 1))"))
            s.commit()
    return engine


def _sqlite_add_missing_columns() -> None:
    """Older local databases were made by create_all; add columns that were added since."""
    from sqlalchemy import inspect
    insp = inspect(engine)
    with engine.begin() as c:
        for table in Base.metadata.sorted_tables:
            have = {col["name"] for col in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(engine.dialect)
                    default = f" DEFAULT '{col.server_default.arg}'" if col.server_default is not None else ""
                    c.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{default}'))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_profiles_token_hash ON profiles (token_hash)"))


def migrate(url: str | None = None) -> None:
    """Bring the schema to the latest Alembic revision (`alembic upgrade head`)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "migrations"))
    cfg.attributes["url"] = url or config.DATABASE_URL
    command.upgrade(cfg, "head")


def get_session():
    with SessionLocal() as s:
        yield s


def get_profile_id() -> int:
    """Single local profile until authentication arrives (phase 5)."""
    return DEFAULT_PROFILE_ID
