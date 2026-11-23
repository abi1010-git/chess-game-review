"""Alembic environment. The URL is DATABASE_URL (or passed in by app.db.migrate)."""
from alembic import context
from sqlalchemy import create_engine, text

from app import config, models  # noqa: F401  (registers tables on Base.metadata)
from app.db import Base

target_metadata = Base.metadata
LOCK_ID = 7_352_001  # PostgreSQL advisory lock: API and worker may both start at once


def run() -> None:
    url = context.config.attributes.get("url") or config.DATABASE_URL
    connectable = create_engine(url)
    with connectable.connect() as conn:
        is_pg = conn.dialect.name == "postgresql"
        if is_pg:
            conn.execute(text("SELECT pg_advisory_lock(:i)"), {"i": LOCK_ID})
        try:
            context.configure(connection=conn, target_metadata=target_metadata,
                              compare_type=True, render_as_batch=conn.dialect.name == "sqlite")
            with context.begin_transaction():
                context.run_migrations()
            conn.commit()
        finally:
            if is_pg:
                conn.execute(text("SELECT pg_advisory_unlock(:i)"), {"i": LOCK_ID})
                conn.commit()
    connectable.dispose()


run()
