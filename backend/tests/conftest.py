import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.importers.base import RetryPolicy
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


class FakeWeb:
    """Routes URLs (path prefix) to handler functions; records requests and sleeps."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def add(self, url_prefix: str, handler):
        self.routes[url_prefix] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                h = self.routes[prefix]
                return h(request) if callable(h) else h
        return httpx.Response(404, json={"message": "not found"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def retry(self) -> RetryPolicy:
        return RetryPolicy(sleep=self.sleeps.append)


_PG = {}


def _postgres_url() -> str:
    """CHESS_TEST_PG=1 runs the whole suite on an embedded PostgreSQL (needs pixeltable-pgserver)."""
    if "url" not in _PG:
        import tempfile

        import pixeltable_pgserver as pgserver
        _PG["srv"] = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        _PG["url"] = _PG["srv"].get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    return _PG["url"]


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    from app import limits
    limits.windows.hits.clear()
    yield


@pytest.fixture()
def database(tmp_path):
    if os.environ.get("CHESS_TEST_PG") == "1":
        from sqlalchemy import create_engine, text
        url = _postgres_url()
        with create_engine(url, isolation_level="AUTOCOMMIT").connect() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))
        db.init_db(url)
    else:
        db.init_db(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    yield db
    db.engine.dispose()


@pytest.fixture()
def web():
    return FakeWeb()


@pytest.fixture()
def client(database, web):
    app.state.http_client_factory = web.client
    app.state.retry_factory = web.retry
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def mixed_pgn() -> bytes:
    return (FIXTURES / "mixed_batch.pgn").read_bytes()
