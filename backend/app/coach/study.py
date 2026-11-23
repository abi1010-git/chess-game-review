"""The study coach: pick whole games worth studying and build spoiler-free guided checkpoints.

A good study game shows a decision the player is likely to face again (a theme they have gone
wrong in before), with enough real decisions to be worth replaying and a clear learning
objective. When a candidate is too forced or its key moment is unclear (the engine's quick and
deep checks disagreed and no finding explains it), the coach asks for another example, up to a
fixed number of times.

Answers (best move, evaluation, explanation) are stored with the study but only sent to the
player for checkpoints they choose to reveal.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import chess
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..analysis import INACCURACY, MISTAKE
from ..compare import THEME_LABELS, Decision, load_decisions, situation_for
from ..models import Finding, Game, MoveEval, Study
from .llm import Router
from .schemas import Checkpoint, CheckpointAnswer, ModelFeedback, ModelStudyText, StudyPick, TraceStep
from .tools import engine_lines, load_context

MAX_EXAMPLES = 5  # candidate games the coach looks at before giving up
MIN_DECISIONS = 8  # fewer real decisions than this: too forced to be worth a whole-game study
MAX_CHECKPOINTS = 3
SAN_RE = re.compile(r"\b(?:O-O(?:-O)?|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h](?:x[a-h])?[1-8](?:=[QRBN])?)[+#]?")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")

THEME_HINTS = {
    "tactic": "Before you choose, check every capture, check and threat, for both sides.",
    "opening.development": "Which of your pieces are still at home, and where do they belong?",
    "opening.king_safety": "How safe is your king, and what would it take to secure it?",
    "middlegame.pawn_break": "Which pawn move would open the position, and is now the right time?",
    "middlegame.piece_activity": "Which of your pieces is doing the least, and how can it join the game?",
    "endgame.passed_pawn": "Which passed pawns matter most here, yours or your opponent's?",
    "endgame.king_activity": "Where does your king belong in this endgame?",
    "endgame.conversion": "You are better here. What is the safest way to make progress?",
    "endgame.defense": "You are worse here. What is your opponent threatening, and how do you hold?",
}


def _step(action: str, **detail) -> dict:
    return TraceStep(actor="study_coach", action=action, detail=detail).model_dump()


def _label(theme: str | None) -> str:
    return THEME_LABELS.get(theme or "", "Key decisions")


# ---------------------------------------------------------------- choosing a game

def find_study_game(decisions: list[Decision], profile_id: int, theme: str | None, exclude: set[int],
                    session: Session | None = None) -> tuple[dict | None, list[dict]]:
    """Pick one whole game that illustrates `theme`. Returns the pick and the coach's trace."""
    by_game: dict[int, list[Decision]] = defaultdict(list)
    for d in decisions:
        by_game[d.game_id].append(d)
    met = {gid for gid, ds in by_game.items() if any(theme in d.themes and not d.low_info for d in ds)} if theme else set()
    went_wrong = {gid for gid in met if any(theme in d.themes and d.drop >= MISTAKE and not d.low_info
                                            for d in by_game[gid])}
    rate = (len(went_wrong) / len(met)) if met else None

    cands = []
    for gid, ds in by_game.items():
        if gid in exclude:
            continue
        keys = [d for d in ds if not d.low_info and d.drop >= MISTAKE and (theme is None or theme in d.themes)]
        if not keys:
            continue
        key = max(keys, key=lambda d: d.drop)
        cands.append((gid, key, ds))
    cands.sort(key=lambda x: -(x[1].played_at.timestamp() if x[1].played_at else 0))

    steps = [_step("tool:find_candidates", theme=theme, candidates=len(cands))]
    for gid, key, ds in cands[:MAX_EXAMPLES]:
        meaningful = sum(1 for d in ds if not d.low_info)
        steps.append(_step("propose", game_id=gid, key_ply=key.ply, decisions=meaningful))
        reason = None
        if meaningful < MIN_DECISIONS:
            reason = f"too forced: only {meaningful} real decisions"
        elif session is not None:
            row = session.scalar(select(MoveEval).where(MoveEval.game_id == gid, MoveEval.ply == key.ply,
                                                        MoveEval.analysis_version == config.ANALYSIS_VERSION))
            has_finding = session.scalar(select(Finding.id).where(Finding.game_id == gid, Finding.ply == key.ply)
                                         .limit(1)) is not None
            if row is None:
                reason = "no stored evidence for the key move"
            elif row.legal_moves < 4:
                reason = "too forced: few legal moves at the key moment"
            elif not row.stable and not has_finding:
                reason = "unclear objective: the engine's checks disagreed and nothing explains the moment"
        if reason:
            steps.append(_step("ask_another_example", game_id=gid, reason=reason))
            continue
        label = _label(theme).lower()
        history = (f" You've met this kind of position in {len(met)} games and went wrong in {len(went_wrong)}."
                   if rate is not None else "")
        pick = {"game_id": gid, "key_ply": key.ply, "move_number": key.move_number,
                "reason": f"A whole game built around {label}, which you can replay from the start.{history}",
                "theme": theme}
        steps.append(_step("accept", game_id=gid))
        return pick, steps
    steps.append(_step("no_suitable_game", theme=theme))
    return None, steps


def study_picks(s: Session, profile_id: int, limit: int = 2) -> dict:
    """Spoiler-free recommendations for the home screen: one or two whole games to study."""
    from ..dashboard import dashboard

    decisions = load_decisions(s, profile_id)
    studied = set(s.scalars(select(Study.game_id).where(Study.profile_id == profile_id)))
    d = dashboard(s, profile_id)
    themes: list[str | None] = []
    for item in d.get("recurring", []):
        sit = item.get("situation")
        if item["status"] in ("recurring", "emerging") and sit and sit not in themes:
            themes.append(sit)
    themes.append(None)  # fall back to the most instructive recent game of any kind
    picks, trace, used = [], [], set(studied)
    for theme in themes:
        if len(picks) >= limit:
            break
        pick, steps = find_study_game(decisions, profile_id, theme, used, s)
        trace += steps
        if not pick:
            continue
        g = s.get(Game, pick["game_id"])
        used.add(g.id)
        picks.append({**pick, "theme_label": _label(theme), "opponent": g.opponent_name, "result": g.player_result,
                      "played_at": g.played_at, "time_class": g.time_class, "color": g.player_color,
                      "start_move": max(1, pick["move_number"] - 3)})
    for p in picks:  # the exact moment stays a surprise until the study starts
        p.pop("key_ply", None)
        p.pop("move_number", None)
    saved = [{"id": st.id, "game_id": st.game_id, "title": st.title, "theme": st.theme,
              "checkpoints": len(st.checkpoints), "revealed": len(st.revealed), "notes": len(st.notes),
              "updated_at": st.updated_at}
             for st in s.scalars(select(Study).where(Study.profile_id == profile_id).order_by(Study.updated_at.desc())
                                 .limit(10))]
    return {"picks": picks, "saved": saved, "trace": trace}


# ---------------------------------------------------------------- building a study

def build_study(s: Session, game: Game, theme: str | None, router: Router | None) -> tuple[StudyPick, str, list]:
    """Build the checkpoints and spoiler-free text for one game. Returns (pick, provider, trace)."""
    ctx = load_context(s, game)
    if ctx is None:
        raise ValueError("This game has not been analyzed yet.")
    mine = [p for p in ctx.plies if p.is_player and not p.low_info]
    if not mine:
        raise ValueError("Every move in this game was forced or already decided, so there is nothing to study.")

    def theme_of(p: MoveEval) -> str | None:
        fs = ctx.by_ply.get(p.ply, [])
        return situation_for(fs[0].theme if fs else None, list(p.themes or []))

    keyed = [p for p in mine if p.win_drop >= MISTAKE and (theme is None or theme in (p.themes or [])
                                                           or theme_of(p) == theme)]
    key = max(keyed or mine, key=lambda p: p.win_drop)
    others = sorted((p for p in mine if p.ply != key.ply and (p.win_drop >= INACCURACY or ctx.by_ply.get(p.ply))),
                    key=lambda p: (p.phase == key.phase, -p.win_drop))
    chosen = [key] + others[:MAX_CHECKPOINTS - 1]
    chosen.sort(key=lambda p: p.ply)
    trace = [_step("build_checkpoints", key_ply=key.ply, plies=[p.ply for p in chosen])]

    checkpoints = []
    for p in chosen:
        t = theme_of(p)
        fs = ctx.by_ply.get(p.ply, [])
        line = engine_lines(ctx, p.ply)
        expl = fs[0].summary if fs else (
            f"{p.best_san} kept {p.win_best:.0f}% expected score; {p.move_san} left {p.win_played:.0f}%."
            if p.best_san and p.best_san != p.move_san else "Your move matched the engine's choice.")
        hint = THEME_HINTS.get(t or "", "Look at what both sides want before you decide.")
        checkpoints.append(Checkpoint(
            ply=p.ply, move_number=p.move_number, side=p.side, fen=p.fen_before,
            prompt=f"Move {p.move_number}, your move. {hint} Name a plan or two, choose a move, "
                   f"and predict your opponent's reply.",
            answer=CheckpointAnswer(
                played=p.move_san, best=p.best_san, best_line=line,
                candidates=[{"san": c.get("san"), "win": round(c.get("win", 0), 1)} for c in (p.candidates or [])[:3]],
                win_before=round(p.win_best, 1), win_after=round(p.win_played, 1), verdict=p.classification,
                explanation=expl)))

    label = _label(theme)
    pick = StudyPick(
        game_id=game.id, theme=theme,
        title=f"{label}: vs {game.opponent_name or 'Unknown'}",
        rationale=(f"This game has {len(mine)} real decisions and a turning point in {label.lower()}, a kind of "
                   f"position you are likely to meet again. Replay it from the start and stop at each checkpoint "
                   f"before you look at what happened."),
        objective=f"Find the right idea in {label.lower()} before the game shows you.",
        start_ply=max(0, chosen[0].ply - 6),
        checkpoints=checkpoints)

    provider = None
    if router is not None and router.enabled:
        facts = {"theme": label, "decisions": len(mine), "opponent": game.opponent_name, "result": game.player_result,
                 "time_control": game.time_class, "opening": game.opening,
                 "checkpoints": [{"move_number": c.move_number, "phase": ctx.plies[c.ply].phase,
                                  "hint": THEME_HINTS.get(theme_of(ctx.plies[c.ply]) or "", "")} for c in checkpoints]}
        numbers = set(NUM_RE.findall(json.dumps(facts)))

        def check(v: ModelStudyText) -> str | None:
            for text in [v.title, v.rationale, v.objective, *v.prompts]:
                if SAN_RE.search(text):
                    return "named a move (that would give the answer away)"
                bad = [n for n in NUM_RE.findall(text) if n not in numbers and float(n) > 3]
                if bad:
                    return f"used a number not in the facts ({bad[0]})"
            if v.prompts and len(v.prompts) != len(checkpoints):
                return "wrong number of prompts"
            return None

        res = router.complete(
            "write",
            "You are a chess coach preparing a study game for a club player. Explain why this whole game is "
            "worth their time and write one short question per checkpoint. NEVER name a chess move or square, "
            "and never hint at the answer: the player must find it themselves. Use only the facts given. "
            'Reply with JSON only: {"title": "...", "rationale": "...", "objective": "...", "prompts": ["..."]}',
            json.dumps(facts, default=str), ModelStudyText, check=check)
        if res:
            pick.title, pick.rationale, pick.objective = res.value.title, res.value.rationale, res.value.objective
            for c, ptxt in zip(pick.checkpoints, res.value.prompts):
                c.prompt = f"Move {c.move_number}, your move. {ptxt}"
            provider = res.provider
            trace.append(TraceStep(actor="router", action="model:study_text", detail={
                "provider": res.provider, "attempts": [a.__dict__ for a in res.attempts]}).model_dump())
        else:
            trace.append(TraceStep(actor="router", action="model:study_text_failed").model_dump())
    return pick, provider, trace


# ---------------------------------------------------------------- the player's notes and feedback

def parse_move(fen: str, text: str | None) -> str | None:
    """A legal move in SAN, from SAN or UCI text; None when empty. Raises ValueError when illegal."""
    if not text or not text.strip():
        return None
    b = chess.Board(fen)
    t = text.strip()
    try:
        return b.san(b.parse_san(t))
    except ValueError:
        pass
    try:
        m = chess.Move.from_uci(t.lower())
        if m in b.legal_moves:
            return b.san(m)
    except ValueError:
        pass
    raise ValueError(f"{t} is not a legal move in this position.")


def reply_position(fen: str, san: str) -> str:
    b = chess.Board(fen)
    b.push_san(san)
    return b.fen()


def rule_feedback(cp: dict, note: dict) -> str:
    a = cp["answer"]
    best, played = a.get("best"), a.get("played")
    chosen, reply = note.get("move"), note.get("reply")
    parts = []
    if not chosen:
        parts.append("You didn't choose a move, so here is what the engine found.")
    elif best and chosen == best:
        parts.append(f"{chosen} is the engine's top move.")
    elif chosen == played:
        parts.append(f"{chosen} is what you played in the game; the engine preferred {best}."
                     if best and best != played else f"{chosen} is what you played, and it was a good choice.")
    else:
        alt = next((c for c in a.get("candidates", []) if c.get("san") == chosen), None)
        top = next((c for c in a.get("candidates", []) if c.get("san") == best), None)
        if alt and top:
            parts.append(f"{chosen} is one of the engine's top choices ({alt['win']:.0f}% expected score, "
                         f"against {top['win']:.0f}% for {best}).")
        else:
            parts.append(f"The engine did not rank {chosen} among its top moves; it chose {best}.")
    line = a.get("best_line") or []
    if reply and len(line) > 1:
        parts.append("You predicted the opponent's best reply too." if reply == line[1]
                     else f"After {line[0]}, the engine expects {line[1]} as the reply.")
    parts.append(a.get("explanation") or "")
    return " ".join(p for p in parts if p)


def model_feedback(router: Router | None, cp: dict, note: dict) -> tuple[str | None, str | None]:
    if router is None or not router.enabled or not (note.get("plans") or note.get("text")):
        return None, None
    a = cp["answer"]
    allowed = {x.rstrip("+#") for x in [a.get("best"), a.get("played"), note.get("move"), note.get("reply"),
                                         *a.get("best_line", []), *[c.get("san") for c in a.get("candidates", [])]] if x}
    facts = {"player_plans": note.get("plans"), "player_notes": note.get("text"), "player_move": note.get("move"),
             "player_predicted_reply": note.get("reply"), "engine_best": a.get("best"),
             "engine_line": a.get("best_line", [])[:6], "move_played_in_game": a.get("played"),
             "expected_score_before_pct": round(a.get("win_before", 0)),
             "expected_score_after_game_move_pct": round(a.get("win_after", 0)),
             "engine_top_moves": a.get("candidates", []), "standard_explanation": a.get("explanation"),
             "standard_feedback": rule_feedback(cp, note)}
    numbers = set(NUM_RE.findall(json.dumps(facts, default=str)))

    def check(v: ModelFeedback) -> str | None:
        for tok in SAN_RE.findall(v.feedback):
            if tok.rstrip("+#") not in allowed:
                return f"named a move not in the evidence ({tok})"
        for n in NUM_RE.findall(v.feedback):
            if n not in numbers and str(int(float(n))) not in numbers and float(n) > 3:
                return f"used a number not in the evidence ({n})"
        return None

    res = router.complete(
        "write",
        "You are a chess coach giving feedback on a player's thinking at one moment of their own game. "
        "Respond to their plans and notes, then explain the engine's evidence in plain language. Use only "
        "the facts; never name a move outside them and never invent numbers. Be encouraging and specific. "
        'Reply with JSON only: {"feedback": "..."}',
        json.dumps(facts, default=str), ModelFeedback, check=check)
    return (res.value.feedback, res.provider) if res else (None, None)
