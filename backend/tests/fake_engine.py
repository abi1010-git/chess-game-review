"""A tiny deterministic stand-in for Stockfish: one-ply material search plus mate-in-one."""
import chess

from app.engine import Line
from app.features import VALUES


def _material(board: chess.Board, color: bool) -> int:
    return sum(VALUES[p.piece_type] * (1 if p.color == color else -1) for p in board.piece_map().values())


def _key(line: Line) -> tuple:
    """Order lines from the side to move's view: mates for us, then centipawns, then mates against."""
    if line.mate is not None:
        return (2, -line.mate) if line.mate > 0 else (0, -line.mate)
    return (1, line.cp or 0)


class FakeEngine:
    name = "FakeFish 1"

    def __init__(self):
        self.calls = 0
        self.on_call = None  # hook for tests (e.g. cancel mid-analysis)

    def analyse(self, board, nodes, multipv=1, root_moves=None):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        us = board.turn
        scored = []
        for m in board.legal_moves:
            if root_moves and m.uci() not in root_moves:
                continue
            board.push(m)
            if board.is_checkmate():
                line = Line(cp=None, mate=1, pv=[m.uci()])
            elif board.is_stalemate() or not any(board.legal_moves):
                line = Line(cp=0, mate=None, pv=[m.uci()])
            else:
                # Two plies: assume the opponent's reply that is worst for us.
                worst = None
                for r in list(board.legal_moves):
                    board.push(r)
                    if board.is_checkmate():
                        cand = Line(cp=None, mate=-1, pv=[m.uci(), r.uci()])
                    else:
                        cand = Line(cp=100 * _material(board, us), mate=None, pv=[m.uci(), r.uci()])
                    board.pop()
                    if worst is None or _key(cand) < _key(worst):
                        worst = cand
                line = worst
            board.pop()
            scored.append(line)
        scored.sort(key=_key, reverse=True)
        return scored[:multipv]

    def close(self):
        pass
