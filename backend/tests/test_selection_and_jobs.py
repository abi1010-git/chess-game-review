from datetime import datetime, timedelta

import pytest

from app import db, jobs
from app.models import AnalysisJob, Game, utcnow
from app.pgn_parse import fingerprint

_n = 0


def make_game(s, played_at, rating=1500, time_class="blitz", provider="lichess", rated=True, color="white",
              result="win"):
    global _n
    _n += 1
    g = Game(profile_id=1, source_kind="account", provider=provider, provider_game_id=f"g{_n}",
             fingerprint=fingerprint("start", [str(_n)], {}), white="me", black="opp", result="1-0",
             player_color=color, player_name="me", player_rating=rating, opponent_name="opp",
             player_result=result, played_at=played_at, time_class=time_class, rated=rated,
             initial_fen="start", moves_uci="e2e4", ply_count=1, pgn="", headers={})
    s.add(g)
    s.commit()
    return g


@pytest.fixture()
def history(client):
    """Target month: March 2025. Older games in several pools and ratings."""
    with db.SessionLocal() as s:
        ids = {
            "t1": make_game(s, datetime(2025, 3, 5), 1510).id,
            "t2": make_game(s, datetime(2025, 3, 20), 1530, color="black", result="loss").id,
            "near": [make_game(s, datetime(2025, 2, 1) - timedelta(days=i), 1500 + (i % 5) * 10).id
                     for i in range(12)],
            "far_rating": make_game(s, datetime(2025, 1, 15), 1900).id,
            "other_pool": make_game(s, datetime(2025, 1, 10), 1500, time_class="rapid").id,
            "other_site": make_game(s, datetime(2025, 1, 9), 1500, provider="chesscom").id,
            "unrated": make_game(s, datetime(2025, 1, 8), None).id,
            "undated": make_game(s, None, 1500).id,
            "after": make_game(s, datetime(2025, 4, 2), 1500).id,
        }
    return ids


def test_filters(client, history):
    r = client.get("/api/games", params={"date_from": "2025-03-01", "date_to": "2025-03-31"}).json()
    assert {g["id"] for g in r["items"]} == {history["t1"], history["t2"]}
    r = client.get("/api/games", params={"color": "black"}).json()
    assert [g["id"] for g in r["items"]] == [history["t2"]]
    r = client.get("/api/games", params={"time_class": "rapid"}).json()
    assert [g["id"] for g in r["items"]] == [history["other_pool"]]
    r = client.get("/api/games", params={"rating_min": 1800}).json()
    assert [g["id"] for g in r["items"]] == [history["far_rating"]]
    r = client.get("/api/games/ids", params={"provider": "chesscom"}).json()
    assert r["ids"] == [history["other_site"]]
    facets = client.get("/api/games/facets").json()
    assert facets["total"] == 20 and "rapid" in facets["time_classes"]


def test_manual_selection_preview_and_idempotent_batch(client, history):
    ids = [history["t1"], history["t2"]]
    p = client.post("/api/analysis/preview", json={"game_ids": ids}).json()
    assert p["will_queue"] == 2 and p["already_queued"] == 0

    b1 = client.post("/api/analysis/batches", json={"game_ids": ids, "mode": "manual"}).json()
    assert b1["summary"]["queued"] == 2
    assert b1["progress"]["counts"] == {"queued": 2}

    # Same request again: no duplicate jobs.
    b2 = client.post("/api/analysis/batches", json={"game_ids": ids + [history["after"]]}).json()
    assert b2["summary"] == {"queued": 1, "requeued": 0, "already_queued": 2, "already_analyzed": 0}
    with db.SessionLocal() as s:
        assert s.query(AnalysisJob).count() == 3

    p = client.post("/api/analysis/preview", json={"game_ids": ids}).json()
    assert p["will_queue"] == 0 and p["already_queued"] == 2
    listed = client.get("/api/games", params={"analysis": "queued"}).json()
    assert listed["total"] == 3
    assert client.post("/api/analysis/batches", json={"game_ids": [999999]}).status_code == 404


def test_suggested_set_separates_targets_from_comparisons(client, history):
    r = client.post("/api/selection/suggest", json={"target_from": "2025-03-01", "target_to": "2025-03-31",
                                                     "max_comparison": 10}).json()
    groups = {g["key"].split(":")[0]: g for g in r["groups"]}
    assert set(groups["target"]["game_ids"]) == {history["t1"], history["t2"]}
    comp = groups["comparison"]["game_ids"]
    assert len(comp) == 10
    assert set(comp) <= set(history["near"])  # same pool, nearby rating, earlier only
    for excluded in ("far_rating", "other_pool", "other_site", "after", "undated", "t1", "t2"):
        assert history[excluded] not in comp
    assert "±100" in groups["comparison"]["reason"]
    assert any("no recorded date" in w for w in r["warnings"])
    assert r["preview"]["will_queue"] == 12

    # The player edits the proposal (drops two comparison games) and queues it.
    chosen = groups["target"]["game_ids"] + comp[:8]
    roles = {**{i: "target" for i in groups["target"]["game_ids"]}, **{i: "comparison" for i in comp[:8]}}
    b = client.post("/api/analysis/batches", json={"game_ids": chosen, "mode": "suggested", "roles": roles,
                                                   "rationale": {"groups": r["groups"]}}).json()
    assert b["mode"] == "suggested" and b["progress"]["total"] == 10
    assert sum(1 for g in b["progress"]["games"] if g["role"] == "target") == 2


def test_suggestion_fills_with_unrated_games_and_explains(client):
    with db.SessionLocal() as s:
        make_game(s, datetime(2025, 3, 5), 1500)
        unrated = make_game(s, datetime(2025, 1, 1), None).id
    r = client.post("/api/selection/suggest", json={"target_from": "2025-03-01", "target_to": "2025-03-31"}).json()
    unrated_group = [g for g in r["groups"] if g["key"].startswith("comparison_unrated")][0]
    assert unrated_group["game_ids"] == [unrated]
    assert any("Only 1 comparison" in w for w in r["warnings"])


def test_suggestion_with_no_target_games(client, history):
    r = client.post("/api/selection/suggest", json={"target_from": "2020-01-01", "target_to": "2020-01-31"}).json()
    assert r["groups"] == [] and "No games" in r["warnings"][-1]


# ---------------------------------------------------------------- job leases

def _queue(client, ids):
    client.post("/api/analysis/batches", json={"game_ids": ids})


def test_claim_complete_and_stale_worker_cannot_finish(client, history):
    _queue(client, [history["t1"]])
    with db.SessionLocal() as s:
        job = jobs.claim_next(s, "worker-a", lease_seconds=60)
        assert job.status == "running" and job.attempts == 1
        assert jobs.claim_next(s, "worker-b") is None  # lease held

        # worker-a dies: lease expires and worker-b takes over
        s.query(AnalysisJob).update({"lease_expires_at": utcnow() - timedelta(seconds=1)})
        s.commit()
        taken = jobs.claim_next(s, "worker-b")
        assert taken.id == job.id and taken.attempts == 2 and taken.lease_owner == "worker-b"

        assert jobs.complete(s, job.id, "worker-a") is False  # stale worker is ignored
        assert jobs.complete(s, job.id, "worker-b") is True
        assert s.get(AnalysisJob, job.id, populate_existing=True).status == "done"
    p = client.post("/api/analysis/preview", json={"game_ids": [history["t1"]]}).json()
    assert p["already_analyzed"] == 1 and p["will_queue"] == 0


def test_retries_are_bounded_and_failed_jobs_can_be_requeued(client, history):
    _queue(client, [history["t1"]])
    with db.SessionLocal() as s:
        for _ in range(3):
            j = jobs.claim_next(s, "w")
            assert jobs.fail(s, j.id, "w", "engine crashed")
        assert jobs.claim_next(s, "w") is None
        assert s.query(AnalysisJob).one().status == "failed"
    b = client.post("/api/analysis/batches", json={"game_ids": [history["t1"]]}).json()
    assert b["summary"]["requeued"] == 1


def test_expired_lease_after_last_attempt_never_looks_complete(client, history):
    _queue(client, [history["t1"]])
    with db.SessionLocal() as s:
        j = jobs.claim_next(s, "w")
        s.query(AnalysisJob).update({"attempts": 3, "lease_expires_at": utcnow() - timedelta(seconds=1)})
        s.commit()
        assert jobs.claim_next(s, "w2") is None
        job = s.get(AnalysisJob, j.id, populate_existing=True)
        assert job.status == "failed" and "stopped responding" in job.last_error


def test_cancel_batch_stops_running_and_queued_jobs(client, history):
    b = client.post("/api/analysis/batches", json={"game_ids": [history["t1"], history["t2"]]}).json()
    with db.SessionLocal() as s:
        running = jobs.claim_next(s, "w")
    assert client.post(f"/api/analysis/batches/{b['id']}/cancel").json() == {"cancelled": 2}
    with db.SessionLocal() as s:
        assert jobs.complete(s, running.id, "w") is False  # a cancelled job is never marked complete
    progress = client.get(f"/api/analysis/batches/{b['id']}").json()["progress"]
    assert progress["counts"] == {"cancelled": 2}
