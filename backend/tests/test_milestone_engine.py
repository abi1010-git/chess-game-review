"""Phase 2 milestone checks against real Stockfish (skipped when no engine binary is available).

Each case was reviewed by hand: the expected verdicts below are what a strong player would say,
and the assertions check that the app says it with evidence and keeps its claims scoped.
"""
from datetime import datetime

import chess
import pytest

from app import compare, config, db, jobs, worker
from app.analysis import analyze_game
from app.engine import EngineUnavailable, StockfishEngine
from app.models import Game

from .test_analysis import add_game, ucis

try:
    _probe = StockfishEngine()
    _probe.close()
    HAVE_ENGINE = True
except EngineUnavailable:
    HAVE_ENGINE = False

pytestmark = pytest.mark.skipif(not HAVE_ENGINE, reason="Stockfish not installed")
PROFILE = config.ENGINE_PROFILE  # the production budget, so verdicts match what players see


@pytest.fixture(scope="module")
def engine():
    e = StockfishEngine()
    yield e
    e.close()


def run(engine, san, color, fen=chess.STARTING_FEN):
    return analyze_game(fen, ucis(san, fen), color, engine, PROFILE)


def themes_at(r, ply):
    return {f["theme"] for f in r.findings if f["ply"] == ply}


# ---------------------------------------------------------------- hand-reviewed, one per phase

def test_opening_king_walk_is_flagged_as_early_king_exposure(engine):
    # 4.Ke2?: the king leaves its castling squares with queens on; 4.d3 / 4.Ng5 were normal.
    r = run(engine, "e4 e5 Nf3 Nc6 Bc4 Nf6 Ke2", "white")
    f = next(f for f in r.findings if f["ply"] == 6)
    assert f["theme"] == "opening.king_exposure" and f["phase"] == "opening"
    assert f["evidence"]["king_moved"] is True and f["evidence"]["win_drop"] >= 10
    assert f["evidence"]["best_move"] != "Ke2"


def test_middlegame_hanging_piece(engine):
    # Queen's Gambit Declined: after 9...b5 the bishop must retreat (10.Bd3). 10.Bxb5?? cxb5
    # gives a bishop for a pawn.
    san = "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7 e3 O-O Nf3 Nbd7 Rc1 c6 Bd3 dxc4 Bxc4 b5 Bxb5"
    r = run(engine, san, "white")
    ply = len(ucis(san)) - 1
    plies = {p["ply"]: p for p in r.plies}
    assert plies[ply]["phase"] == "middlegame"
    assert plies[ply]["classification"] in ("mistake", "blunder")
    found = themes_at(r, ply)
    assert found & {"tactic.material_loss", "tactic.hanging_piece"}, found
    f = next(f for f in r.findings if f["ply"] == ply and f["theme"].startswith("tactic."))
    assert f["evidence"]["reply_line"][0] == "cxb5"  # the line that proves it


def test_endgame_defensive_slip(engine):
    # From one of the imported games, reviewed: black must keep the bishop on the e8-h5 diagonal
    # (Be8 holds about -0.4); Ba4? lets white's king and rook combine and loses.
    fen = "8/5R2/6p1/1b4Kp/2k5/p1P3P1/5P1P/8 b - - 0 43"
    r = run(engine, "Ba4", "black", fen=fen)
    p = r.plies[0]
    assert p["phase"] == "endgame" and p["win_drop"] >= 10
    assert "endgame.defense" in themes_at(r, 0)
    assert "endgame.defense" in p["themes"]


# ---------------------------------------------------------------- short and forced games

def test_four_move_game_gets_no_improvement_verdict_but_a_strongest_observation(client, engine):
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#", color="black", result="loss")
        jobs.enqueue(s, 1, [g.id], "manual")
        gid = g.id
    assert worker.process_one("w", engine, PROFILE) == "done"
    rep = client.get(f"/api/games/{gid}/report").json()["report"]
    assert rep["verdict"]["kind"] == "short"
    assert rep["verdict"]["strongest"]["ply"] == 5
    assert rep["verdict"]["strongest"]["title"] == "Allowed a forced mate"
    assert all(d["comparison"]["verdict"] == "insufficient" for d in rep["decisions"])


def test_game_with_only_forced_choices(client, engine):
    fen = "7k/8/8/8/8/2q5/8/K1r5 w - - 0 1"
    with db.SessionLocal() as s:
        g = Game(profile_id=1, source_kind="upload", provider="pgn", fingerprint="forced-only", white="me",
                 black="opp", result="0-1", player_color="white", player_name="me", player_result="loss",
                 played_at=datetime(2025, 1, 1), time_class="blitz", initial_fen=fen,
                 moves_uci="a1a2 c1c2 a2b1 c3b3 b1a1 b3a4 a1b1 a4a2", ply_count=8, pgn="", headers={})
        s.add(g)
        s.commit()
        jobs.enqueue(s, 1, [g.id], "manual")
        gid = g.id
    assert worker.process_one("w", engine, PROFILE) == "done"
    rep = client.get(f"/api/games/{gid}/report").json()["report"]
    assert rep["verdict"]["kind"] == "no_decisions"
    assert rep["findings"] == []
    assert all(p["classification"] == "forced" for p in rep["plies"] if p["is_player"])
    assert rep["loss"]["decisive"] is None  # a loss with nothing to blame on a decision


def test_lost_game_separates_earlier_decision_from_decisive_mistake(client, engine):
    # Legal's trap, player is black: 4...g6 (no development; allows 5.Nxe5) is the earlier slip,
    # 5...Bxd1?? (allows mate) is the decisive one. 6...Ke7 is forced and must not be blamed.
    with db.SessionLocal() as s:
        g = add_game(s, "e4 e5 Nf3 d6 Bc4 Bg4 Nc3 g6 Nxe5 Bxd1 Bxf7+ Ke7 Nd5#", color="black", result="loss")
        jobs.enqueue(s, 1, [g.id], "manual")
        gid = g.id
    assert worker.process_one("w", engine, PROFILE) == "done"
    rep = client.get(f"/api/games/{gid}/report").json()["report"]
    loss = rep["loss"]
    assert loss["decisive"]["ply"] == 9 and loss["decisive"]["move"] == "Bxd1"
    assert loss["decisive"]["basis"]["kind"] == "finding"
    assert loss["decisive"]["basis"]["title"] == "Allowed a forced mate"
    assert loss["earlier"]["ply"] == 7 and loss["earlier"]["move"] == "g6"
    assert loss["earlier"]["win_drop"] < loss["decisive"]["win_drop"]
    # With no other games, it must not call anything a recurring weakness.
    assert loss["decisive"]["recurrence"]["status"] in ("no_data", "not_seen")
    forced = next(p for p in rep["plies"] if p["ply"] == 11)
    assert forced["classification"] == "forced" and forced["low_info_reason"] == "forced"


def test_repeated_mistake_across_games_counts_wins_and_draws(client, engine):
    # The same queen raid in three games (two of them still won on time, say) is a pattern;
    # the report must count all of them, not only the losses.
    with db.SessionLocal() as s:
        ids = []
        # Different continuations, so these are distinct games rather than duplicates.
        for i, (res, tail) in enumerate([("win", "d4"), ("draw", "d3"), ("loss", "Nc3"), ("loss", "f4")]):
            ids.append(add_game(s, f"e4 e5 Qh5 Nc6 Qxe5+ Nxe5 {tail}", result=res,
                                played_at=datetime(2025, 1, 5 + i)).id)
        jobs.enqueue(s, 1, ids, "manual")
    while worker.process_one("w", engine, PROFILE) != "idle":
        pass
    rep = client.get(f"/api/games/{ids[-1]}/report").json()["report"]
    worst = next(d for d in rep["decisions"] if d["ply"] == 4)
    assert worst["classification"] == "blunder"
    cmp = worst["comparison"]
    assert cmp["stats"]["by_match"]["exact"] == 3  # the same position in the three other games
    assert cmp["verdict"] == "similar"
    rec = worst["recurrence"]
    assert rec["status"] == "recurring" or rec["games_with_mistake"] == 3
    assert rec["by_result"]["win"] == 1 and rec["by_result"]["draw"] == 1
