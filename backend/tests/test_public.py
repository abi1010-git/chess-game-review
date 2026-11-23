"""Phase 5: guest spaces, isolation, quotas, rate limits, sample data, deleting your data, health."""
import pytest
from fastapi.testclient import TestClient

from app import config, db, limits
from app.main import app
from app.models import Finding, Game, MoveEval, Profile, Study

PGN = (b'[White "Me"]\n[Black "Opp"]\n[Result "1-0"]\n[Date "2025.02.02"]\n[Site "x"]\n\n'
       b'1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0\n')


@pytest.fixture(autouse=True)
def guest_mode(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "guest")
    limits.windows.hits.clear()


def visitor(database, web):
    app.state.http_client_factory = web.client
    app.state.retry_factory = web.retry
    return TestClient(app, base_url="https://testserver")


def upload(c, pgn=PGN, name="Me"):
    r = c.post("/api/imports/pgn", files={"file": ("g.pgn", pgn)}, data={"player_names": name})
    assert r.status_code == 202, r.text
    return r.json()


def test_reading_without_a_cookie_creates_nothing_and_shows_empty_views(database, web):
    c = visitor(database, web)
    assert c.get("/api/dashboard").json()["coverage"]["games"] == 0
    assert c.get("/api/games").json()["total"] == 0
    assert c.get("/api/games/1").status_code == 404
    with db.SessionLocal() as s:
        assert s.query(Profile).filter_by(kind="guest").count() == 0


def test_first_write_creates_a_private_space_with_a_safe_cookie(database, web):
    c = visitor(database, web)
    r = c.post("/api/imports/pgn", files={"file": ("g.pgn", PGN)}, data={"player_names": "Me"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    with db.SessionLocal() as s:
        p = s.query(Profile).filter_by(kind="guest").one()
        assert p.token_hash and p.token_hash != c.cookies.get(config.COOKIE_NAME)  # only a hash is stored


def test_visitors_cannot_see_or_touch_each_others_data(database, web):
    a, b = visitor(database, web), visitor(database, web)
    upload(a)
    gid = a.get("/api/games").json()["items"][0]["id"]
    assert b.get("/api/games").json()["total"] == 0
    assert b.get(f"/api/games/{gid}").status_code == 404
    assert b.get(f"/api/games/{gid}/report").status_code == 404
    assert b.post("/api/analysis/batches", json={"game_ids": [gid], "mode": "manual"}).status_code in (404, 422)
    assert b.post(f"/api/games/{gid}/coach-review").status_code == 404
    assert b.post("/api/studies", json={"game_id": gid, "theme": None}).status_code == 404
    assert b.post("/api/analysis/preview", json={"game_ids": [gid]}).json()["found"] == 0
    assert b.get("/api/imports").json() == [] and b.get("/api/me/export").json()["games"] == []
    # b's own writes work and a still sees only its own
    upload(b, PGN.replace(b"Bb5", b"Bc4"))
    assert a.get("/api/games").json()["total"] == 1 and b.get("/api/games").json()["total"] == 1
    # a forged or unknown cookie is just "no space"
    forged = visitor(database, web)
    forged.cookies.set(config.COOKIE_NAME, "x" * 43)
    assert forged.get("/api/games").json()["total"] == 0


def test_sample_history_gives_a_full_private_workflow_without_the_engine(database, web):
    a, b = visitor(database, web), visitor(database, web)
    assert a.get("/api/me").json()["sample_available"]
    assert a.post("/api/me/sample").json()["games"] == 24
    assert a.post("/api/me/sample").status_code == 409            # only into an empty space
    d = a.get("/api/dashboard").json()
    assert d["coverage"]["games"] == 24 and d["coverage"]["analyzed"] == 24
    assert any(r["status"] == "recurring" for r in d["recurring"])
    last = a.get("/api/games", params={"limit": 1}).json()["items"][0]
    rep = a.get(f"/api/games/{last['id']}/report").json()["report"]
    assert rep["findings"] and rep["decisions"]
    review = a.post(f"/api/games/{last['id']}/coach-review", params={"mode": "rules"}).json()["review"]
    assert review["claims"]
    pick = a.get("/api/studies").json()["picks"][0]
    st = a.post("/api/studies", json={"game_id": pick["game_id"], "theme": pick["theme"]}).json()
    assert "answer" not in str(st)
    assert b.get("/api/dashboard").json()["coverage"]["games"] == 0   # b got nothing


def test_delete_my_data_removes_everything_and_the_space(database, web):
    a, other = visitor(database, web), visitor(database, web)
    a.post("/api/me/sample")
    upload(other)
    pick = a.get("/api/studies").json()["picks"][0]
    a.post("/api/studies", json={"game_id": pick["game_id"], "theme": None})
    assert a.delete("/api/me").json()["deleted"]["games"] == 24
    with db.SessionLocal() as s:
        mine = s.query(Game).count()
        assert mine == 1                                        # only the other visitor's game is left
        assert s.query(MoveEval).count() == 0 and s.query(Finding).count() == 0 and s.query(Study).count() == 0
        assert s.query(Profile).filter_by(kind="guest").count() == 1
    assert a.get("/api/games").json()["total"] == 0
    assert other.get("/api/games").json()["total"] == 1


def test_export_contains_the_visitors_own_games_and_notes(database, web):
    a = visitor(database, web)
    upload(a)
    ex = a.get("/api/me/export")
    assert "attachment" in ex.headers["content-disposition"]
    assert len(ex.json()["games"]) == 1 and "Bb5" in ex.json()["games"][0]["pgn"]


def test_idle_guest_spaces_are_purged_but_active_ones_are_kept(database, web):
    from datetime import timedelta
    from app import auth
    from app.models import utcnow
    a, b = visitor(database, web), visitor(database, web)
    a.post("/api/me/sample")
    upload(b)
    with db.SessionLocal() as s:
        old = s.query(Profile).filter_by(kind="guest").order_by(Profile.id).first()
        old.last_seen = utcnow() - timedelta(days=config.GUEST_RETENTION_DAYS + 1)
        s.commit()
        assert auth.purge_idle_guests(s) == 1
        assert s.query(Profile).filter_by(kind="guest").count() == 1 and s.query(MoveEval).count() == 0


def test_space_and_queue_quotas(database, web, monkeypatch):
    a = visitor(database, web)
    monkeypatch.setattr(config, "MAX_GAMES_PER_PROFILE", 2)
    pgn = b"\n".join(PGN.replace(b"Nf3", m) for m in (b"Nc3", b"h3", b"f4"))
    r = upload(a, pgn)
    run = a.get(f"/api/imports/{r['id']}").json()
    assert run["imported"] == 2 and any("space is full" in k["reason"] for k in run["skips"])
    assert a.post("/api/imports/pgn", files={"file": ("g.pgn", PGN)}, data={"player_names": "Me"}).status_code == 409
    ids = [g["id"] for g in a.get("/api/games").json()["items"]]
    monkeypatch.setattr(config, "MAX_QUEUED_JOBS_PER_PROFILE", 1)
    assert a.post("/api/analysis/batches", json={"game_ids": ids, "mode": "manual"}).status_code == 429
    monkeypatch.setattr(config, "MAX_QUEUED_JOBS_PER_PROFILE", 10)
    monkeypatch.setattr(config, "MAX_QUEUED_JOBS_TOTAL", 1)
    assert a.post("/api/analysis/batches", json={"game_ids": ids, "mode": "manual"}).status_code == 503
    monkeypatch.setattr(config, "MAX_QUEUED_JOBS_TOTAL", 10)
    assert a.post("/api/analysis/batches", json={"game_ids": ids, "mode": "manual"}).status_code == 201


def test_rate_limits_and_new_space_limits(database, web, monkeypatch):
    c = visitor(database, web)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 5)
    codes = [c.get("/api/games").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    r = c.get("/api/games")
    assert "Retry-After" in r.headers and "wait" in r.json()["detail"]
    assert c.get("/api/health").status_code == 200               # health checks are never limited
    limits.windows.hits.clear()
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 1000)
    monkeypatch.setattr(config, "RATE_LIMIT_IMPORTS_PER_HOUR", 2)
    assert [c.post("/api/imports/account/lichess", json={"username": "someone"}).status_code
            for _ in range(3)][2] == 429
    limits.windows.hits.clear()
    monkeypatch.setattr(config, "MAX_GUEST_PROFILES_PER_IP_PER_HOUR", 2)
    new = [visitor(database, web).post("/api/me/sample").status_code for _ in range(3)]
    assert new == [200, 200, 429]


def test_client_address_ignores_forged_forwarded_for_entries():
    class R:  # the proxy appends the real address on the right
        headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.9"}
        client = None
    assert limits.client_ip(R) == "203.0.113.9"


def test_health_ready_reports_worker_and_model_state(database, web):
    c = visitor(database, web)
    r = c.get("/api/health/ready")
    assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["worker"] is False
    from app.models import WorkerHeartbeat
    with db.SessionLocal() as s:
        s.add(WorkerHeartbeat(worker_id="w", engine_name="Stockfish 18"))
        s.commit()
    r = c.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["queued_jobs"] == 0 and "model_enabled" in r.json()


def test_unexpected_errors_give_a_reference_not_a_stack_trace(database, web, monkeypatch):
    from app import selection
    monkeypatch.setattr(selection, "facets", lambda *a, **k: 1 / 0)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/games/facets")
    assert r.status_code == 500 and "reference" in r.json()["detail"] and "Traceback" not in r.text


def test_public_coach_status_hides_provider_and_model_names(database, web, monkeypatch):
    from app.coach.llm import Provider, Router, set_router
    set_router(Router([Provider("gemini", "Gemini", "https://x.test/v1", "secret-model-name", "k", 10, 10, 1, 1)]))
    try:
        st = visitor(database, web).get("/api/coach/status").json()
    finally:
        set_router(None)
    assert st["enabled"] is True and "secret-model-name" not in str(st) and "gemini" not in str(st).lower()


def test_security_headers_are_set(database, web):
    r = visitor(database, web).get("/api/health", headers={"x-forwarded-proto": "https"})
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "max-age" in r.headers["strict-transport-security"]
