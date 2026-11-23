"""Phase 4: chess correctness of the stored evidence, on reviewed positions from every phase.

The legality, orientation and mislabel checks use real Stockfish (skipped without one); the
position-key checks are plain python-chess.
"""
import chess
import pytest

from app import config
from app.analysis import analyze_game
from app.engine import EngineUnavailable, StockfishEngine
from app.features import position_key

from .test_analysis import ucis

try:
    _p = StockfishEngine()
    _p.close()
    HAVE_ENGINE = True
except EngineUnavailable:
    HAVE_ENGINE = False

needs_engine = pytest.mark.skipif(not HAVE_ENGINE, reason="Stockfish not installed")
# Same logic as production at a smaller node budget: these tests check legality and orientation,
# not the exact numbers.
FAST = {**config.ENGINE_PROFILE, "first_nodes": 30_000, "deep_nodes": 90_000, "deep_max": 2}

# One reviewed game fragment per phase: (name, start FEN, moves, player colour)
OPENING = ("opening", chess.STARTING_FEN, "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7", "white")
MIDDLEGAME = ("middlegame", chess.STARTING_FEN,
              "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7 e3 O-O Nf3 Nbd7 Rc1 c6 Bd3 dxc4 Bxc4 b5 Bd3", "white")
ENDGAME = ("endgame", "8/5R2/6p1/1b4Kp/2k5/p1P3P1/5P1P/8 b - - 0 43", "Ba4 Rc7+ Kb3 Rb7", "black")


@pytest.fixture(scope="module")
def engine():
    e = StockfishEngine()
    yield e
    e.close()


@needs_engine
@pytest.mark.parametrize("case", [OPENING, MIDDLEGAME, ENDGAME], ids=lambda c: c[0])
def test_every_stored_line_is_legal_from_its_own_position(engine, case):
    _, fen, san, color = case
    r = analyze_game(fen, ucis(san, fen), color, engine, FAST)
    board = chess.Board(fen)
    for p in r.plies:
        assert p["fen_before"] == board.fen()
        assert board.turn == (p["side"] == "white")          # side to move matches the side recorded
        assert p["is_player"] == (p["side"] == color)
        # the stored best move and every candidate line replay legally from this position
        if p["best_uci"]:
            assert chess.Move.from_uci(p["best_uci"]) in board.legal_moves
            assert board.san(chess.Move.from_uci(p["best_uci"])) == p["best_san"]
        for c in p["candidates"]:
            b = board.copy()
            for san_move in c["line"]:
                b.push_san(san_move)                          # raises if any move is illegal
        if p["reply_line"]:
            b = board.copy()
            b.push_uci(p["move_uci"])
            for san_move in p["reply_line"]:
                b.push_san(san_move)
        assert 0 <= p["win_drop"] <= 100 and 0 <= p["win_played"] <= 100
        board.push_uci(p["move_uci"])


@needs_engine
@pytest.mark.parametrize("case", [MIDDLEGAME, ENDGAME], ids=lambda c: c[0])
def test_mirrored_game_with_colours_swapped_gets_the_same_verdicts(engine, case):
    """Orientation check: flipping the board and the player's colour must not change who is 'worse'."""
    _, fen, san, color = case
    moves = ucis(san, fen)
    mirrored_fen = chess.Board(fen).mirror().fen()
    mirrored_moves = [chess.Move(chess.square_mirror(m.from_square), chess.square_mirror(m.to_square),
                                 m.promotion).uci() for m in map(chess.Move.from_uci, moves)]
    other = "black" if color == "white" else "white"
    a = analyze_game(fen, moves, color, engine, FAST)
    b = analyze_game(mirrored_fen, mirrored_moves, other, engine, FAST)
    assert len(a.plies) == len(b.plies)
    for pa, pb in zip(a.plies, b.plies):
        assert pa["is_player"] == pb["is_player"]
        assert pa["classification"] == pb["classification"] or abs(pa["win_drop"] - pb["win_drop"]) < 6
        assert abs(pa["win_played"] - pb["win_played"]) < 8
    assert {f["theme"] for f in a.findings if f["polarity"] == "worse"} == \
           {f["theme"] for f in b.findings if f["polarity"] == "worse"}


@needs_engine
def test_an_even_trade_and_good_development_are_not_labelled_as_losing_material(engine):
    # QGD: 10.Bxf6 Nxf6 (bishop for knight) is an even trade, not a blunder.
    san = "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7 e3 O-O Nf3 Nbd7 Bxf6 Nxf6"
    r = analyze_game(chess.STARTING_FEN, ucis(san), "white", engine, FAST)
    bxf6 = next(p for p in r.plies if p["move_san"] == "Bxf6")
    assert bxf6["classification"] in ("best", "good", "inaccuracy")
    assert not [f for f in r.findings if f["ply"] == bxf6["ply"] and f["polarity"] == "worse"
                and f["theme"].startswith("tactic.")]


@needs_engine
def test_a_sound_sacrifice_is_not_called_a_hanging_piece(engine):
    # Légal's mate, reviewed: the player (white) gives up the queen with 5.Nxe5! and wins by force.
    san = "e4 e5 Nf3 d6 Bc4 Bg4 Nc3 g6 Nxe5 Bxd1 Bxf7+ Ke7 Nd5#"
    r = analyze_game(chess.STARTING_FEN, ucis(san), "white", engine, FAST)
    for p in r.plies:
        if p["is_player"]:
            assert p["classification"] in ("best", "good", "forced", "inaccuracy"), (p["move_san"], p["classification"])
    assert not [f for f in r.findings if f["polarity"] == "worse"]


@needs_engine
def test_game_ending_in_the_opening_is_not_given_phase_findings_it_cannot_support(engine):
    r = analyze_game(chess.STARTING_FEN, ucis("f3 e5 g4 Qh4#"), "white", engine, FAST)
    assert all(f["phase"] == "opening" for f in r.findings)
    assert not any(f["theme"].startswith(("middlegame.", "endgame.")) for f in r.findings)


# ---------------------------------------------------------------- position matching (no engine needed)

def test_transpositions_share_one_position_key():
    a, b = chess.Board(), chess.Board()
    for m in "e4 e5 Nf3 Nc6".split():
        a.push_san(m)
    for m in "Nf3 Nc6 e4 e5".split():
        b.push_san(m)
    assert position_key(a) == position_key(b)


def test_position_key_separates_what_changes_the_position():
    base = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    key = position_key(base)
    assert key != position_key(chess.Board("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1"))   # side to move
    assert key != position_key(chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w Kkq - 0 1"))    # castling rights
    assert key == position_key(chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 17 40")) # move counters ignored
    # an en passant square only counts when the capture is actually legal
    legal_ep = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
    no_ep = chess.Board("4k3/8/8/3p4/8/8/8/4K3 w - d6 0 2")
    assert " d6" in position_key(legal_ep) and " d6" not in position_key(no_ep)
