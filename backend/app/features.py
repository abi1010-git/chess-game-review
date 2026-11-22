"""Explicit board features used as evidence for findings and for matching similar positions.

Everything here is plain python-chess and deterministic. Features are measured for one color
("us"), so the same function describes the player's side whether they played white or black.
"""
from __future__ import annotations

import math

import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_LETTERS = [(chess.QUEEN, "Q"), (chess.ROOK, "R"), (chess.BISHOP, "B"), (chess.KNIGHT, "N"), (chess.PAWN, "P")]
MINOR_HOME = {
    chess.WHITE: {chess.KNIGHT: [chess.B1, chess.G1], chess.BISHOP: [chess.C1, chess.F1]},
    chess.BLACK: {chess.KNIGHT: [chess.B8, chess.G8], chess.BISHOP: [chess.C8, chess.F8]},
}
CENTER = [chess.D4, chess.E4, chess.D5, chess.E5]


def material(board: chess.Board, color: bool) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values() if p.color == color)


def non_pawn_material(board: chess.Board, color: bool | None = None) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values()
               if p.piece_type not in (chess.PAWN, chess.KING) and (color is None or p.color == color))


def material_balance(board: chess.Board, color: bool) -> int:
    return material(board, color) - material(board, not color)


def material_key(board: chess.Board, us: bool) -> str:
    """Piece counts, our side first, e.g. 'Q1R2B1N1P6|Q1R2B2N0P6'."""
    def side(c):
        return "".join(f"{l}{len(board.pieces(pt, c))}" for pt, l in PIECE_LETTERS)
    return f"{side(us)}|{side(not us)}"


def _oriented(sq: int, us: bool) -> int:
    return sq if us == chess.WHITE else chess.square_mirror(sq)


def pawn_key(board: chess.Board, us: bool) -> str:
    """Pawn squares seen from our side of the board, so structures compare across colors."""
    ours = sorted(chess.square_name(_oriented(s, us)) for s in board.pieces(chess.PAWN, us))
    theirs = sorted(chess.square_name(_oriented(s, us)) for s in board.pieces(chess.PAWN, not us))
    return ",".join(ours) + "|" + ",".join(theirs)


def position_key(board: chess.Board) -> str:
    """Exact position: placement, side to move, castling and en passant (no move counters)."""
    return " ".join(board.fen(en_passant="legal").split(" ")[:4])


def phase(board: chess.Board) -> str:
    npm = non_pawn_material(board)
    if npm <= 26 or (not board.pieces(chess.QUEEN, chess.WHITE) and not board.pieces(chess.QUEEN, chess.BLACK)
                     and npm <= 32):
        return "endgame"
    ply = (board.fullmove_number - 1) * 2 + (0 if board.turn == chess.WHITE else 1)
    undeveloped = undeveloped_minors(board, chess.WHITE) + undeveloped_minors(board, chess.BLACK)
    if npm >= 46 and (ply < 16 or (ply < 30 and undeveloped >= 3)):
        return "opening"
    return "middlegame"


def undeveloped_minors(board: chess.Board, color: bool) -> int:
    n = 0
    for pt, squares in MINOR_HOME[color].items():
        for sq in squares:
            p = board.piece_at(sq)
            if p and p.color == color and p.piece_type == pt:
                n += 1
    return n


def has_castled_shape(board: chess.Board, color: bool) -> bool:
    k = board.king(color)
    if k is None:
        return False
    rank = 0 if color == chess.WHITE else 7
    return chess.square_rank(k) == rank and chess.square_file(k) in (1, 2, 6, 7)


def pawn_shield(board: chess.Board, color: bool) -> int:
    """Own pawns on the three files around the king, one or two ranks in front of it."""
    k = board.king(color)
    if k is None:
        return 0
    kf, kr = chess.square_file(k), chess.square_rank(k)
    step = 1 if color == chess.WHITE else -1
    n = 0
    for f in range(max(0, kf - 1), min(7, kf + 1) + 1):
        for d in (1, 2):
            r = kr + step * d
            if 0 <= r <= 7:
                p = board.piece_at(chess.square(f, r))
                if p and p.color == color and p.piece_type == chess.PAWN:
                    n += 1
                    break
    return n


def king_center_distance(board: chess.Board, color: bool) -> int:
    k = board.king(color)
    return min(chess.square_distance(k, c) for c in CENTER) if k is not None else 0


def passed_pawns(board: chess.Board, color: bool) -> list[int]:
    out = []
    enemy = board.pieces(chess.PAWN, not color)
    for sq in board.pieces(chess.PAWN, color):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        blocked = False
        for e in enemy:
            ef, er = chess.square_file(e), chess.square_rank(e)
            if abs(ef - f) <= 1 and ((er > r) if color == chess.WHITE else (er < r)):
                blocked = True
                break
        if not blocked:
            out.append(sq)
    return out


def isolated_pawns(board: chess.Board, color: bool) -> int:
    files = [chess.square_file(s) for s in board.pieces(chess.PAWN, color)]
    return sum(1 for f in files if (f - 1) not in files and (f + 1) not in files)


def doubled_pawns(board: chess.Board, color: bool) -> int:
    files = [chess.square_file(s) for s in board.pieces(chess.PAWN, color)]
    return sum(files.count(f) - 1 for f in set(files) if files.count(f) > 1)


def mobility(board: chess.Board, color: bool) -> int:
    """Pseudo-legal moves for `color` (as if it were their turn), excluding pawn and king moves."""
    b = board.copy(stack=False)
    b.turn = color
    b.ep_square = None
    return sum(1 for m in b.pseudo_legal_moves
               if b.piece_type_at(m.from_square) not in (chess.PAWN, chess.KING))


def hanging_value(board: chess.Board, color: bool) -> int:
    """Value of our pieces that are attacked and either undefended or attacked by a cheaper piece."""
    worst = 0
    for sq, p in board.piece_map().items():
        if p.color != color or p.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        defended = bool(board.attackers(color, sq))
        cheapest = min(VALUES[board.piece_type_at(a)] or 100 for a in attackers)
        if not defended or cheapest < VALUES[p.piece_type]:
            worst = max(worst, VALUES[p.piece_type])
    return worst


def forked_targets(board: chess.Board, square: int) -> list[str]:
    """After a move lands on `square`: the enemy king or pieces worth 3+ that the piece now attacks."""
    p = board.piece_at(square)
    if p is None:
        return []
    out = []
    for t in board.attacks(square):
        q = board.piece_at(t)
        if q and q.color != p.color and (q.piece_type == chess.KING or VALUES[q.piece_type] > VALUES[p.piece_type]
                                         or (VALUES[q.piece_type] >= 3 and not board.attackers(q.color, t))):
            out.append(chess.square_name(t))
    return out if len(out) >= 2 else []


def creates_pawn_contact(board: chess.Board, move: chess.Move) -> bool:
    """A pawn move that attacks an enemy pawn or captures one: a pawn break."""
    if board.piece_type_at(move.from_square) != chess.PAWN:
        return False
    color = board.turn
    if board.is_capture(move):
        victim = board.piece_at(move.to_square)
        return victim is None or victim.piece_type == chess.PAWN  # en passant or pawn capture
    after = board.copy(stack=False)
    after.push(move)
    return any(after.piece_type_at(t) == chess.PAWN and after.color_at(t) != color
               for t in after.attacks(move.to_square))


def snapshot(board: chess.Board, us: bool) -> dict:
    """Compact feature record stored with each decision."""
    return {
        "material": material(board, us),
        "opp_material": material(board, not us),
        "balance": material_balance(board, us),
        "undeveloped": undeveloped_minors(board, us),
        "castled": has_castled_shape(board, us),
        "can_castle": board.has_castling_rights(us),
        "shield": pawn_shield(board, us),
        "king_center": king_center_distance(board, us),
        "opp_king_center": king_center_distance(board, not us),
        "passed": len(passed_pawns(board, us)),
        "opp_passed": len(passed_pawns(board, not us)),
        "isolated": isolated_pawns(board, us),
        "doubled": doubled_pawns(board, us),
        "mobility": mobility(board, us),
        "opp_mobility": mobility(board, not us),
        "queens_on": bool(board.pieces(chess.QUEEN, chess.WHITE) or board.pieces(chess.QUEEN, chess.BLACK)),
    }


# ---------------------------------------------------------------- scores

def win_pct(cp: int | None, mate: int | None) -> float:
    """Expected score (0-100) from a centipawn or mate score, same side's point of view."""
    if mate is not None:
        return 100.0 if mate > 0 else 0.0
    cp = max(-1500, min(1500, cp or 0))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def score_text(cp: int | None, mate: int | None) -> str:
    if mate is not None:
        return f"mate in {mate}" if mate > 0 else f"getting mated in {-mate}"
    return f"{(cp or 0) / 100:+.1f}"
