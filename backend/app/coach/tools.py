"""Tools the specialists call. Each reads stored engine evidence; none of them runs the engine.

All tools are deterministic, so the rule-based mode and the model-backed mode see the same
evidence. The model only chooses which tools to call and how to word the result.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import chess
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..analysis import INACCURACY, MISTAKE
from ..compare import THEME_LABELS, Decision, _game_hash, earlier_pool, load_decisions, situation_for
from ..models import AnalysisRun, Finding, Game, MoveEval
from .schemas import CandidateDecision, HistoryMatch, HistoryResult, SearchFeature

MIN_GAMES = 3  # distinct earlier games before a comparison counts as strong
WIDEN = {"exact": "structure", "structure": "theme", "theme": "phase", "phase": None}
FEATURE_LABEL = {"exact": "the exact same position", "structure": "the same material and pawn structure",
                 "theme": "the same kind of decision in the same phase", "phase": "the same kind of decision in any phase"}


@dataclass
class GameContext:
    """Everything the tools need about one game, loaded once per workflow run."""
    game: Game
    plies: list[MoveEval]
    findings: list[Finding]
    decisions: list[Decision]
    pool: list[Decision]
    by_ply: dict[int, list[Finding]] = field(default_factory=dict)

    def ply(self, n: int) -> MoveEval | None:
        return self.plies[n] if 0 <= n < len(self.plies) else None


def load_context(s: Session, game: Game, version: str | None = None) -> GameContext | None:
    version = version or config.ANALYSIS_VERSION
    run = s.scalar(select(AnalysisRun).where(AnalysisRun.game_id == game.id, AnalysisRun.analysis_version == version))
    if run is None:
        return None
    plies = list(s.scalars(select(MoveEval).where(MoveEval.run_id == run.id).order_by(MoveEval.ply)))
    findings = list(s.scalars(select(Finding).where(Finding.run_id == run.id).order_by(Finding.ply)))
    decisions = load_decisions(s, game.profile_id, version)
    target_hash = _game_hash(game)
    pool = earlier_pool(decisions, game, target_hash)
    by_ply: dict[int, list[Finding]] = defaultdict(list)
    for f in findings:
        by_ply[f.ply].append(f)
    return GameContext(game, plies, findings, decisions, pool, dict(by_ply))


# ---------------------------------------------------------------- reviewer tools

def candidate_decisions(ctx: GameContext, limit: int = 8) -> list[CandidateDecision]:
    """Decisions worth comparing: findings first (worst first), then the costliest move per phase."""
    meaningful = [p for p in ctx.plies if p.is_player and not p.low_info]
    order: list[int] = []
    for f in sorted(ctx.findings, key=lambda f: (f.polarity != "worse", f.confidence == "uncertain",
                                                  -ctx.plies[f.ply].win_drop)):
        if f.ply not in order and not ctx.plies[f.ply].low_info:
            order.append(f.ply)
    for ph in ("opening", "middlegame", "endgame"):
        # Only real decisions: something was at stake, and not the first few book-like moves.
        in_phase = [p for p in meaningful if p.phase == ph and p.ply not in order
                    and p.win_drop >= INACCURACY and p.ply >= 8]
        if in_phase and not any(ctx.plies[k].phase == ph for k in order):
            order.append(max(in_phase, key=lambda p: p.win_drop).ply)
    out = []
    for k in order[:limit]:
        p = ctx.plies[k]
        fs = ctx.by_ply.get(k, [])
        f = fs[0] if fs else None
        out.append(CandidateDecision(
            ply=k, move_number=p.move_number, side=p.side, move=p.move_san, best_move=p.best_san,
            phase=p.phase, win_drop=round(p.win_drop, 1), classification=p.classification,
            theme=situation_for(f.theme if f else None, list(p.themes or [])),
            finding_title=f.title if f else None, finding_summary=f.summary if f else None))
    return out


def engine_lines(ctx: GameContext, ply: int) -> list[str]:
    """The engine's best continuation from the position before the move (SAN)."""
    p = ctx.ply(ply)
    if p is None or not p.candidates:
        return []
    first = p.candidates[0]
    if first.get("san") != p.best_san:  # deep pass changed its mind; the stored line is for another move
        return [p.best_san] if p.best_san else []
    return list(first.get("line") or [])[:8]


def board_features(ctx: GameContext, ply: int) -> dict:
    """Mobility, king safety and material after the played move compared with the best move."""
    p = ctx.ply(ply)
    if p is None:
        return {}
    ev = p.features or {}
    ap, ab = ev.get("after_played", {}) or {}, ev.get("after_best", {}) or {}
    out = {"material_balance": p.material_balance, "phase": p.phase}
    for key in ("mobility", "opp_mobility", "balance", "undeveloped", "shield", "king_center", "passed",
                "opp_passed", "isolated", "doubled"):
        if key in ap or key in ab:
            out[key] = {"after_played": ap.get(key), "after_best": ab.get(key)}
    return out


# ---------------------------------------------------------------- history specialist tools

def search_history(ctx: GameContext, ply: int, feature: SearchFeature, theme: str | None) -> HistoryResult:
    """Earlier decisions comparable to this one, using one matching feature."""
    target = next((d for d in ctx.decisions if d.game_id == ctx.game.id and d.ply == ply), None)
    p = ctx.ply(ply)
    if target is None or p is None:
        return HistoryResult(ply=ply, feature=feature, match_quality="none", games=0, decisions=0,
                             mistake_rate=None, gaps=["This move is not one of your decisions."],
                             strong_enough=False, next_step="narrow")
    pool = ctx.pool
    if feature == "exact":
        hits = [d for d in pool if d.position_key == target.position_key]
    elif feature == "structure":
        hits = [d for d in pool if d.phase == target.phase and d.material_key == target.material_key
                and d.pawn_key == target.pawn_key]
    elif feature == "theme":
        hits = [d for d in pool if theme and theme in d.themes and d.phase == target.phase
                and abs(d.balance - target.balance) <= 2]
    elif theme:  # phase, with a theme: the same kind of decision in any phase
        hits = [d for d in pool if theme in d.themes]
    else:  # phase, without a theme: any decision in this phase with similar material
        hits = [d for d in pool if d.phase == target.phase and abs(d.balance - target.balance) <= 1]
    # Prefer the same time control and platform, then recent games; at most 2 decisions per game.
    same = lambda d: d.time_class == ctx.game.time_class and d.provider == ctx.game.provider  # noqa: E731
    hits.sort(key=lambda d: (not same(d), -(d.played_at.timestamp() if d.played_at else 0)))
    per_game: dict[int, int] = defaultdict(int)
    kept: list[Decision] = []
    for d in hits:
        if per_game[d.game_id] < 2:
            per_game[d.game_id] += 1
            kept.append(d)
    games = len(per_game)
    rate = round(sum(d.drop >= MISTAKE for d in kept) / len(kept), 2) if kept else None

    gaps = []
    if not kept:
        gaps.append(f"None of your other analyzed games had {FEATURE_LABEL[feature]}.")
    elif games < MIN_GAMES:
        gaps.append(f"Only {games} other game{'s' if games != 1 else ''} had {FEATURE_LABEL[feature]}; "
                    f"{MIN_GAMES} are needed to compare.")
    other_pool = sum(1 for d in kept if not same(d))
    if kept and other_pool > len(kept) / 2:
        gaps.append("Most matches come from a different time control or platform.")
    if feature in ("theme", "phase"):
        gaps.append("The positions are similar in kind, not identical.")

    strong = games >= MIN_GAMES  # distinct earlier games: two decisions from one game are not a pattern
    nxt = next_feature(feature, theme)
    if strong:
        step, widen = "accept", None
    elif nxt is not None:
        step, widen = "widen", nxt
    else:
        step, widen = "narrow", None
    return HistoryResult(
        ply=ply, feature=feature, match_quality=feature if kept else "none", games=games, decisions=len(kept),
        mistake_rate=rate, strong_enough=strong, next_step=step, widen_to=widen, gaps=gaps,
        matches=[HistoryMatch(game_id=d.game_id, ply=d.ply, move=d.san, best_move=d.best_san,
                              win_drop=round(d.drop, 1), match=feature,
                              played_at=d.played_at.date().isoformat() if d.played_at else None,
                              result=d.result) for d in kept[:8]])


def next_feature(feature: SearchFeature, theme: str | None) -> SearchFeature | None:
    """The next, looser search after a weak one. Theme searches need a theme to search for."""
    nxt = WIDEN[feature]
    if nxt == "theme" and not theme:
        nxt = "phase"
    return nxt


def theme_label(theme: str | None) -> str:
    return THEME_LABELS.get(theme or "", "this kind of decision")


# ---------------------------------------------------------------- verifier helpers

def legal_san_tokens(fen: str, line: list[str]) -> set[str]:
    """SAN moves that are legal along a line from a position (used to check model text)."""
    out: set[str] = set()
    try:
        b = chess.Board(fen)
    except ValueError:
        return out
    for san in line:
        try:
            m = b.parse_san(san)
        except ValueError:
            break
        out.add(b.san(m))
        b.push(m)
    return out


def verdict_for(drop: float, result: HistoryResult) -> str:
    """Rule-based verdict for a decision against its comparison set."""
    if not result.strong_enough or result.mistake_rate is None:
        return "insufficient"
    bad, good = drop >= MISTAKE, drop < INACCURACY
    mr = result.mistake_rate
    if bad and mr <= 0.25:
        return "worse"
    if good and mr >= 0.4:
        return "better"
    if 0.3 <= mr and not bad and not good:
        return "mixed"
    return "similar"
