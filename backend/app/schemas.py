"""Request and response shapes for the API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import config


class AccountImportRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    since: date | None = None
    until: date | None = None
    max_games: int = Field(default=config.DEFAULT_GAMES_PER_IMPORT, ge=1, le=config.MAX_GAMES_PER_IMPORT)


class ImportRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source: str
    label: str
    status: str
    params: dict
    seen: int
    imported: int
    duplicates: int
    skipped: int
    skips: list[dict]
    notices: list[dict]
    created_at: datetime
    finished_at: datetime | None


class GameOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_kind: str
    provider: str
    provider_game_id: str | None
    source_url: str | None
    white: str | None
    black: str | None
    white_rating: int | None
    black_rating: int | None
    result: str
    player_color: str
    player_name: str
    player_rating: int | None
    opponent_name: str | None
    opponent_rating: int | None
    player_result: str
    played_at: datetime | None
    date_raw: str | None
    time_control: str | None
    time_class: str
    rated: bool | None
    ply_count: int
    has_clocks: bool
    eco: str | None
    opening: str | None
    termination: str | None
    analysis_status: str = "none"


class GameDetail(GameOut):
    initial_fen: str
    moves_uci: str
    pgn: str


class GamePage(BaseModel):
    total: int
    items: list[GameOut]


class SelectionRequest(BaseModel):
    game_ids: list[int] = Field(min_length=1, max_length=config.MAX_GAMES_PER_BATCH)


class SuggestRequest(BaseModel):
    target_from: date
    target_to: date
    rating_band: int = Field(default=100, ge=25, le=500)
    max_comparison: int = Field(default=40, ge=1, le=300)


class BatchCreate(BaseModel):
    game_ids: list[int] = Field(min_length=1, max_length=config.MAX_GAMES_PER_BATCH)
    mode: Literal["manual", "suggested"] = "manual"
    rationale: dict = Field(default_factory=dict)
    roles: dict[int, Literal["selected", "target", "comparison"]] = Field(default_factory=dict)


class BatchOut(BaseModel):
    id: int
    mode: str
    analysis_version: str
    rationale: dict
    created_at: datetime
    progress: dict
    summary: dict | None = None


class StudyCreate(BaseModel):
    game_id: int
    theme: str | None = Field(default=None, max_length=40)


class StudyNotes(BaseModel):
    plans: str | None = Field(default=None, max_length=1000)
    move: str | None = Field(default=None, max_length=12)
    reply: str | None = Field(default=None, max_length=12)
    text: str | None = Field(default=None, max_length=2000)
