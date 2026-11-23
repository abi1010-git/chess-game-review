"""Typed inputs and outputs for the specialists and their tools.

Every claim the coach shows carries the (game, move) references that support it. The verifier
checks those references against stored engine evidence before anything reaches the player.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SearchFeature = Literal["exact", "structure", "theme", "phase"]
MatchQuality = Literal["exact", "structure", "theme", "phase", "none"]


class Ref(BaseModel):
    """A move in one of the player's games that supports a claim."""
    game_id: int
    ply: int


class TraceStep(BaseModel):
    """One step of the workflow: who acted, which tool or handoff, and what came back."""
    actor: Literal["reviewer", "history", "study_coach", "verifier", "writer", "router"]
    action: str
    detail: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- game reviewer

class CandidateDecision(BaseModel):
    """A decision the reviewer picked for comparison, with the engine and board evidence it fetched."""
    ply: int
    move_number: int
    side: str
    move: str
    best_move: str | None
    phase: str
    win_drop: float
    classification: str
    theme: str | None
    finding_title: str | None = None
    finding_summary: str | None = None
    engine_line: list[str] = Field(default_factory=list)
    features: dict = Field(default_factory=dict)


class SearchRequest(BaseModel):
    """What the reviewer (or the history specialist itself) asks the history specialist to search."""
    ply: int
    feature: SearchFeature
    theme: str | None = None


# ---------------------------------------------------------------- history specialist

class HistoryMatch(BaseModel):
    game_id: int
    ply: int
    move: str
    best_move: str | None
    win_drop: float
    match: MatchQuality
    played_at: str | None
    result: str


class HistoryResult(BaseModel):
    """Comparable earlier decisions for one candidate, how good the match is, and what is missing."""
    ply: int
    feature: SearchFeature
    match_quality: MatchQuality
    games: int
    decisions: int
    mistake_rate: float | None
    matches: list[HistoryMatch] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    strong_enough: bool
    next_step: Literal["accept", "widen", "narrow"]
    widen_to: SearchFeature | None = None


# ---------------------------------------------------------------- written output

class Claim(BaseModel):
    """One statement shown to the player, with the moves that support it."""
    ply: int
    verdict: Literal["better", "worse", "similar", "mixed", "insufficient", "narrowed"]
    headline: str
    text: str
    refs: list[Ref] = Field(default_factory=list)
    match_quality: MatchQuality = "none"
    searches: int = 1
    source: Literal["rules", "model"] = "rules"


class ReviewResult(BaseModel):
    game_id: int
    mode: Literal["rules", "model"]
    provider: str | None = None
    summary: str
    claims: list[Claim]
    recommended_study: dict | None = None
    handoffs: int
    tool_calls: int
    fallbacks: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- study coach

class CheckpointAnswer(BaseModel):
    """Kept server-side until the player asks for it."""
    played: str
    best: str | None
    best_line: list[str] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)  # engine's top moves: [{san, win}]
    win_before: float
    win_after: float
    verdict: str
    explanation: str


class Checkpoint(BaseModel):
    ply: int
    move_number: int
    side: str
    fen: str
    prompt: str
    answer: CheckpointAnswer


class StudyPick(BaseModel):
    game_id: int
    theme: str | None
    title: str
    rationale: str  # must not name the best move
    objective: str
    start_ply: int
    checkpoints: list[Checkpoint]


# ---------------------------------------------------------------- what the model may return

class ModelPick(BaseModel):
    """The model chooses among candidates the reviewer offered, and a search feature for each."""
    plies: list[int] = Field(max_length=4)
    features: dict[str, SearchFeature] = Field(default_factory=dict)


class ModelClaimText(BaseModel):
    ply: int
    headline: str = Field(max_length=90)
    text: str = Field(max_length=480)


class ModelWriting(BaseModel):
    summary: str = Field(max_length=400)
    claims: list[ModelClaimText]


class ModelStudyText(BaseModel):
    title: str = Field(max_length=90)
    rationale: str = Field(max_length=400)
    objective: str = Field(max_length=200)
    prompts: list[str] = Field(default_factory=list)


class ModelFeedback(BaseModel):
    feedback: str = Field(max_length=500)
