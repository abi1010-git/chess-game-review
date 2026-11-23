"""The Alembic migrations must produce the same schema as the models."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app import db
from app.models import Profile


def test_migrations_match_models_and_are_repeatable(tmp_path):
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    db.migrate(url)
    db.migrate(url)  # already at head
    eng = create_engine(url)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), db.Base.metadata)
    eng.dispose()
    assert diff == [], f"models and migrations disagree: {diff}"


def test_default_profile_is_created_through_migrations(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MIGRATE_SQLITE", True)
    db.init_db(f"sqlite:///{(tmp_path / 'm2.db').as_posix()}")
    with db.SessionLocal() as s:
        assert s.get(Profile, 1).name == "Local player"
    db.engine.dispose()
