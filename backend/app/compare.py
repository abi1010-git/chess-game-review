"""Compare a game's decisions with the player's own earlier decisions, and explain losses.

Matching starts from exact positions, then the same material and pawn structure, then the same
decision theme in the same phase with similar material. Low-information decisions (forced moves,
obvious recaptures, already-decided positions), the target game and duplicates of it are excluded.
Verdicts always carry their sample size and match quality, and fall back to "insufficient evidence"
or "mixed evidence" rather than overstating a change.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .analysis import INACCURACY, MISTAKE
from .models import AnalysisRun, Finding, Game, MoveEval

MIN_GAMES = 3  # distinct earlier games needed before saying "better", "worse" or "similar"
SHORT_GAME = 6  # fewer meaningful decisions than this: no improvement verdict
THEME_CAP_PER_GAME = 2
THEME_EXAMPLES = 30

THEME_LABELS = {
    "tactic": "Tactical moments",
    "opening.development": "Development in the opening",
    "opening.king_safety": "King safety in the opening",
    "middlegame.pawn_break": "Pawn breaks",
    "middlegame.piece_activity": "Piece activity in quiet positions",
    "endgame.passed_pawn": "Passed pawns in the endgame",
    "endgame.king_activity": "King activity in the endgame",
    "endgame.conversion": "Converting a winning endgame",
    "endgame.defense": "Defending a worse endgame",
}
FINDING_TO_SITUATION = {
    "opening.development": "opening.development",
    "opening.king_exposure": "opening.king_safety",
    "middlegame.pawn_break": "middlegame.pawn_break",
    "middlegame.piece_activity": "middlegame.piece_activity",
    "endgame.passed_pawn": "endgame.passed_pawn",
    "endgame.king_activity": "endgame.king_activity",
    "endgame.conversion": "endgame.conversion",
    "endgame.defense": "endgame.defense",
}


def situation_for(finding_theme: str | None, themes: list[str]) -> str | None:
    if finding_theme:
        if finding_theme.startswith("tactic."):
            return "tactic"
        s = FINDING_TO_SITUATION.get(finding_theme)
        if s and s in themes:
            return s
    return themes[0] if themes else None


@dataclass
class Decision:
    game_id: int
    ply: int
    move_number: int
    side: str
    san: str
    best_san: str | None
    drop: float
    win_best: float
    classification: str
    phase: str
    position_key: str
    material_key: str
    pawn_key: str
    balance: int
    themes: list[str]
    low_info: bool
    played_at: datetime | None
    result: str
    time_class: str
    provider: str
    rating: int | None
    game_hash: str


def _game_hash(g: Game) -> str:
    return hashlib.sha1(f"{g.initial_fen}|{g.moves_uci}".encode()).hexdigest()


def load_decisions(s: Session, profile_id: int, version: str | None = None) -> list[Decision]:
    version = version or config.ANALYSIS_VERSION
    rows = s.execute(
        select(MoveEval, Game).join(Game, Game.id == MoveEval.game_id)
        .where(MoveEval.profile_id == profile_id, MoveEval.analysis_version == version,
               MoveEval.is_player.is_(True))).all()
    hashes: dict[int, str] = {}
    out = []
    for m, g in rows:
        if g.id not in hashes:
            hashes[g.id] = _game_hash(g)
        out.append(Decision(
            game_id=g.id, ply=m.ply, move_number=m.move_number, side=m.side, san=m.move_san,
            best_san=m.best_san, drop=m.win_drop, win_best=m.win_best, classification=m.classification,
            phase=m.phase, position_key=m.position_key, material_key=m.material_key, pawn_key=m.pawn_key,
            balance=m.material_balance, themes=list(m.themes or []), low_info=m.low_info,
            played_at=g.played_at, result=g.player_result, time_class=g.time_class, provider=g.provider,
            rating=g.player_rating, game_hash=hashes[g.id]))
    return out


def _example(d: Decision, match: str) -> dict:
    return {"game_id": d.game_id, "ply": d.ply, "move_number": d.move_number, "side": d.side, "move": d.san,
            "best_move": d.best_san, "win_drop": round(d.drop, 1), "classification": d.classification,
            "match": match, "played_at": d.played_at, "result": d.result, "time_class": d.time_class,
            "provider": d.provider}


def _context_rank(target_game: Game, d: Decision) -> tuple:
    same_pool = d.time_class == target_game.time_class and d.provider == target_game.provider
    rating_gap = abs((d.rating or 0) - (target_game.player_rating or 0)) if d.rating and target_game.player_rating else 9999
    when = -(d.played_at.timestamp() if d.played_at else 0)
    return (0 if same_pool else 1, rating_gap, when)


def earlier_pool(decisions: list[Decision], game: Game, target_hash: str) -> list[Decision]:
    """The player's decisions from games played before this one (never later or undated games)."""
    when = (game.played_at, game.id) if game.played_at else None
    return [d for d in decisions if d.game_id != game.id and d.game_hash != target_hash and not d.low_info
            and (when is None or (d.played_at is not None and (d.played_at, d.game_id) < when))]


def compare_decision(target: Decision, target_game: Game, pool: list[Decision], theme: str | None) -> dict:
    exact = [d for d in pool if d.position_key == target.position_key]
    seen = {(d.game_id, d.ply) for d in exact}
    structure = [d for d in pool if (d.game_id, d.ply) not in seen and d.phase == target.phase
                 and d.material_key == target.material_key and d.pawn_key == target.pawn_key]
    seen |= {(d.game_id, d.ply) for d in structure}
    thematic: list[Decision] = []
    if theme:
        cands = sorted((d for d in pool if (d.game_id, d.ply) not in seen and d.phase == target.phase
                        and theme in d.themes and abs(d.balance - target.balance) <= 2),
                       key=lambda d: _context_rank(target_game, d))
        per_game: dict[int, int] = defaultdict(int)
        for d in cands:
            if per_game[d.game_id] < THEME_CAP_PER_GAME:
                per_game[d.game_id] += 1
                thematic.append(d)
            if len(thematic) >= THEME_EXAMPLES:
                break
    examples = [(d, "exact") for d in exact] + [(d, "structure") for d in structure] + [(d, "theme") for d in thematic]
    quality = "exact" if exact else "structure" if structure else "theme" if thematic else "none"

    ds = [d for d, _ in examples]
    n_games = len({d.game_id for d in ds})
    stats = {
        "examples": len(ds), "games": n_games, "match_quality": quality,
        "by_match": {"exact": len(exact), "structure": len(structure), "theme": len(thematic)},
        "avg_drop": round(mean(d.drop for d in ds), 1) if ds else None,
        "mistake_rate": round(sum(d.drop >= MISTAKE for d in ds) / len(ds), 2) if ds else None,
        "good_rate": round(sum(d.drop < INACCURACY for d in ds) / len(ds), 2) if ds else None,
        "results": _result_counts(ds),
    }
    verdict, text = _verdict(target, stats, theme)
    change = _change_over_time(ds)
    return {"theme": theme, "theme_label": THEME_LABELS.get(theme or "", theme), "verdict": verdict,
            "text": text, "stats": stats, "change": change,
            "examples": [_example(d, m) for d, m in sorted(examples, key=lambda x: (x[1] != "exact", x[1] != "structure",
                                                                                    -(x[0].played_at.timestamp() if x[0].played_at else 0)))][:12]}


def _result_counts(ds: list[Decision]) -> dict:
    out = {"win": 0, "draw": 0, "loss": 0, "unknown": 0}
    for gid, r in {(d.game_id, d.result) for d in ds}:
        out[r] = out.get(r, 0) + 1
    return out


def _verdict(t: Decision, st: dict, theme: str | None) -> tuple[str, str]:
    label = THEME_LABELS.get(theme or "", "this kind of decision").lower()
    n, g = st["examples"], st["games"]
    if g < MIN_GAMES and st["by_match"]["exact"] == 0:
        if n == 0:
            return "insufficient", "No comparable decisions in your other analyzed games yet."
        return "insufficient", (f"Only {n} comparable decision{'s' if n != 1 else ''} from {g} other "
                                f"game{'s' if g != 1 else ''}: not enough to say whether this was typical.")
    cur_bad, cur_good = t.drop >= MISTAKE, t.drop < INACCURACY
    mr, gr = st["mistake_rate"], st["good_rate"]
    sample = f"{n} decision{'s' if n != 1 else ''} in {g} game{'s' if g != 1 else ''}, {st['match_quality']} match"
    if mr >= 0.3 and gr >= 0.3:
        return "mixed", (f"Mixed evidence: in {label} you went wrong {mr:.0%} of the time and played well "
                         f"{gr:.0%} of the time ({sample}).")
    if cur_bad and mr <= 0.25:
        return "worse", f"Worse than usual: you normally handle {label} well (mistakes {mr:.0%} of the time; {sample})."
    if cur_good and mr >= 0.4:
        return "better", f"Better than usual: in {label} you made a mistake {mr:.0%} of the time before ({sample})."
    if cur_bad:
        return "similar", f"Similar to before: {label} went wrong {mr:.0%} of the time in your other games ({sample})."
    return "similar", f"In line with your usual play in {label} (mistakes {mr:.0%} of the time; {sample})."


def _change_over_time(ds: list[Decision]) -> dict | None:
    dated = sorted((d for d in ds if d.played_at), key=lambda d: d.played_at)
    if len(dated) < 6:
        return None
    half = len(dated) // 2
    early, late = dated[:half], dated[half:]
    er = sum(d.drop >= MISTAKE for d in early) / len(early)
    lr = sum(d.drop >= MISTAKE for d in late) / len(late)
    if abs(er - lr) < 0.2:
        direction = "steady"
    else:
        direction = "improving" if lr < er else "slipping"
    return {"direction": direction, "earlier_mistake_rate": round(er, 2), "later_mistake_rate": round(lr, 2),
            "earlier_n": len(early), "later_n": len(late),
            "split_date": dated[half].played_at}


# ---------------------------------------------------------------- report

def game_report(s: Session, game: Game, version: str | None = None) -> dict | None:
    version = version or config.ANALYSIS_VERSION
    run = s.scalar(select(AnalysisRun).where(AnalysisRun.game_id == game.id, AnalysisRun.analysis_version == version))
    if run is None:
        return None
    plies = list(s.scalars(select(MoveEval).where(MoveEval.run_id == run.id).order_by(MoveEval.ply)))
    findings = list(s.scalars(select(Finding).where(Finding.run_id == run.id).order_by(Finding.ply)))
    all_dec = load_decisions(s, game.profile_id, version)
    target_hash = _game_hash(game)
    pool = earlier_pool(all_dec, game, target_hash)
    mine = {d.ply: d for d in all_dec if d.game_id == game.id}
    by_ply = defaultdict(list)
    for f in findings:
        by_ply[f.ply].append(f)

    meaningful = [p for p in plies if p.is_player and not p.low_info]
    # Key decisions: findings first (worst first), then the strongest decision per phase.
    keys: list[int] = []
    for f in sorted(findings, key=lambda f: (f.polarity != "worse", -_drop(plies, f.ply))):
        if f.ply not in keys:
            keys.append(f.ply)
    for ph in ("opening", "middlegame", "endgame"):
        in_phase = [p for p in meaningful if p.phase == ph]
        if in_phase and not any(plies[k].phase == ph for k in keys):
            keys.append(max(in_phase, key=lambda p: p.win_drop).ply)
    keys = keys[:10]

    decisions = []
    for k in keys:
        p = plies[k]
        fs = by_ply.get(k, [])
        theme = situation_for(fs[0].theme if fs else None, list(p.themes or []))
        cmp = compare_decision(mine[k], game, pool, theme) if k in mine else None
        recurrence = theme_recurrence(all_dec, theme, exclude_game=game.id) if theme else None
        decisions.append({"ply": k, "move_number": p.move_number, "side": p.side, "move": p.move_san,
                          "phase": p.phase, "classification": p.classification, "win_drop": p.win_drop,
                          "finding_ids": [f.id for f in fs], "comparison": cmp, "recurrence": recurrence})

    return {
        "run": {"analysis_version": run.analysis_version, "engine": run.engine_name, "settings": run.settings,
                "created_at": run.created_at, "summary": run.summary},
        "plies": [_ply_out(p) for p in plies],
        "final_fen": _final_fen(game),
        "findings": [_finding_out(f) for f in findings],
        "decisions": decisions,
        "verdict": _game_verdict(plies, findings, meaningful),
        "phases": _phase_overview(plies, findings),
        "loss": explain_loss(game, plies, findings, all_dec) if game.player_result == "loss" else None,
    }


def _drop(plies, ply):
    return plies[ply].win_drop if 0 <= ply < len(plies) else 0


def _final_fen(game: Game) -> str:
    import chess
    b = chess.Board(game.initial_fen)
    for u in game.moves_uci.split():
        b.push_uci(u)
    return b.fen()


def _ply_out(p: MoveEval) -> dict:
    return {"ply": p.ply, "move_number": p.move_number, "side": p.side, "is_player": p.is_player,
            "san": p.move_san, "uci": p.move_uci, "fen_before": p.fen_before, "best_san": p.best_san,
            "best_uci": p.best_uci, "best_cp": p.best_cp, "best_mate": p.best_mate, "played_cp": p.played_cp,
            "played_mate": p.played_mate, "win_best": p.win_best, "win_played": p.win_played,
            "win_drop": p.win_drop, "classification": p.classification, "phase": p.phase,
            "low_info_reason": p.low_info_reason, "deep_checked": p.deep_checked, "stable": p.stable,
            "candidates": p.candidates, "reply_line": p.reply_line, "themes": p.themes}


def _finding_out(f: Finding) -> dict:
    return {"id": f.id, "ply": f.ply, "phase": f.phase, "theme": f.theme, "kind": f.kind, "polarity": f.polarity,
            "confidence": f.confidence, "title": f.title, "summary": f.summary, "evidence": f.evidence}


def _game_verdict(plies, findings, meaningful) -> dict:
    mine = [p for p in plies if p.is_player]
    if not meaningful:
        return {"kind": "no_decisions",
                "text": "Every one of your moves in this game was forced or came in an already decided position, "
                        "so there is nothing to judge. The game stays in your history without a verdict.",
                "strongest": None}
    strongest = _strongest(mine, findings)
    if len(meaningful) < SHORT_GAME:
        return {"kind": "short",
                "text": f"Only {len(meaningful)} real decision{'s' if len(meaningful) != 1 else ''} in this game, "
                        "too few for an improvement verdict. Here is the strongest observation instead.",
                "strongest": strongest}
    return {"kind": "full", "text": None, "strongest": strongest}


def _strongest(mine, findings) -> dict | None:
    worse = [f for f in findings if f.polarity == "worse"]
    if worse:
        f = max(worse, key=lambda f: (f.confidence != "uncertain", f.evidence.get("win_drop", 0)))
        return {"ply": f.ply, "title": f.title, "summary": f.summary, "confidence": f.confidence}
    better = [f for f in findings if f.polarity == "better"]
    if better:
        f = better[0]
        return {"ply": f.ply, "title": f.title, "summary": f.summary, "confidence": f.confidence}
    worst = max((p for p in mine if not p.low_info), key=lambda p: p.win_drop, default=None)
    if worst is None:
        return None
    if worst.win_drop < INACCURACY:
        return {"ply": worst.ply, "title": "No clear errors",
                "summary": f"Your biggest slip was {worst.move_san}, worth {worst.win_drop:.0f} win-% points.",
                "confidence": "clear"}
    return {"ply": worst.ply, "title": f"{worst.move_san} was your costliest move",
            "summary": f"{worst.best_san} was stronger ({worst.win_drop:.0f} win-% points).", "confidence": "likely"}


def _phase_overview(plies, findings) -> dict:
    out = {}
    for ph in ("opening", "middlegame", "endgame"):
        rows = [p for p in plies if p.is_player and p.phase == ph]
        real = [p for p in rows if not p.low_info]
        fs = [f for f in findings if f.phase == ph]
        out[ph] = {
            "moves": len(rows), "decisions": len(real),
            "avg_drop": round(mean(p.win_drop for p in real), 1) if real else None,
            "mistakes": sum(1 for p in real if p.classification in ("mistake", "blunder")),
            "findings": len(fs),
            "headline": (fs[0].title if fs else ("No issues found" if real else
                                                  ("Not reached" if not rows else "Only forced or decided moves"))),
        }
    return out


# ---------------------------------------------------------------- loss explanation

def explain_loss(game: Game, plies: list[MoveEval], findings: list[Finding], all_dec: list[Decision]) -> dict:
    mine = [p for p in plies if p.is_player and p.legal_moves > 1]
    fby = defaultdict(list)
    for f in findings:
        fby[f.ply].append(f)
    competitive = [p for p in mine if p.win_best >= 30]
    if not competitive:
        return {"text": "You were worse from the first moves you made, so no single decision explains the loss.",
                "decisive": None, "earlier": None, "missed": None}
    # The decisive mistake is the one that tipped a playable game into a losing one; only when no
    # single move did that do we fall back to the biggest drop.
    tipping = [p for p in competitive if p.win_played < 35]
    decisive = max(tipping or competitive, key=lambda p: p.win_drop)
    if decisive.win_drop < MISTAKE:
        return {"text": "No single move lost this game: the evaluation slipped a little at a time. "
                        "Look at the phase summaries for where most of it went.",
                "decisive": None, "earlier": None, "missed": _missed_chance(mine, decisive.ply, fby)}

    def claim(p: MoveEval, role: str) -> dict:
        fs = fby.get(p.ply, [])
        f = fs[0] if fs else None
        ev = (p.features or {})
        ap, ab = ev.get("after_played", {}), ev.get("after_best", {})
        basis = {"kind": "finding", "finding_id": f.id, "title": f.title, "summary": f.summary} if f else (
            {"kind": "features", "summary": f"Your pieces had {ap.get('mobility')} moves after {p.move_san} "
                                            f"against {ab.get('mobility')} after {p.best_san}."}
            if ap.get("mobility") is not None and ab.get("mobility") is not None
            and ab["mobility"] - ap["mobility"] >= 3 else
            {"kind": "line", "summary": _line_summary(p)})
        return {"role": role, "ply": p.ply, "move_number": p.move_number, "move": p.move_san,
                "best_move": p.best_san, "win_before": round(p.win_best, 0), "win_after": round(p.win_played, 0),
                "win_drop": round(p.win_drop, 1), "stable": p.stable, "basis": basis,
                "theme": situation_for(f.theme if f else None, list(p.themes or []))}

    out = {"decisive": claim(decisive, "decisive")}
    earlier_cands = [p for p in mine if p.ply < decisive.ply and p.win_best >= 40 and p.win_drop >= INACCURACY
                     and not p.low_info]
    earlier = None
    if earlier_cands:
        def reduces_options(p: MoveEval) -> float:
            fs = fby.get(p.ply, [])
            positional = any(f.kind in ("positional", "strategic") and f.polarity == "worse" for f in fs)
            ap = (p.features or {}).get("after_played", {})
            ab = (p.features or {}).get("after_best", {})
            mob = (ab.get("mobility") or 0) - (ap.get("mobility") or 0)
            return p.win_drop + (10 if positional else 0) + max(0, mob)
        earlier = max(earlier_cands, key=reduces_options)
    out["earlier"] = claim(earlier, "earlier") if earlier else None
    out["missed"] = _missed_chance(mine, decisive.ply, fby)

    parts = [f"The game turned on move {decisive.move_number}, {decisive.move_san} "
             f"({decisive.win_best:.0f}% → {decisive.win_played:.0f}% expected score)."]
    if earlier:
        if out["earlier"]["basis"]["kind"] == "line":  # no positional evidence: don't claim more than the score
            parts.append(f"Before that, {earlier.move_number}. {earlier.move_san} had already cost "
                         f"{earlier.win_drop:.0f} win-% points.")
        else:
            parts.append(f"Before that, {earlier.move_number}. {earlier.move_san} had already narrowed your options.")
    if out["missed"]:
        m = out["missed"]
        parts.append(f"Later, {m['move_number']}. {m['best_move']} was a concrete chance to get back into the game.")
    out["text"] = " ".join(parts)

    # Recurrence: only call something a pattern after counting it across comparable games.
    for key in ("decisive", "earlier"):
        c = out.get(key)
        if c and c.get("theme"):
            c["recurrence"] = theme_recurrence(all_dec, c["theme"], exclude_game=game.id)
    return out


def _line_summary(p: MoveEval) -> str:
    line = (p.candidates or [{}])[0].get("line", [])
    if p.candidates and p.candidates[0].get("san") != p.best_san:
        line = []  # first-pass line belongs to a different move than the deep-pass best
    tail = f" The engine's line: {' '.join(line[:6])}." if len(line) > 1 else ""
    return f"{p.best_san} was stronger by {p.win_drop:.0f} win-% points.{tail}"


def _missed_chance(mine: list[MoveEval], after_ply: int, fby) -> dict | None:
    best = None
    for p in mine:
        if p.ply <= after_ply or p.low_info:
            continue
        ev = p.features or {}
        concrete = (ev.get("best_line_delta") or 0) >= 2 or (p.best_mate or 0) > 0
        if p.win_drop >= 15 and concrete and p.win_best >= 35 and (best is None or p.win_drop > best.win_drop):
            best = p
    if best is None:
        return None
    line = (best.candidates or [{}])[0].get("line", [])
    return {"role": "missed", "ply": best.ply, "move_number": best.move_number, "move": best.move_san,
            "best_move": best.best_san, "win_before": round(best.win_best, 0), "win_after": round(best.win_played, 0),
            "win_drop": round(best.win_drop, 1), "stable": best.stable,
            "basis": {"kind": "line", "summary": f"{' '.join(line[:6])} — concrete, "
                                                 f"{'mate' if (best.best_mate or 0) > 0 else 'wins material'}."}}


def theme_recurrence(all_dec: list[Decision], theme: str, exclude_game: int | None = None) -> dict:
    """How often this kind of decision went wrong across all comparable games, wins and draws included."""
    rel = [d for d in all_dec if theme in d.themes and not d.low_info and d.game_id != exclude_game]
    games = defaultdict(list)
    for d in rel:
        games[d.game_id].append(d)
    bad_games = {gid for gid, ds in games.items() if any(d.drop >= MISTAKE for d in ds)}
    by_result = {"win": 0, "draw": 0, "loss": 0, "unknown": 0}
    for gid in bad_games:
        by_result[games[gid][0].result] = by_result.get(games[gid][0].result, 0) + 1
    n_opps = len(rel)
    mistakes = sum(d.drop >= MISTAKE for d in rel)
    rate = mistakes / n_opps if n_opps else 0.0
    if len(bad_games) >= 3 and n_opps >= 8 and rate >= 0.25:
        status = "recurring"
        text = (f"A recurring weakness: it went wrong in {len(bad_games)} of {len(games)} other games "
                f"({by_result['win']} wins, {by_result['draw']} draws, {by_result['loss']} losses).")
    elif bad_games:
        status = "occasional"
        text = (f"Seen in {len(bad_games)} of {len(games)} other games, not often enough to call it a pattern.")
    else:
        status = "not_seen" if games else "no_data"
        text = ("No similar mistakes in your other analyzed games." if games
                else "No other analyzed games have this kind of decision yet.")
    return {"theme": theme, "status": status, "text": text, "games": len(games), "games_with_mistake": len(bad_games),
            "decisions": n_opps, "mistakes": mistakes, "mistake_rate": round(rate, 2), "by_result": by_result}
