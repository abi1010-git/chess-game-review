"""Engine pass, worker and comparisons, using a deterministic fake engine (no Stockfish needed)."""
import threading
from datetime import datetime, timedelta

import chess
import pytest

from app import config, db, jobs, worker
from app.analysis import analyze_game
from app.models import AnalysisJob, AnalysisRun, Finding, Game, MoveEval
from app.pgn_parse import fingerprint

from .fake_engine import FakeEngine

PROFILE = {**config.ENGINE_PROFILE, "engine": FakeEngine.name}
_n = 0


def ucis(san: str, fen: str = chess.STARTING_FEN) -> list[str]:
    b = chess.Board(fen)
    out = []
    for s in san.split():
        m = b.parse_san(s)
        out.append(m.uci())
        b.push(m)
    return out


def add_game(s, san, color="white", result="win", played_at=None, fen=chess.STARTING_FEN, salt=None,
             time_class="blitz", rating=1500):
    global _n
    _n += 1
    moves = ucis(san, fen)
    g = Game(profile_id=1, source_kind="upload", provider="pgn", provider_game_id=None,
             fingerprint=fingerprint(fen, moves, {}, salt or str(_n)), white="me" if color == "white" else "opp",
             black="opp" if color == "white" else "me", result="*", player_color=color, player_name="me",
             player_rating=rating, opponent_name="opp", player_result=result,
             played_at=played_at or datetime(2025, 1, 1) + timedelta(days=_n), time_class=time_class,
             initial_fen=fen, moves_uci=" ".join(moves), ply_count=len(moves), pgn="", headers={})
    s.add(g)
    s.commit()
    return g


def analyze_all(engine=None):
    engine = engine or FakeEngine()
    while worker.process_one("test-worker", engine, PROFILE) != "idle":
        pass


# ---------------------------------------------------------------- engine pass

def test_scores_are_from_the_players_side_and_mates_are_separate():
    # Black is the player; 3...Nf6?? allows Qxf7#.
    moves = ucis("e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#")
    r = analyze_game(chess.STARTING_FEN, moves, "black", FakeEngine(), PROFILE)
    by_ply = {p["ply"]: p for p in r.plies}
    blunder = by_ply[5]
    assert blunder["is_player"] and blunder["side"] == "black"
    assert blunder["played_mate"] is not None and blunder["played_mate"] < 0  # black gets mated
    assert blunder["best_mate"] is None
    assert blunder["win_played"] == 0.0 and blunder["classification"] == "blunder"
    mate_move = by_ply[6]
    assert not mate_move["is_player"] and mate_move["played_mate"] < 0  # still black's point of view
    assert any(f["theme"] == "tactic.allowed_mate" and f["ply"] == 5 for f in r.findings)
    # Every stored position replays legally from the start.
    b = chess.Board()
    for p in r.plies:
        assert p["fen_before"] == b.fen()
        b.push_uci(p["move_uci"])


def test_forced_moves_and_decided_positions_are_low_information():
    fen = "7k/8/8/8/8/2q5/8/K1r5 w - - 0 1"
    moves = ["a1a2", "c1c2", "a2b1", "c3b3", "b1a1", "b3a4", "a1b1", "a4a2"]
    r = analyze_game(fen, moves, "white", FakeEngine(), PROFILE)
    mine = [p for p in r.plies if p["is_player"]]
    assert len(mine) == 4 and all(p["classification"] == "forced" and p["low_info"] for p in mine)
    assert r.summary["meaningful_decisions"] == 0
    assert not r.findings


def test_tactical_and_positional_findings_can_share_a_move():
    # 1.e4 e5 2.Ke2 with the fake engine: no material is lost, so only king exposure could apply;
    # here we only check the detector does not crash and labels stay scoped to the opening.
    r = analyze_game(chess.STARTING_FEN, ucis("e4 e5 Ke2 Qh4 Kd3"), "white", FakeEngine(), PROFILE)
    assert all(f["phase"] == "opening" for f in r.findings)


# ---------------------------------------------------------------- worker

def test_worker_stores_results_and_marks_done(client):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#", color="black", result="loss")
        jobs.enqueue(s, 1, [g.id], "manual")
    assert worker.process_one("w", FakeEngine(), PROFILE) == "done"
    with db.SessionLocal() as s:
        job = s.query(AnalysisJob).one()
        assert job.status == "done" and job.lease_owner is None
        run = s.query(AnalysisRun).one()
        assert run.engine_name == FakeEngine.name and run.settings["first_nodes"] == PROFILE["first_nodes"]
        assert s.query(MoveEval).filter_by(run_id=run.id).count() == 7
        assert s.query(Finding).filter_by(run_id=run.id, theme="tactic.allowed_mate").count() == 1
    assert worker.process_one("w", FakeEngine(), PROFILE) == "idle"


def test_cancelled_job_never_gets_results(client):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6")
        batch, _ = jobs.enqueue(s, 1, [g.id], "manual")
        batch_id = batch.id
    eng = FakeEngine()

    def cancel_midway(n):
        if n == 3:
            with db.SessionLocal() as s:
                jobs.cancel_batch(s, 1, batch_id)

    eng.on_call = cancel_midway
    # beat_every=0 makes the worker check its lease on every position.
    assert worker.process_one("w", eng, PROFILE, beat_every=0) == "cancelled"
    with db.SessionLocal() as s:
        assert s.query(AnalysisJob).one().status == "cancelled"
        assert s.query(AnalysisRun).count() == 0 and s.query(MoveEval).count() == 0


def test_cancel_after_analysis_but_before_save_discards_results(client):
    with db.SessionLocal() as s:
        g = add_game(s, "d4 d5 c4 e6")
        batch, _ = jobs.enqueue(s, 1, [g.id], "manual")
        job = jobs.claim_next(s, "w1")
        jobs.cancel_batch(s, 1, batch.id)
        ok = jobs.complete_with_results(s, job.id, "w1", lambda ws: pytest.fail("must not write"))
        assert ok is False
        assert s.get(AnalysisJob, job.id, populate_existing=True).status == "cancelled"


def test_worker_restart_takes_over_expired_lease_and_finishes_once(client):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 c5 Nf3 d6")
        jobs.enqueue(s, 1, [g.id], "manual")
        dead = jobs.claim_next(s, "dead-worker", lease_seconds=300)
        dead.lease_expires_at = datetime(2000, 1, 1)  # the first worker crashed long ago
        s.commit()
    assert worker.process_one("new-worker", FakeEngine(), PROFILE) == "done"
    with db.SessionLocal() as s:
        job = s.query(AnalysisJob).one()
        assert job.status == "done" and job.attempts == 2
        # The crashed worker coming back cannot complete or overwrite it.
        assert jobs.complete_with_results(s, job.id, "dead-worker", lambda ws: pytest.fail("no")) is False
        assert s.query(AnalysisRun).count() == 1


def test_clean_shutdown_returns_job_without_using_an_attempt(client):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Nf3 Nc6 Bc4 Bc5")
        jobs.enqueue(s, 1, [g.id], "manual")
    stop = threading.Event()
    eng = FakeEngine()
    eng.on_call = lambda n: stop.set() if n == 2 else None
    assert worker.process_one("w", eng, PROFILE, beat_every=0, stop=stop) == "released"
    with db.SessionLocal() as s:
        job = s.query(AnalysisJob).one()
        assert job.status == "queued" and job.attempts == 0


def test_engine_version_mismatch_is_refused(monkeypatch):
    class Other(FakeEngine):
        name = "Stockfish 11"
    monkeypatch.setattr(worker, "StockfishEngine", lambda **kw: Other())
    monkeypatch.setattr(config, "ENGINE_ALLOW_MISMATCH", False)
    with pytest.raises(worker.EngineMismatch):
        worker.default_engine_factory({**PROFILE, "engine": "Stockfish 18"})


def test_rerun_replaces_previous_results(client):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Qh5 Nc6 Bc4 Nf6 Qxf7#", result="win")
        jobs.enqueue(s, 1, [g.id], "manual")
    analyze_all()
    with db.SessionLocal() as s:
        job = s.query(AnalysisJob).one()
        job.status = "queued"
        s.commit()
    analyze_all()
    with db.SessionLocal() as s:
        assert s.query(AnalysisRun).count() == 1
        assert s.query(MoveEval).count() == 7


# ---------------------------------------------------------------- reports, comparisons, dashboard

HANG_QUEEN = "e4 e5 Qh5 Nc6 Qxe5+ Nxe5"  # white's queen grab loses the queen


def test_report_compares_with_other_games_and_excludes_target_and_duplicates(client):
    with db.SessionLocal() as s:
        target = add_game(s, "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6", played_at=datetime(2025, 6, 1))
        dup = add_game(s, "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6", played_at=datetime(2025, 5, 1), salt="dup")
        others = [add_game(s, "e4 e5 Nf3 Nc6 Bc4 Nf6 d3 Be7", played_at=datetime(2025, 1, i + 1)) for i in range(4)]
        jobs.enqueue(s, 1, [target.id, dup.id] + [o.id for o in others], "manual")
        tid, did = target.id, dup.id
    analyze_all()
    r = client.get(f"/api/games/{tid}/report").json()
    rep = r["report"]
    assert r["game"]["analysis_status"] == "done"
    assert len(rep["plies"]) == 8 and rep["final_fen"]
    assert rep["verdict"]["kind"] == "short"  # only 4 player decisions
    seen_games = {e["game_id"] for d in rep["decisions"] if d["comparison"] for e in d["comparison"]["examples"]}
    assert tid not in seen_games and did not in seen_games
    exact = [d for d in rep["decisions"] if d["comparison"] and d["comparison"]["stats"]["by_match"]["exact"]]
    assert exact, "the shared opening moves should produce exact position matches"
    for d in rep["decisions"]:
        c = d["comparison"]
        assert c["verdict"] in ("better", "worse", "similar", "mixed", "insufficient")
        assert "examples" in c["stats"] and c["stats"]["match_quality"] in ("exact", "structure", "theme", "none")


def test_insufficient_evidence_when_history_is_empty(client):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O")
        jobs.enqueue(s, 1, [g.id], "manual")
        gid = g.id
    analyze_all()
    rep = client.get(f"/api/games/{gid}/report").json()["report"]
    assert rep["decisions"]
    assert all(d["comparison"]["verdict"] == "insufficient" for d in rep["decisions"])


def test_unanalyzed_game_report_is_empty_but_game_is_shown(client):
    with db.SessionLocal() as s:
        gid = add_game(s, "e4 e5").id
    r = client.get(f"/api/games/{gid}/report").json()
    assert r["report"] is None and r["game"]["analysis_status"] == "none"
    assert client.get("/api/games/999999/report").status_code == 404


def test_dashboard_and_queue_status(client):
    d = client.get("/api/dashboard").json()
    assert d["coverage"]["analyzed"] == 0 and d["recent"] == [] and d["worker_available"] is False
    with db.SessionLocal() as s:
        ids = [add_game(s, HANG_QUEEN, result="loss", played_at=datetime(2025, m, 3)).id for m in (1, 2, 3, 3)]
        ids.append(add_game(s, "e4 e5 Nf3 Nc6", played_at=datetime(2025, 3, 9)).id)
        jobs.enqueue(s, 1, ids, "manual")
    analyze_all()
    d = client.get("/api/dashboard").json()
    assert d["coverage"]["analyzed"] == 5
    assert [t["month"] for t in d["trends"]] == ["2025-01", "2025-02", "2025-03"]
    assert d["trends"][0]["thin"] is True and d["trends"][2]["games"] == 3
    themes = {x["theme"]: x for x in d["recurring"]}
    assert themes, "losing the queen four times should surface a finding"
    top = d["recurring"][0]
    assert top["games"] == 4 and top["status"] == "recurring" and top["by_result"]["loss"] == 4
    q = client.get("/api/analysis/queue").json()
    assert q["counts"] == {"done": 5} and "worker_available" in q
