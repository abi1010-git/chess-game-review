"""Generate the pilot sample: engine-played games where a weakness is planted on purpose.

Why synthetic: with real players the "right answer" is a matter of opinion, so every score would
depend on who judged it. Here the ground truth is known because we put it there. Every game is
played by Stockfish at a small node budget, picking its best or second-best move at random
(seeded), which gives varied but sensible middlegames and endgames with natural mistakes.

Players (all white, Italian Game: 1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6, so positions genuinely repeat):
  PilotA   "changed": 12 baseline games where 4.Ke2?! shows up 2 times, then 12 recent games
           where it shows up 8 times (the planted, recurring king walk).
  PilotB   "steady": the same 24 games' structure, but 4.Ke2?! shows up 2 times in each half,
           so nothing has changed. The app should not claim a change.
  PilotC   "sparse": only 2 games, so the app should decline to compare.
Run:  python -m eval.gen_games     (rewrites eval/data/*.pgn and eval/data/manifest.json)
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

import chess
import chess.pgn

from app.engine import StockfishEngine

OUT = Path(__file__).parent / "data"
NODES = 3000
MAX_PLIES = 90
BOOK = "e4 e5 Nf3 Nc6 Bc4 Nf6".split()
PLANTED = "Ke2"
NORMAL = "d3"


def pick(engine: StockfishEngine, board: chess.Board, rng: random.Random) -> chess.Move:
    lines = engine.analyse(board, NODES, multipv=3)
    moves = [chess.Move.from_uci(l.pv[0]) for l in lines if l.pv]
    if len(lines) > 1 and rng.random() < 0.25:
        a, b = lines[0], lines[1]
        if a.cp is not None and b.cp is not None and a.cp - b.cp < 60:
            return moves[1]
    return moves[0]


def play_game(engine, rng: random.Random, planted: bool, white: str, black: str, when: date, idx: int) -> dict:
    board = chess.Board()
    sans = []
    for san in BOOK + [PLANTED if planted else NORMAL]:
        m = board.parse_san(san)
        sans.append(board.san(m))
        board.push(m)
    planted_ply = 6 if planted else None
    while not board.is_game_over() and len(sans) < MAX_PLIES:
        m = pick(engine, board, rng)
        sans.append(board.san(m))
        board.push(m)
    if board.is_game_over():
        result = board.result()
    else:  # adjudicate on the engine's view of the final position
        line = engine.analyse(board, 20_000)[0]
        score = line.cp if line.cp is not None else (10_000 if line.mate and line.mate > 0 else -10_000)
        score = score if board.turn == chess.WHITE else -score
        result = "1-0" if score > 300 else "0-1" if score < -300 else "1/2-1/2"
    g = chess.pgn.Game()
    g.headers.update({"Event": "Pilot sample", "Site": "eval", "Date": when.strftime("%Y.%m.%d"), "Round": str(idx),
                      "White": white, "Black": black, "Result": result, "WhiteElo": "1500",
                      "BlackElo": str(1450 + rng.randrange(0, 100, 10)), "TimeControl": "600+0"})
    node, b = g, chess.Board()
    for san in sans:
        node = node.add_variation(b.parse_san(san))
        b.push_san(san)
    return {"pgn": str(g) + "\n\n", "planted_ply": planted_ply, "result": result, "plies": len(sans),
            "date": when.isoformat()}


def build_player(engine, name: str, seed: int, planted_by_half: tuple[int, int], n_half: int = 12) -> list[dict]:
    rng = random.Random(seed)
    games = []
    for half, count in enumerate(planted_by_half):
        flags = [True] * count + [False] * (n_half - count)
        rng.shuffle(flags)
        start = date(2025, 1, 6) if half == 0 else date(2025, 3, 3)
        for i, flag in enumerate(flags):
            games.append(play_game(engine, rng, flag, name, f"Opp{seed}-{half}{i}", start + timedelta(days=2 * i),
                                   len(games) + 1))
    return games


def main() -> None:
    OUT.mkdir(exist_ok=True)
    engine = StockfishEngine()
    manifest = {"players": {}, "nodes": NODES, "engine": engine.name}
    try:
        for name, seed, planted, n_half in (("PilotA", 11, (2, 8), 12), ("PilotB", 22, (2, 2), 12),
                                            ("PilotC", 33, (1, 1), 1)):
            games = build_player(engine, name, seed, planted, n_half)
            (OUT / f"{name}.pgn").write_text("".join(g["pgn"] for g in games), encoding="utf-8")
            manifest["players"][name] = [{k: v for k, v in g.items() if k != "pgn"} for g in games]
            print(name, len(games), "games,", sum(1 for g in games if g["planted_ply"]), "planted")
    finally:
        engine.close()
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
