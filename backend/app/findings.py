"""Deterministic finding detectors for one of the player's decisions.

Each detector needs explicit evidence: an engine line, a board feature, or both. Tactical and
strategic findings may apply to the same move. A finding whose engine verdict was not stable
between the two passes is marked uncertain.
"""
from __future__ import annotations

import chess

from . import features as F
from .analysis import INACCURACY, MISTAKE, ONLY_MOVE_GAP, san_line

PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook",
               chess.QUEEN: "queen", chess.KING: "king"}


def _confidence(c: dict, strong: bool) -> str:
    if c["deep"] is not None and not c["deep"]["stable"]:
        return "uncertain"
    if strong and c["drop"] >= MISTAKE:
        return "clear"
    if strong or c["drop"] >= MISTAKE:
        return "likely"
    return "uncertain"


def _base_evidence(c: dict) -> dict:
    b = c["board"]
    return {
        "move": b.san(c["move"]),
        "best_move": san_line(b, c["best_pv"], 1)[0] if c["best_pv"] else None,
        "best_line": san_line(b, list(c["best_pv"])),
        "reply_line": san_line(c["after"], list(c["reply_pv"])),
        "eval_best": c["best"].text(),
        "eval_played": c["played"].text(),
        "win_best": round(c["best"].win, 1),
        "win_played": round(c["played"].win, 1),
        "win_drop": round(c["drop"], 1),
        "deep_checked": c["deep"] is not None,
        "stable": c["deep"]["stable"] if c["deep"] else None,
    }


def _finding(c, theme, kind, polarity, title, summary, strong=True, **extra) -> dict:
    ev = _base_evidence(c)
    ev.update(extra)
    return {"theme": theme, "kind": kind, "polarity": polarity, "phase": c["phase"], "title": title,
            "summary": summary, "confidence": _confidence(c, strong), "evidence": ev}


def _is_development(b: chess.Board, m: chess.Move) -> bool:
    if b.is_castling(m):
        return True
    p = b.piece_at(m.from_square)
    homes = F.MINOR_HOME[b.turn].get(p.piece_type, []) if p else []
    return m.from_square in homes


def detect_findings(c: dict) -> list[dict]:
    if c["legal"] <= 1:
        return []
    b, move, after = c["board"], c["move"], c["after"]
    us = c["mover"]
    out: list[dict] = []
    drop = c["drop"]
    best_uci = c["best_pv"][0] if c["best_pv"] else None
    best_move = chess.Move.from_uci(best_uci) if best_uci else None
    best_san = b.san(best_move) if best_move else None
    played_san = b.san(move)
    best_mate, played_mate = c["best"].mate, c["played"].mate
    decided = c["low_reason"] == "decided"

    # ---- mate
    if best_mate is not None and 0 < best_mate <= 5 and not (played_mate and played_mate > 0):
        if best_mate <= 3 or drop >= INACCURACY:
            out.append(_finding(c, "tactic.missed_mate", "tactical", "worse", "Missed a forced mate",
                                f"{best_san} forced mate in {best_mate}. {played_san} let the opponent continue.",
                                mate_in=best_mate))
    if played_mate is not None and played_mate < 0 and not (best_mate is not None and best_mate < 0):
        out.append(_finding(c, "tactic.allowed_mate", "tactical", "worse", "Allowed a forced mate",
                            f"After {played_san} the opponent has mate in {-played_mate}. "
                            f"{best_san} avoided it ({c['best'].text()}).", mate_in=-played_mate))
    if (c["played_is_best"] and best_mate is not None and 2 <= best_mate <= 5
            and c["second_gap"] >= ONLY_MOVE_GAP):
        out.append(_finding(c, "tactic.found_mate", "tactical", "better", "Found a forced mate",
                            f"{played_san} starts a forced mate in {best_mate}.", mate_in=best_mate))
    if out:
        return out  # mate verdicts dominate; material counts are beside the point

    tactical = False
    # ---- material tactics (only when the move actually cost something)
    if drop >= INACCURACY and not decided:
        pl, bl = c["played_line_delta"], c["best_line_delta"]
        if pl <= -2 and bl - pl >= 2:
            tactical = True
            out.append(_allowed_tactic(c, played_san, best_san, pl))
        elif bl >= 2 and bl - pl >= 2 and best_move is not None:
            tactical = True
            out.append(_missed_tactic(c, best_move, best_san, played_san, bl))

    ph = c["phase"]
    before, ap, ab = c["before"], c["after_played"], c["after_best"]
    costly = drop >= INACCURACY and not decided

    # ---- opening
    if ph == "opening" and costly:
        if (before["undeveloped"] >= 2 and not _is_development(b, move) and best_move is not None
                and _is_development(b, best_move) and not tactical):
            out.append(_finding(
                c, "opening.development", "positional", "worse", "Delayed development",
                f"With {before['undeveloped']} minor pieces still at home, {played_san} did not develop. "
                f"{best_san} brings a piece into play.", strong=False, undeveloped=before["undeveloped"]))
        king_moved = b.piece_type_at(move.from_square) == chess.KING and not b.is_castling(move)
        lost_castling = before["can_castle"] and not ap["can_castle"] and not b.is_castling(move)
        weakened = ap["shield"] < before["shield"] and not before["castled"]
        if before["queens_on"] and (king_moved or lost_castling or weakened):
            why = ("moved the king before castling" if king_moved else
                   "gave up the right to castle" if lost_castling else
                   f"removed a pawn in front of the king (cover {before['shield']} → {ap['shield']})")
            out.append(_finding(
                c, "opening.king_exposure", "positional", "worse", "Exposed the king early",
                f"{played_san} {why} while queens are on the board. {best_san} kept the king safer.",
                strong=king_moved or drop >= MISTAKE, shield_before=before["shield"], shield_after=ap["shield"],
                king_moved=king_moved, lost_castling=lost_castling))

    # ---- middlegame (pawn structure findings may also appear in other phases)
    if costly and b.piece_type_at(move.from_square) == chess.PAWN:
        new_iso = ap["isolated"] - before["isolated"]
        new_dbl = ap["doubled"] - before["doubled"]
        if (new_iso > 0 or new_dbl > 0) and not (ab["isolated"] > before["isolated"] or ab["doubled"] > before["doubled"]):
            what = "an isolated pawn" if new_iso > 0 else "doubled pawns"
            out.append(_finding(
                c, "middlegame.weakness", "positional", "worse", "Created a pawn weakness",
                f"{played_san} left {what}; {best_san} kept the structure intact.", strong=False,
                isolated=[before["isolated"], ap["isolated"]], doubled=[before["doubled"], ap["doubled"]]))
    if ph == "middlegame":
        if costly and best_move is not None and F.creates_pawn_contact(b, best_move) \
                and not F.creates_pawn_contact(b, move) and not tactical:
            out.append(_finding(
                c, "middlegame.pawn_break", "strategic", "worse", "Missed a pawn break",
                f"{best_san} challenges the opponent's pawns ({c['best'].text()}); {played_san} "
                f"left the structure as it was ({c['played'].text()}).", strong=False))
        elif (c["played_is_best"] and F.creates_pawn_contact(b, move) and c["second_gap"] >= 10
              and c["low_reason"] is None):
            out.append(_finding(
                c, "middlegame.pawn_break", "strategic", "better", "Timely pawn break",
                f"{played_san} was the strongest move, and the alternatives were clearly worse "
                f"(next best {c['second_gap']:.0f} win-% points lower).", second_gap=c["second_gap"]))

    # ---- endgame
    if ph == "endgame":
        passed_from = set(F.passed_pawns(b, us))
        if costly and best_move is not None and best_move.from_square in passed_from \
                and move.from_square not in passed_from:
            out.append(_finding(
                c, "endgame.passed_pawn", "strategic", "worse", "Did not push the passed pawn",
                f"{best_san} advances the passed pawn ({c['best'].text()}); after {played_san} "
                f"it is {c['played'].text()}.", strong=False))
        elif costly and c["reply_pv"]:
            r = chess.Move.from_uci(c["reply_pv"][0])
            if r in after.legal_moves and r.from_square in set(F.passed_pawns(after, not us)):
                out.append(_finding(
                    c, "endgame.passed_pawn", "strategic", "worse", "Let a passed pawn run",
                    f"After {played_san}, the opponent's best reply {after.san(r)} pushes a passed pawn. "
                    f"{best_san} dealt with it.", strong=False))
        elif (c["played_is_best"] and move.from_square in passed_from and not b.is_capture(move)
              and c["second_gap"] >= 10 and c["low_reason"] is None):
            out.append(_finding(
                c, "endgame.passed_pawn", "strategic", "better", "Pushed the passed pawn at the right time",
                f"{played_san} was best, and the alternatives were {c['second_gap']:.0f} win-% points worse.",
                second_gap=c["second_gap"]))
        if (costly and not before["queens_on"] and best_move is not None
                and b.piece_type_at(best_move.from_square) == chess.KING
                and b.piece_type_at(move.from_square) != chess.KING
                and ab["king_center"] < before["king_center"]):
            out.append(_finding(
                c, "endgame.king_activity", "strategic", "worse", "Kept the king passive",
                f"{best_san} activates the king (distance to the centre {before['king_center']} → "
                f"{ab['king_center']}); {played_san} left it where it was.", strong=False,
                king_center=[before["king_center"], ab["king_center"]]))
        if c["best"].win >= 80 and c["played"].win < 65:
            out.append(_finding(
                c, "endgame.conversion", "strategic", "worse", "Let a winning endgame slip",
                f"The position was winning ({c['best'].text()}). After {played_san} it is "
                f"{c['played'].text()}. {best_san} kept the advantage."))
        elif 20 <= c["best"].win < 50 and drop >= MISTAKE and c["played"].win < 20:
            out.append(_finding(
                c, "endgame.defense", "strategic", "worse", "Defensive slip",
                f"The endgame was holdable ({c['best'].text()}) with {best_san}; {played_san} made it "
                f"{c['played'].text()}."))
        elif (c["played_is_best"] and c["best"].win <= 45 and c["second_gap"] >= ONLY_MOVE_GAP
              and c["low_reason"] is None):
            out.append(_finding(
                c, "endgame.defense", "strategic", "better", "Found the only defence",
                f"{played_san} was the one move that held ({c['best'].text()}). The next best lost "
                f"{c['second_gap']:.0f} win-% points.", second_gap=c["second_gap"]))

    # ---- piece activity, only when nothing more specific explains a costly move
    gain = ab["mobility"] - ap["mobility"]
    if ph == "middlegame" and costly and not out and gain >= 6 and gain >= 0.2 * max(1, ap["mobility"]):
        out.append(_finding(
            c, "middlegame.piece_activity", "positional", "worse", "Passive piece placement",
            f"{best_san} gives your pieces {ab['mobility']} moves against {ap['mobility']} after "
            f"{played_san} ({c['best'].text()} vs {c['played'].text()}).", strong=False,
            mobility=[ap["mobility"], ab["mobility"]]))

    # ---- a hard decision handled well
    if (not out and c["played_is_best"] and c["second_gap"] >= ONLY_MOVE_GAP and c["low_reason"] is None):
        out.append(_finding(
            c, f"{ph}.only_move", "tactical" if "tactic" in c["themes"] else "positional", "better",
            "Found the only good move",
            f"{played_san} was the only move that kept the evaluation ({c['best'].text()}); the next best "
            f"was {c['second_gap']:.0f} win-% points worse.", second_gap=c["second_gap"]))
    return out


def _allowed_tactic(c, played_san, best_san, delta) -> dict:
    after, us = c["after"], c["mover"]
    r = chess.Move.from_uci(c["reply_pv"][0]) if c["reply_pv"] else None
    lost = f"loses about {-delta} point{'s' if -delta != 1 else ''} of material"
    if r is not None and r in after.legal_moves:
        reply_san = after.san(r)
        victim = after.piece_at(r.to_square)
        if victim is not None and victim.color == us and victim.piece_type != chess.PAWN \
                and F.hanging_value(after, us) >= F.VALUES[victim.piece_type]:
            name = PIECE_NAMES[victim.piece_type]
            return _finding(c, "tactic.hanging_piece", "tactical", "worse", f"Left the {name} undefended",
                            f"After {played_san}, {reply_san} wins the {name} on {chess.square_name(r.to_square)}. "
                            f"{best_san} kept it.", square=chess.square_name(r.to_square), material=delta)
        b2 = after.copy(stack=False)
        b2.push(r)
        targets = F.forked_targets(b2, r.to_square)
        if targets:
            return _finding(c, "tactic.fork", "tactical", "worse", "Allowed a double attack",
                            f"After {played_san}, {reply_san} attacks {' and '.join(targets)} at once and {lost}.",
                            targets=targets, material=delta)
    return _finding(c, "tactic.material_loss", "tactical", "worse", "Allowed a tactic",
                    f"After {played_san}, the line {' '.join(san_line(after, list(c['reply_pv']), 4))} {lost}. "
                    f"{best_san} avoided it.", material=delta)


def _missed_tactic(c, best_move, best_san, played_san, delta) -> dict:
    b = c["board"]
    move = c["move"]
    if b.is_capture(move) and b.is_capture(best_move) and move.to_square == best_move.to_square:
        # Both moves take the same piece; the difference is what happens afterwards.
        pl = c["played_line_delta"]
        return _finding(c, "tactic.material_loss", "tactical", "worse", "Recaptured the wrong way",
                        f"{best_san} and {played_san} both capture on {chess.square_name(move.to_square)}, but after "
                        f"{played_san} the line {' '.join(san_line(c['after'], list(c['reply_pv']), 4))} leaves you "
                        f"{pl:+d} in material against {delta:+d} after {best_san}.", material=pl - delta)
    victim = b.piece_at(best_move.to_square) if b.is_capture(best_move) else None
    if victim is not None and victim.piece_type != chess.PAWN \
            and F.hanging_value(b, not c["mover"]) >= F.VALUES[victim.piece_type]:
        name = PIECE_NAMES[victim.piece_type]
        return _finding(c, "tactic.missed_material", "tactical", "worse", f"Missed a free {name}",
                        f"{best_san} won the {name} on {chess.square_name(best_move.to_square)}. "
                        f"{played_san} left it.", material=delta)
    bb = c["best_board"]
    targets = F.forked_targets(bb, best_move.to_square) if bb is not None else []
    if targets:
        return _finding(c, "tactic.missed_fork", "tactical", "worse", "Missed a double attack",
                        f"{best_san} attacks {' and '.join(targets)} at once and wins about {delta} points of "
                        f"material. {played_san} missed it.", targets=targets, material=delta)
    return _finding(c, "tactic.missed_material", "tactical", "worse", "Missed a winning line",
                    f"{' '.join(san_line(b, list(c['best_pv']), 5))} wins about {delta} points of material. "
                    f"{played_san} missed it.", material=delta)
