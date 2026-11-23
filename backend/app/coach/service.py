"""Glue between the API and the specialists: caching, per-visitor model limits, and safe output.

The LangGraph workflow is imported lazily, so the API and the analysis worker don't pay for it
until someone asks for a coach review.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

import chess
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import config
from ..models import CoachReview, Game, Study, utcnow
from . import study as study_mod
from .llm import Router, get_router

MODEL_RUNS_PER_HOUR = int(os.environ.get("COACH_MODEL_RUNS_PER_HOUR", 20))


class VisitorLimiter:
    """Caps model-backed runs per visitor so one person can't use up the shared free quota."""

    def __init__(self, per_hour: int = MODEL_RUNS_PER_HOUR, clock=time.monotonic):
        self.per_hour, self.clock = per_hour, clock
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, visitor: str) -> bool:
        now = self.clock()
        with self.lock:
            q = self.hits[visitor]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_hour:
                return False
            q.append(now)
            return True

    def remaining(self, visitor: str) -> int:
        now = self.clock()
        with self.lock:
            return max(0, self.per_hour - sum(1 for t in self.hits[visitor] if now - t <= 3600))


limiter = VisitorLimiter()


def _router_for(visitor: str, mode: str) -> tuple[Router | None, str | None]:
    """The router to use for this request, or None for rules only, with a note when limited."""
    router = get_router()
    if mode == "rules" or not router.enabled:
        return None, None
    if not limiter.allow(visitor):
        return None, ("You've reached this hour's limit for AI-written reviews, "
                      "so this one uses the standard explanations.")
    return router, None


# ---------------------------------------------------------------- compare this game

def _review_out(r: CoachReview, include_trace: bool = True) -> dict:
    return {"id": r.id, "game_id": r.game_id, "mode": r.mode, "provider": r.provider, "created_at": r.created_at,
            **r.result, **({"trace": r.trace} if include_trace else {})}


def cached_review(s: Session, game: Game) -> dict | None:
    rows = {r.mode: r for r in s.scalars(select(CoachReview).where(
        CoachReview.game_id == game.id, CoachReview.analysis_version == config.ANALYSIS_VERSION))}
    r = rows.get("model") or rows.get("rules")
    return _review_out(r) if r else None


def review_game(s: Session, game: Game, visitor: str, mode: str = "auto") -> dict:
    from .review import run_review  # lazy: LangGraph loads on first use
    from .tools import load_context

    existing = {r.mode: r for r in s.scalars(select(CoachReview).where(
        CoachReview.game_id == game.id, CoachReview.analysis_version == config.ANALYSIS_VERSION))}
    if mode != "rules" and "model" in existing:
        return _review_out(existing["model"])
    if mode == "rules" and "rules" in existing:
        return _review_out(existing["rules"])
    router, note = _router_for(visitor, mode)
    if router is None and "rules" in existing:
        out = _review_out(existing["rules"])
        if note:
            out["fallbacks"] = [note, *out.get("fallbacks", [])]
        return out

    ctx = load_context(s, game)
    if ctx is None:
        raise ValueError("Analyze this game first; the coach works from the engine's evidence.")
    result, trace = run_review(ctx, router, session=s)
    if note:
        result.fallbacks.insert(0, note)
    payload = result.model_dump(exclude={"game_id", "mode", "provider"})
    row = existing.get(result.mode)
    if row is None:
        row = CoachReview(profile_id=game.profile_id, game_id=game.id, analysis_version=config.ANALYSIS_VERSION,
                          mode=result.mode)
        s.add(row)
    row.provider, row.result, row.trace, row.created_at = result.provider, payload, trace, utcnow()
    try:
        s.commit()
    except IntegrityError:  # a parallel request saved the same review first
        s.rollback()
    return _review_out(row)


# ---------------------------------------------------------------- studies

def _positions(game: Game) -> tuple[list[str], list[str]]:
    b = chess.Board(game.initial_fen)
    fens, sans = [b.fen()], []
    for u in game.moves_uci.split():
        m = chess.Move.from_uci(u)
        sans.append(b.san(m))
        b.push(m)
        fens.append(b.fen())
    return fens, sans


def study_out(st: Study, game: Game) -> dict:
    """The study as the player sees it: answers only for checkpoints they have revealed."""
    fens, sans = _positions(game)
    cps = []
    for i, c in enumerate(st.checkpoints):
        shown = i in st.revealed
        cps.append({"index": i, "ply": c["ply"], "move_number": c["move_number"], "side": c["side"], "fen": c["fen"],
                    "prompt": c["prompt"], "notes": st.notes.get(str(i), {}), "revealed": shown,
                    **({"answer": c["answer"], "feedback": st.feedback.get(str(i))} if shown else {})})
    return {"id": st.id, "game_id": st.game_id, "title": st.title, "rationale": st.rationale,
            "objective": st.objective, "theme": st.theme, "start_ply": st.start_ply, "mode": st.mode,
            "provider": st.provider, "created_at": st.created_at, "updated_at": st.updated_at,
            "game": {"opponent": game.opponent_name, "opponent_rating": game.opponent_rating,
                     "player_rating": game.player_rating, "color": game.player_color, "result": game.player_result,
                     "played_at": game.played_at, "time_class": game.time_class, "opening": game.opening,
                     "provider": game.provider},
            "positions": fens, "moves": sans, "checkpoints": cps}


def create_study(s: Session, game: Game, theme: str | None, visitor: str) -> Study:
    router, _ = _router_for(visitor, "auto")
    pick, provider, trace = study_mod.build_study(s, game, theme, router)
    st = Study(profile_id=game.profile_id, game_id=game.id, analysis_version=config.ANALYSIS_VERSION,
               theme=pick.theme, title=pick.title, rationale=pick.rationale, objective=pick.objective,
               start_ply=pick.start_ply, mode="model" if provider else "rules", provider=provider,
               checkpoints=[c.model_dump() for c in pick.checkpoints], notes={}, revealed=[], feedback={}, trace=trace)
    s.add(st)
    s.commit()
    return st


def save_notes(s: Session, st: Study, index: int, plans: str | None, move: str | None, reply: str | None,
               text: str | None) -> None:
    cp = st.checkpoints[index]
    san = study_mod.parse_move(cp["fen"], move)
    rep = None
    if reply and reply.strip():
        if not san:
            raise ValueError("Choose your move before predicting the reply.")
        rep = study_mod.parse_move(study_mod.reply_position(cp["fen"], san), reply)
    notes = dict(st.notes)
    notes[str(index)] = {"plans": (plans or "").strip()[:1000], "move": san, "reply": rep,
                         "text": (text or "").strip()[:2000]}
    st.notes = notes
    s.commit()


def reveal(s: Session, st: Study, index: int, visitor: str) -> None:
    cp = st.checkpoints[index]
    note = st.notes.get(str(index), {})
    text, provider = None, None
    if note.get("plans") or note.get("text"):
        router, _ = _router_for(visitor, "auto")
        text, provider = study_mod.model_feedback(router, cp, note)
    fb = dict(st.feedback)
    fb[str(index)] = {"text": text or study_mod.rule_feedback(cp, note), "source": "model" if text else "rules",
                      "provider": provider}
    st.feedback = fb
    if index not in st.revealed:
        st.revealed = [*st.revealed, index]
    s.commit()


def coach_status(visitor: str) -> dict:
    st = get_router().status()
    st["visitor_runs_left_this_hour"] = limiter.remaining(visitor)
    return st
