"""Tables for profiles, source identities, games, imports, analysis jobs and engine evidence."""
from datetime import datetime, timezone

from sqlalchemy import (
    Float,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceIdentity(Base):
    """A username on a platform (or a name in uploaded PGNs) that belongs to the player."""
    __tablename__ = "source_identities"
    __table_args__ = (UniqueConstraint("profile_id", "provider", "username"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(20))
    username: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # lichess | chesscom | pgn
    label: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    imported: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    skips: Mapped[list] = mapped_column(JSON, default=list)  # [{index, label, reason}]
    notices: Mapped[list] = mapped_column(JSON, default=list)  # [{level, message}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (
        UniqueConstraint("profile_id", "provider", "provider_game_id", name="uq_game_provider_id"),
        UniqueConstraint("profile_id", "fingerprint", name="uq_game_fingerprint"),
        Index("ix_games_profile_played", "profile_id", "played_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    import_run_id: Mapped[int | None] = mapped_column(ForeignKey("import_runs.id", ondelete="SET NULL"), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(10))  # account | upload
    provider: Mapped[str] = mapped_column(String(20))  # lichess | chesscom | pgn (platform unknown)
    provider_game_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(300), nullable=True)

    white: Mapped[str | None] = mapped_column(String(100), nullable=True)
    black: Mapped[str | None] = mapped_column(String(100), nullable=True)
    white_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    black_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(String(10), default="*")

    player_color: Mapped[str] = mapped_column(String(5))  # white | black
    player_name: Mapped[str] = mapped_column(String(100))
    player_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opponent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    opponent_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    player_result: Mapped[str] = mapped_column(String(10), default="unknown")  # win | loss | draw | unknown

    played_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_raw: Mapped[str | None] = mapped_column(String(30), nullable=True)
    time_control: Mapped[str | None] = mapped_column(String(30), nullable=True)
    time_class: Mapped[str] = mapped_column(String(20), default="unknown")
    rated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    variant: Mapped[str] = mapped_column(String(30), default="standard")

    initial_fen: Mapped[str] = mapped_column(Text)
    moves_uci: Mapped[str] = mapped_column(Text)
    ply_count: Mapped[int] = mapped_column(Integer)
    has_clocks: Mapped[bool] = mapped_column(Boolean, default=False)
    eco: Mapped[str | None] = mapped_column(String(10), nullable=True)
    opening: Mapped[str | None] = mapped_column(String(200), nullable=True)
    termination: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pgn: Mapped[str] = mapped_column(Text)
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AnalysisBatch(Base):
    __tablename__ = "analysis_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20))  # manual | suggested
    analysis_version: Mapped[str] = mapped_column(String(30))
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BatchGame(Base):
    __tablename__ = "batch_games"
    batch_id: Mapped[int] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="selected")  # selected | target | comparison


class AnalysisJob(Base):
    """One engine job per (game, analysis version). job_key makes duplicate requests idempotent."""
    __tablename__ = "analysis_jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    analysis_version: Mapped[str] = mapped_column(String(30))
    job_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed | cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkerHeartbeat(Base):
    """Lets the API tell the player whether an analysis worker is running."""
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    engine_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    analysis_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    current_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnalysisRun(Base):
    """The stored engine result for one game and analysis version. Written once, atomically."""
    __tablename__ = "analysis_runs"
    __table_args__ = (UniqueConstraint("game_id", "analysis_version", name="uq_run_game_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    analysis_version: Mapped[str] = mapped_column(String(30))
    engine_name: Mapped[str] = mapped_column(String(100))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MoveEval(Base):
    """Evidence for one ply: the position before it, the move, and the engine's view.

    All scores are from the player's point of view, whichever side made the move.
    """
    __tablename__ = "move_evals"
    __table_args__ = (
        UniqueConstraint("run_id", "ply"),
        Index("ix_move_evals_profile_version", "profile_id", "analysis_version", "is_player"),
        Index("ix_move_evals_position", "profile_id", "position_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int] = mapped_column(Integer)
    analysis_version: Mapped[str] = mapped_column(String(30))
    ply: Mapped[int] = mapped_column(Integer)  # 0-based index of the move in the game
    move_number: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(5))  # white | black (who made this move)
    is_player: Mapped[bool] = mapped_column(Boolean)
    fen_before: Mapped[str] = mapped_column(Text)
    position_key: Mapped[str] = mapped_column(String(100))
    move_uci: Mapped[str] = mapped_column(String(8))
    move_san: Mapped[str] = mapped_column(String(12))
    legal_moves: Mapped[int] = mapped_column(Integer)

    best_uci: Mapped[str | None] = mapped_column(String(8), nullable=True)
    best_san: Mapped[str | None] = mapped_column(String(12), nullable=True)
    best_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_mate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    played_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    played_mate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    win_best: Mapped[float] = mapped_column(Float)
    win_played: Mapped[float] = mapped_column(Float)
    win_drop: Mapped[float] = mapped_column(Float)  # >= 0; how much the move gave away
    classification: Mapped[str] = mapped_column(String(15))  # best|good|inaccuracy|mistake|blunder|forced
    candidates: Mapped[list] = mapped_column(JSON, default=list)  # [{uci, san, cp, mate, win, line}]
    reply_line: Mapped[list] = mapped_column(JSON, default=list)  # engine's best continuation after the move (SAN)
    deep_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    stable: Mapped[bool] = mapped_column(Boolean, default=True)

    phase: Mapped[str] = mapped_column(String(12))
    material_key: Mapped[str] = mapped_column(String(40))
    pawn_key: Mapped[str] = mapped_column(String(120))
    material_balance: Mapped[int] = mapped_column(Integer)
    low_info: Mapped[bool] = mapped_column(Boolean, default=False)
    low_info_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    themes: Mapped[list] = mapped_column(JSON, default=list)  # decision situations, for matching
    features: Mapped[dict] = mapped_column(JSON, default=dict)  # before / after-played / after-best


class Finding(Base):
    """A labelled observation about one decision, with the evidence that supports it."""
    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_profile_version", "profile_id", "analysis_version", "theme"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int] = mapped_column(Integer)
    analysis_version: Mapped[str] = mapped_column(String(30))
    ply: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(12))
    theme: Mapped[str] = mapped_column(String(40))  # e.g. tactic.hanging_piece, endgame.king_activity
    kind: Mapped[str] = mapped_column(String(12))  # tactical | positional | strategic
    polarity: Mapped[str] = mapped_column(String(8))  # worse | better
    confidence: Mapped[str] = mapped_column(String(10))  # clear | likely | uncertain
    title: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
