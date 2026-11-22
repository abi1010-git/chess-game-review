"""Engine pass over one game: record the evidence first, then derive findings from it.

Pass 1 analyses every position at a fixed node budget with several candidate lines.
Pass 2 re-checks the player's critical or close decisions at a larger budget, scoring the
played move directly, and marks a verdict unstable when the two passes disagree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Callable

import chess

from . import features as F
from .engine import Engine, Line

TERMINAL_MATED = Line(cp=None, mate=-1, pv=[])  # side to move is checkmated (see negate())
TERMINAL_DRAW = Line(cp=0, mate=None, pv=[])

INACCURACY, MISTAKE, BLUNDER = 5.0, 10.0, 20.0
ONLY_MOVE_GAP = 20.0  # second-best line is this much worse: the decision had one good answer
DECIDED = 97.0  # positions already this won (or lost) say little about decision quality


class Cancelled(Exception):
    """The job was cancelled or its lease was taken over while analysing."""


@dataclass
class Score:
    cp: int | None
    mate: int | None

    @property
    def win(self) -> float:
        return F.win_pct(self.cp, self.mate)

    def neg(self) -> "Score":
        return Score(None if self.cp is None else -self.cp, None if self.mate is None else -self.mate)

    def text(self) -> str:
        return F.score_text(self.cp, self.mate)


def _score(line: Line) -> Score:
    return Score(line.cp, line.mate)


def classify(drop: float, played_is_best: bool, forced: bool) -> str:
    if forced:
        return "forced"
    if played_is_best:
        return "best"
    if drop >= BLUNDER:
        return "blunder"
    if drop >= MISTAKE:
        return "mistake"
    if drop >= INACCURACY:
        return "inaccuracy"
    return "good"


def san_line(board: chess.Board, uci_moves: list[str], limit: int = 8) -> list[str]:
    b = board.copy(stack=False)
    out = []
    for u in uci_moves[:limit]:
        try:
            m = chess.Move.from_uci(u)
        except ValueError:
            break
        if m not in b.legal_moves:
            break
        out.append(b.san(m))
        b.push(m)
    return out


def line_material_delta(board: chess.Board, uci_moves: list[str], us: bool, plies: int = 4) -> int:
    """Our material balance change after following a line, extended while captures continue."""
    start = F.material_balance(board, us)
    b = board.copy(stack=False)
    for i, u in enumerate(uci_moves[: plies + 2]):
        m = chess.Move.from_uci(u)
        if m not in b.legal_moves:
            break
        if i >= plies and not b.is_capture(m):
            break
        b.push(m)
    return F.material_balance(b, us) - start


@dataclass
class GameAnalysis:
    engine_name: str
    settings: dict
    plies: list[dict] = field(default_factory=list)  # one dict per move, MoveEval columns
    findings: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    contexts: list[dict] = field(default_factory=list)  # in-memory detail for finding detectors


def _terminal(board: chess.Board) -> Line | None:
    if board.is_checkmate():
        return TERMINAL_MATED
    if board.is_stalemate() or board.is_insufficient_material():
        return TERMINAL_DRAW
    return None


def _analyse(engine: Engine, board: chess.Board, nodes: int, multipv: int) -> list[Line]:
    t = _terminal(board)
    if t is not None:
        return [t]
    n = board.legal_moves.count()
    lines = [l for l in engine.analyse(board, nodes, multipv=min(multipv, n)) if l.pv]
    if not lines:
        raise RuntimeError(f"The engine returned no line for {board.fen()}")
    return lines


def analyze_game(initial_fen: str, moves_uci: list[str], player_color: str, engine: Engine, profile: dict,
                 progress: Callable[[int, int], None] | None = None) -> GameAnalysis:
    player = chess.WHITE if player_color == "white" else chess.BLACK
    boards = [chess.Board(initial_fen)]
    for u in moves_uci:
        b = boards[-1].copy(stack=False)
        b.push(chess.Move.from_uci(u))
        boards.append(b)

    total = len(boards) + profile["deep_max"]
    lines: list[list[Line]] = []
    for i, b in enumerate(boards):
        lines.append(_analyse(engine, b, profile["first_nodes"], profile["multipv"]))
        if progress:
            progress(i + 1, total)

    out = GameAnalysis(engine_name=engine.name, settings=dict(profile))
    for i, u in enumerate(moves_uci):
        b, after = boards[i], boards[i + 1]
        mover = b.turn
        move = chess.Move.from_uci(u)
        best = _score(lines[i][0])
        best_uci = lines[i][0].move
        played_is_best = best_uci == u
        played = best if played_is_best else _score(lines[i + 1][0]).neg()
        legal = b.legal_moves.count()
        cands = []
        for ln in lines[i]:
            sc = _score(ln)
            cands.append({"uci": ln.move, "san": san_line(b, ln.pv, 1)[0] if ln.pv else None,
                          "cp": sc.cp, "mate": sc.mate, "win": round(sc.win, 1), "line": san_line(b, ln.pv)})
        reply = lines[i + 1][0].pv if lines[i + 1][0].pv else []
        out.contexts.append({
            "board": b, "after": after, "move": move, "mover": mover, "is_player": mover == player,
            "best": best, "played": played, "best_pv": lines[i][0].pv, "reply_pv": reply,
            "first_best": best, "first_played": played, "deep": None, "legal": legal, "cands": cands,
        })

    # Pass 2: deeper checks on the player's close or critical decisions.
    def priority(c):
        drop = max(0.0, c["best"].win - c["played"].win)
        gap = c["cands"][0]["win"] - c["cands"][1]["win"] if len(c["cands"]) > 1 else 0
        mate_involved = c["best"].mate is not None or c["played"].mate is not None
        if drop >= INACCURACY - 2 or mate_involved:
            return drop + (5 if mate_involved else 0)
        if gap >= ONLY_MOVE_GAP and c["move"].uci() == c["cands"][0]["uci"]:
            return 1.0  # an only-move the player found: confirm the positive verdict
        return None

    ranked = sorted(((priority(c), idx) for idx, c in enumerate(out.contexts)
                     if c["is_player"] and c["legal"] > 1 and priority(c) is not None), reverse=True)
    done = len(boards)
    for _, idx in ranked[: profile["deep_max"]]:
        c = out.contexts[idx]
        b, u = c["board"], c["move"].uci()
        deep = _analyse(engine, b, profile["deep_nodes"], 2)
        deep_best = _score(deep[0])
        if deep[0].move == u:
            deep_played, reply = deep_best, deep[0].pv[1:]
        else:
            pl = engine.analyse(b, profile["deep_nodes"], multipv=1, root_moves=[u])
            deep_played, reply = (_score(pl[0]), pl[0].pv[1:]) if pl else (c["played"], c["reply_pv"])
        first_drop = max(0.0, c["best"].win - c["played"].win)
        deep_drop = max(0.0, deep_best.win - deep_played.win)
        stable = (abs(c["best"].win - deep_best.win) <= profile["stable_win_delta"]
                  and abs(first_drop - deep_drop) <= profile["stable_win_delta"])
        c["deep"] = {"best": deep_best, "played": deep_played, "stable": stable,
                     "first_drop": round(first_drop, 1), "deep_drop": round(deep_drop, 1)}
        c["best"], c["played"], c["best_pv"], c["reply_pv"] = deep_best, deep_played, deep[0].pv, reply
        done += 1
        if progress:
            progress(done, total)

    prev_move: chess.Move | None = None
    for i, c in enumerate(out.contexts):
        b, move, mover = c["board"], c["move"], c["mover"]
        best_uci = c["best_pv"][0] if c["best_pv"] else None
        played_is_best = best_uci == move.uci()
        if played_is_best:
            c["played"] = c["best"]
        drop = 0.0 if played_is_best else max(0.0, c["best"].win - c["played"].win)
        forced = c["legal"] == 1
        second_gap = (c["cands"][0]["win"] - c["cands"][1]["win"]) if len(c["cands"]) > 1 else 100.0
        low_reason = None
        if forced:
            low_reason = "forced"
        elif c["best"].win >= DECIDED and c["played"].win >= DECIDED or c["best"].win <= 100 - DECIDED:
            low_reason = "decided"
        elif (prev_move is not None and b.is_capture(move) and move.to_square == prev_move.to_square
              and played_is_best and second_gap >= 15):
            low_reason = "recapture"
        elif played_is_best and second_gap >= 15 and _takes_piece(b, move):
            low_reason = "obvious_capture"  # taking a piece that any alternative would leave en prise
        c.update(drop=drop, played_is_best=played_is_best, second_gap=second_gap, low_reason=low_reason)
        c["phase"] = F.phase(b)
        c["before"] = F.snapshot(b, mover)
        c["after_played"] = F.snapshot(c["after"], mover)
        best_board = None
        if best_uci:
            best_board = b.copy(stack=False)
            best_board.push(chess.Move.from_uci(best_uci))
        c["best_board"] = best_board
        c["after_best"] = F.snapshot(best_board, mover) if best_board else c["after_played"]
        c["played_line_delta"] = line_material_delta(b, [move.uci()] + list(c["reply_pv"]), mover)
        c["best_line_delta"] = line_material_delta(b, list(c["best_pv"]), mover)
        c["themes"] = situation_themes(c) if c["is_player"] else []
        prev_move = move

    from .findings import detect_findings  # local import: findings uses the helpers above
    for i, c in enumerate(out.contexts):
        out.plies.append(_ply_record(i, c, player))
        if c["is_player"]:
            for f in detect_findings(c):
                f["ply"] = i
                out.findings.append(f)
    out.summary = summarize(out, player)
    return out


def _takes_piece(board: chess.Board, move: chess.Move) -> bool:
    victim = board.piece_at(move.to_square)
    return victim is not None and F.VALUES[victim.piece_type] >= 3


def situation_themes(c: dict) -> list[str]:
    """What kind of decision this was, independent of how it went. Used to match like decisions."""
    b, ph, before = c["board"], c["phase"], c["before"]
    themes = []
    best_mate = c["best"].mate
    if (c["best_line_delta"] >= 2 or c["played_line_delta"] <= -2
            or (best_mate is not None and 0 < best_mate <= 5) or (c["played"].mate or 0) < 0):
        themes.append("tactic")
    if ph == "opening":
        if before["undeveloped"] >= 2:
            themes.append("opening.development")
        if not before["castled"] and before["queens_on"]:
            themes.append("opening.king_safety")
    elif ph == "middlegame":
        if any(F.creates_pawn_contact(b, m) for m in b.legal_moves):
            themes.append("middlegame.pawn_break")
        if "tactic" not in themes:
            themes.append("middlegame.piece_activity")
    else:
        if before["passed"] or before["opp_passed"]:
            themes.append("endgame.passed_pawn")
        if not before["queens_on"]:
            themes.append("endgame.king_activity")
        if c["best"].win >= 75:
            themes.append("endgame.conversion")
        elif c["best"].win < 50:
            themes.append("endgame.defense")
    return themes


def _pov(score: Score, mover: bool, player: bool) -> Score:
    return score if mover == player else score.neg()


def _ply_record(i: int, c: dict, player: bool) -> dict:
    b, mover = c["board"], c["mover"]
    best = _pov(c["best"], mover, player)
    played = _pov(c["played"], mover, player)
    best_uci = c["best_pv"][0] if c["best_pv"] else None
    deep = c["deep"]
    return {
        "ply": i,
        "move_number": b.fullmove_number,
        "side": "white" if mover == chess.WHITE else "black",
        "is_player": c["is_player"],
        "fen_before": b.fen(),
        "position_key": F.position_key(b),
        "move_uci": c["move"].uci(),
        "move_san": b.san(c["move"]),
        "legal_moves": c["legal"],
        "best_uci": best_uci,
        "best_san": san_line(b, [best_uci], 1)[0] if best_uci else None,
        "best_cp": best.cp, "best_mate": best.mate,
        "played_cp": played.cp, "played_mate": played.mate,
        "win_best": round(best.win, 2), "win_played": round(played.win, 2),
        "win_drop": round(c["drop"], 2),
        "classification": classify(c["drop"], c["played_is_best"], c["legal"] == 1),
        "candidates": c["cands"],
        "reply_line": san_line(c["after"], list(c["reply_pv"])),
        "deep_checked": deep is not None,
        "stable": deep["stable"] if deep else True,
        "phase": c["phase"],
        "material_key": F.material_key(b, mover),
        "pawn_key": F.pawn_key(b, mover),
        "material_balance": F.material_balance(b, mover),
        "low_info": c["low_reason"] is not None,
        "low_info_reason": c["low_reason"],
        "themes": c["themes"],
        "features": {
            "before": c["before"], "after_played": c["after_played"], "after_best": c["after_best"],
            "played_line_delta": c["played_line_delta"], "best_line_delta": c["best_line_delta"],
            "second_gap": round(c["second_gap"], 1),
            "deep": None if not deep else {
                "first_drop": deep["first_drop"], "deep_drop": deep["deep_drop"], "stable": deep["stable"]},
        },
    }


def accuracy_from_drop(drop: float) -> float:
    """Per-move accuracy on a 0-100 scale from the win-% given away (Lichess-style curve)."""
    return max(0.0, min(100.0, 103.1668 * 2.718281828 ** (-0.04354 * drop) - 3.1669))


def summarize(out: GameAnalysis, player: bool) -> dict:
    mine = [p for p in out.plies if p["is_player"]]
    meaningful = [p for p in mine if not p["low_info"]]
    by_phase = {}
    for ph in ("opening", "middlegame", "endgame"):
        rows = [p for p in meaningful if p["phase"] == ph]
        by_phase[ph] = {
            "decisions": len(rows),
            "avg_drop": round(mean(p["win_drop"] for p in rows), 2) if rows else None,
            "mistakes": sum(1 for p in rows if p["classification"] in ("mistake", "blunder")),
        }
    counts = {}
    for p in mine:
        counts[p["classification"]] = counts.get(p["classification"], 0) + 1
    return {
        "player_moves": len(mine),
        "meaningful_decisions": len(meaningful),
        "low_info": len(mine) - len(meaningful),
        "avg_drop": round(mean(p["win_drop"] for p in meaningful), 2) if meaningful else None,
        "accuracy": round(mean(accuracy_from_drop(p["win_drop"]) for p in meaningful), 1) if meaningful else None,
        "classifications": counts,
        "by_phase": by_phase,
        "findings": len(out.findings),
        "deep_checks": sum(1 for p in mine if p["deep_checked"]),
        "unstable": sum(1 for p in mine if not p["stable"]),
    }
