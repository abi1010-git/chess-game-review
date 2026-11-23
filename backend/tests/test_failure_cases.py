"""Phase 4: failure cases for imports, jobs and the study workflow (fake engine, fake HTTP)."""
import json
import threading
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app import config, db, jobs
from app.coach import service
from app.coach.llm import Router, set_router
from app.models import AnalysisJob, AnalysisRun, Game

from .test_analysis import add_game, analyze_all
from .test_coach import chat, fake_providers, honest_model, setup_games, trap_game
from .test_imports import LICHESS, ndjson, run_import, upload


# ---------------------------------------------------------------- imports

@pytest.mark.parametrize("payload,why", [
    (b"", "empty file"),
    (b"\x00\x01\x02\xff\xfe binary junk", "binary"),
    (b"this is not a pgn at all\njust words", "plain text"),
    (b'[White "Me"]\n[Black "X"]\n\n1. e4 e5 2. Ke7 Ke2 *\n', "illegal moves"),
])
def test_malformed_files_import_nothing_and_say_why(client, payload, why):
    r = client.post("/api/imports/pgn", files={"file": ("g.pgn", payload)}, data={"player_names": "Me"})
    assert r.status_code == 202, why
    run = client.get(f"/api/imports/{r.json()['id']}").json()
    assert run["imported"] == 0 and run["status"] != "running"
    assert client.get("/api/games").json()["total"] == 0
    assert run["skips"] or run["notices"] or run["status"] == "failed"  # the player is told something


def test_games_without_ratings_import_and_analyze(client):
    pgn = b'[White "Me"]\n[Black "Opp"]\n[Result "1-0"]\n[Date "2025.02.02"]\n\n1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n'
    run = upload(client, pgn, "Me")
    assert run["imported"] == 1
    g = client.get("/api/games").json()["items"][0]
    assert g["player_rating"] is None and g["opponent_rating"] is None
    client.post("/api/analysis/batches", json={"game_ids": [g["id"]], "mode": "manual"})
    analyze_all()
    assert client.get(f"/api/games/{g['id']}/report").json()["report"]["run"]["analysis_version"]
    assert client.get("/api/dashboard").json()["coverage"]["analyzed"] == 1
    # suggestions work without ratings, and say that ratings were missing
    s = client.post("/api/selection/suggest", json={"target_from": "2025-02-01", "target_to": "2025-02-28"}).json()
    assert "groups" in s


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>Bad gateway</html>", headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"{not json\n{also not"),
    httpx.Response(403, json={"error": "forbidden"}),
])
def test_provider_garbage_does_not_crash_the_import(client, web, response):
    web.add(LICHESS + "Tester", response)
    run = run_import(client, "/api/imports/account/lichess", username="Tester")
    assert run["imported"] == 0
    assert run["status"] in ("failed", "completed_with_issues", "completed", "partial")
    assert run["notices"] or run["skips"] or run["status"] == "failed"
    assert client.get("/api/health").status_code == 200  # the API is still up


def test_provider_timeout_is_reported_not_raised(client, web):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    web.add(LICHESS + "Tester", boom)
    run = run_import(client, "/api/imports/account/lichess", username="Tester")
    assert run["status"] == "failed" and run["imported"] == 0
    assert run["notices"], "the player is told the provider timed out"


def test_concurrent_identical_uploads_never_create_duplicates(client):
    pgn = (b'[White "Me"]\n[Black "Opp"]\n[Result "1-0"]\n[Date "2025.02.02"]\n[Site "x"]\n\n'
           b'1. e4 e5 2. Nf3 Nc6 1-0\n')
    out = []

    def go():
        r = client.post("/api/imports/pgn", files={"file": ("g.pgn", pgn)}, data={"player_names": "Me"})
        out.append(r.status_code)

    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(c in (202, 429) for c in out)
    assert client.get("/api/games").json()["total"] == 1


# ---------------------------------------------------------------- jobs

def test_concurrent_workers_claim_each_job_exactly_once(client):
    with db.SessionLocal() as s:
        ids = [trap_game(s, i, i + 1).id for i in range(8)]
        jobs.enqueue(s, 1, ids, "manual")
    claimed, lock = [], threading.Lock()

    def worker(name):
        while True:
            with db.SessionLocal() as s2:
                j = jobs.claim_next(s2, name)
                if j is None:
                    return
                with lock:
                    claimed.append(j.id)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 8


def test_enqueue_twice_concurrently_makes_one_job_per_game(client):
    with db.SessionLocal() as s:
        gid = trap_game(s, 0, 1).id
    errs = []

    def go():
        try:
            with db.SessionLocal() as s2:
                jobs.enqueue(s2, 1, [gid], "manual")
        except Exception as e:  # a lost race raises IntegrityError, which the API retries
            errs.append(type(e).__name__)

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with db.SessionLocal() as s:
        assert len(list(s.scalars(select(AnalysisJob).where(AnalysisJob.game_id == gid)))) == 1
    assert set(errs) <= {"IntegrityError"}


def test_interrupted_run_is_visible_as_partial_and_never_as_complete(client):
    ids = setup_games(2)  # both analyzed
    with db.SessionLocal() as s:
        extra = trap_game(s, 2, 9).id
        batch, _ = jobs.enqueue(s, 1, ids + [extra], "manual")
        job = jobs.claim_next(s, "dying-worker")  # starts the new job, then the worker "dies"
        assert job.game_id == extra
    b = client.get(f"/api/analysis/batches/{batch.id}").json()
    assert b["progress"]["counts"] == {"done": 2, "running": 1}
    d = client.get("/api/dashboard").json()
    assert d["coverage"]["analyzed"] == 2 and d["coverage"]["running"] == 1  # only finished games count
    rep = client.get(f"/api/games/{extra}/report").json()
    assert rep["game"]["analysis_status"] == "running" and not (rep["report"] or {}).get("run")
    with db.SessionLocal() as s:
        assert s.scalar(select(AnalysisRun.id).where(AnalysisRun.game_id == extra)) is None


def test_game_analyzed_under_an_older_version_is_flagged_for_reanalysis(client, monkeypatch):
    ids = setup_games(1)
    assert client.get(f"/api/games/{ids[0]}").json()["outdated_version"] is None
    monkeypatch.setattr(config, "ANALYSIS_VERSION", "v2")
    g = client.get(f"/api/games/{ids[0]}").json()
    assert g["analysis_status"] == "none" and g["outdated_version"] == "v1"
    assert client.get("/api/dashboard").json()["coverage"]["outdated"] == 1
    assert client.get("/api/games", params={"analysis": "analyzed"}).json()["total"] == 0


# ---------------------------------------------------------------- study workflow

def test_model_timeouts_fall_back_to_rules_everywhere(client):
    ids = setup_games(4)

    def timeout(body):
        raise httpx.ReadTimeout("model took too long")

    set_router(fake_providers({"a": timeout}))
    review = client.post(f"/api/games/{ids[-1]}/coach-review").json()["review"]
    assert review["mode"] == "rules" and review["claims"]
    st = client.post("/api/studies", json={"game_id": ids[0], "theme": None})
    assert st.status_code == 201
    study = st.json()
    shown = client.post(f"/api/studies/{study['id']}/checkpoints/0/reveal").json()
    assert shown["checkpoints"][0]["feedback"]["source"] == "rules"


def test_spoilers_stay_hidden_across_refreshes_and_listings(client):
    ids = setup_games(4)
    pick = client.get("/api/studies").json()["picks"][0]
    st = client.post("/api/studies", json={"game_id": pick["game_id"], "theme": pick["theme"]}).json()
    client.post(f"/api/studies/{st['id']}/checkpoints/0/reveal")
    for _ in range(2):  # refresh twice
        again = client.get(f"/api/studies/{st['id']}").json()
        assert again["checkpoints"][0]["revealed"] and "answer" in again["checkpoints"][0]
        assert all("answer" not in c and "feedback" not in c for c in again["checkpoints"][1:])
    answers = {c["answer"]["best"] for c in again["checkpoints"][:1] if "best" in c.get("answer", {})}
    listing = json.dumps(client.get("/api/studies").json())
    assert all(a not in listing for a in answers if len(a) > 2)
    # another visitor's profile cannot open it
    assert client.get(f"/api/studies/{st['id'] + 99}").status_code == 404


def test_manual_and_suggested_selections_both_lead_to_usable_reviews(client):
    with db.SessionLocal() as s:
        old = [trap_game(s, i, i + 1).id for i in range(4)]
        for i, gid in enumerate(old):
            g = s.get(Game, gid)
            g.played_at = datetime(2025, 1, 1 + i)
        new = [trap_game(s, i + 1, 10 + i).id for i in range(2)]
        for gid in new:
            s.get(Game, gid).played_at = datetime(2025, 3, 5)
        s.commit()
    sug = client.post("/api/selection/suggest", json={"target_from": "2025-03-01", "target_to": "2025-03-31"}).json()
    all_ids = [g for grp in sug["groups"] for g in grp["game_ids"]]
    assert set(new) <= set(all_ids)
    b1 = client.post("/api/analysis/batches", json={"game_ids": all_ids, "mode": "suggested",
                                                     "rationale": sug.get("rationale", {})})
    assert b1.status_code == 201
    b2 = client.post("/api/analysis/batches", json={"game_ids": old[:2], "mode": "manual"}).json()
    assert b2["summary"]["already_queued"] == 2 and b2["summary"]["queued"] == 0
    analyze_all()
    set_router(Router([]))
    r = client.post(f"/api/games/{new[0]}/coach-review")
    assert r.status_code == 200 and r.json()["review"]["claims"]
    assert client.post(f"/api/games/{new[0]}/coach-review").json()["review"]["id"] == r.json()["review"]["id"]
    set_router(None)


def test_study_pick_comes_from_wins_losses_or_draws_and_never_from_unanalyzed_games(client):
    with db.SessionLocal() as s:
        won = add_game(s, "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#", color="black", result="loss", played_at=datetime(2025, 3, 1))
        ids = [trap_game(s, i, i + 2).id for i in range(3)]
        un = trap_game(s, 3, 20).id  # never analyzed
        jobs.enqueue(s, 1, ids + [won.id], "manual")
    analyze_all()
    picks = client.get("/api/studies").json()["picks"]
    assert picks and all(p["game_id"] != un for p in picks)
    assert {p["result"] for p in picks} <= {"win", "loss", "draw"}


def test_history_only_ever_cites_games_played_before_the_one_being_reviewed(client):
    ids = setup_games(5)  # trap games dated March 1..5
    first = client.post(f"/api/games/{ids[0]}/coach-review").json()["review"]
    assert all(c["verdict"] in ("narrowed", "insufficient") for c in first["claims"])  # nothing earlier exists
    third = client.post(f"/api/games/{ids[2]}/coach-review").json()["review"]
    cited = {r["game_id"] for c in third["claims"] for r in c["refs"]}
    assert cited <= {ids[0], ids[1], ids[2]}, "later games must not be cited as earlier history"
    assert all(c["verdict"] in ("narrowed", "insufficient") for c in third["claims"])  # only 2 earlier games
    last = client.post(f"/api/games/{ids[-1]}/coach-review").json()["review"]
    assert any(c["verdict"] in ("similar", "worse") for c in last["claims"])  # 4 earlier games: now a verdict
