"""Schema and queue behaviour on a real PostgreSQL (skipped when none is available).

Set TEST_DATABASE_URL to a throwaway PostgreSQL database, or install `pixeltable-pgserver`
to get an embedded server for the run.
"""
import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, select

from app import db, jobs
from app.models import AnalysisJob, Game, Profile


def _server_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url, None
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        return None, None
    srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    return srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1), srv


@pytest.fixture(scope="module")
def pg():
    url, srv = _server_url()
    if not url:
        pytest.skip("no PostgreSQL available")
    db.init_db(url)
    yield url
    db.engine.dispose()
    if srv:
        srv.cleanup()


def test_migrations_create_every_table_and_are_repeatable(pg):
    db.migrate(pg)  # second run is a no-op
    names = set(inspect(create_engine(pg)).get_table_names())
    assert {"profiles", "games", "analysis_jobs", "analysis_runs", "move_evals", "findings",
            "coach_reviews", "studies", "alembic_version"} <= names
    with db.SessionLocal() as s:
        # explicit id=1 for the default profile must not collide with the next generated id
        p = Profile(name="second")
        s.add(p)
        s.commit()
        assert p.id > 1


def test_queue_claims_are_exclusive_on_postgres(pg):
    from datetime import datetime
    from tests.test_selection_and_jobs import make_game
    with db.SessionLocal() as s:
        g = make_game(s, datetime(2025, 3, 5))
        jobs.enqueue(s, db.DEFAULT_PROFILE_ID, [g.id], "manual")
    import threading
    got = []

    def claim(name):
        with db.SessionLocal() as s2:
            j = jobs.claim_next(s2, name)
            got.append(j.id if j else None)

    ts = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len([x for x in got if x]) == 1  # exactly one worker won the job
    with db.SessionLocal() as s:
        assert s.scalar(select(AnalysisJob.attempts).where(AnalysisJob.game_id == g.id)) == 1


def test_filter_and_ordering_queries_work_on_postgres(pg):
    from datetime import datetime
    from app import selection
    from tests.test_selection_and_jobs import make_game
    with db.SessionLocal() as s:
        make_game(s, None)
        make_game(s, datetime(2025, 5, 1))
        rows = s.scalars(selection.apply_filter(select(Game), selection.GameFilter(), 1)
                         .order_by(Game.played_at.desc().nulls_last())).all()
        assert rows[-1].played_at is None
