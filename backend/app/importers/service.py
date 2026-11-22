"""Turns adapter output or uploaded PGN text into stored, deduplicated games."""
from __future__ import annotations

import logging
from typing import Callable, Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Game, ImportRun, SourceIdentity, utcnow
from ..pgn_parse import (
    ParsedGame,
    ParseSkip,
    detect_platform,
    fingerprint,
    has_identifying_metadata,
    iter_pgn,
    normalize_headers,
    player_result,
)
from .base import ProviderError, RawGame

log = logging.getLogger(__name__)

MAX_STORED_SKIPS = 500
MAX_STORED_NOTICES = 100


class ImportProcessor:
    """Stores games one at a time so they show up while the import is still running."""

    def __init__(self, session: Session, run: ImportRun, player_names: Iterable[str], source_kind: str):
        self.s = session
        self.run_id = run.id
        self.profile_id = run.profile_id
        self.player_names = {n.strip().lower() for n in player_names if n and n.strip()}
        self.source_kind = source_kind
        self.counts = {"seen": 0, "imported": 0, "duplicates": 0, "skipped": 0}
        self.skips: list[dict] = list(run.skips or [])
        self.notices: list[dict] = list(run.notices or [])
        self._kept_separate = 0

    # ---- bookkeeping
    @property
    def run(self) -> ImportRun:
        return self.s.get(ImportRun, self.run_id)

    def notify(self, level: str, message: str) -> None:
        if len(self.notices) < MAX_STORED_NOTICES and {"level": level, "message": message} not in self.notices:
            self.notices.append({"level": level, "message": message})
            self._save_progress()

    def _skip(self, index: int | None, label: str, reason: str) -> None:
        self.counts["skipped"] += 1
        if len(self.skips) < MAX_STORED_SKIPS:
            self.skips.append({"index": index, "label": label, "reason": reason})

    def _save_progress(self) -> None:
        run = self.run
        run.seen = self.counts["seen"]
        run.imported = self.counts["imported"]
        run.duplicates = self.counts["duplicates"]
        run.skipped = self.counts["skipped"]
        run.skips = list(self.skips)
        run.notices = list(self.notices)
        self.s.commit()

    # ---- input paths
    def add_raw(self, raw: RawGame) -> None:
        self.counts["seen"] += 1
        label = raw.label or f"Game {self.counts['seen']}"
        if raw.variant and raw.variant != "standard":
            self._skip(self.counts["seen"], label, f"Unsupported variant: {raw.variant} (only standard chess for now)")
            self._save_progress()
            return
        if not raw.pgn:
            self._skip(self.counts["seen"], label, "The platform returned no PGN for this game")
            self._save_progress()
            return
        items = list(iter_pgn(raw.pgn, start_index=self.counts["seen"]))
        if not items:
            self._skip(self.counts["seen"], label, "The PGN for this game was empty")
            self._save_progress()
            return
        item = items[0]
        if isinstance(item, ParseSkip):
            self._skip(item.index, label, item.reason)
            self._save_progress()
            return
        item.label = label
        self._store(item, raw)

    def add_pgn_text(self, text: str) -> None:
        for item in iter_pgn(text):
            self.counts["seen"] += 1
            if isinstance(item, ParseSkip):
                self._skip(item.index, item.label, item.reason)
                self._save_progress()
            else:
                self._store(item, None)

    # ---- core
    def _store(self, parsed: ParsedGame, raw: RawGame | None) -> None:
        h = parsed.headers
        n = normalize_headers(h)

        white_l, black_l = (n.white or "").lower(), (n.black or "").lower()
        is_white, is_black = white_l in self.player_names, black_l in self.player_names
        if is_white and is_black:
            self._skip(parsed.index, parsed.label, "Both sides match the selected player, so the side is ambiguous")
            self._save_progress()
            return
        if not (is_white or is_black):
            self._skip(parsed.index, parsed.label, "The selected player did not play in this game")
            self._save_progress()
            return
        color = "white" if is_white else "black"

        detected = detect_platform(h.get("Site"), h.get("Link"))
        if raw is not None and raw.provider_game_id:
            provider, provider_game_id = raw.provider, raw.provider_game_id
        elif detected:
            provider, provider_game_id = detected
        else:
            provider, provider_game_id = (raw.provider if raw else "pgn"), None

        salt = ""
        if self.source_kind == "upload" and not provider_game_id and not has_identifying_metadata(h):
            # Without players, date or a game link we cannot tell a re-upload from a different game
            # with the same moves, so keep it as its own game rather than merging on moves alone.
            salt = f"run{self.run_id}:{parsed.index}"
            self._kept_separate += 1
        fp = fingerprint(parsed.initial_fen, parsed.moves_uci, h, salt)

        dup_conditions = [Game.fingerprint == fp]
        if provider_game_id:
            dup_conditions.append(and_(Game.provider == provider, Game.provider_game_id == provider_game_id))
        existing = self.s.scalar(
            select(Game.id).where(Game.profile_id == self.profile_id, or_(*dup_conditions)).limit(1))
        if existing is not None:
            self.counts["duplicates"] += 1
            self._save_progress()
            return

        played_at = n.played_at or (raw.played_at if raw else None)
        time_class = (raw.time_class if raw and raw.time_class else None) or n.time_class
        rated = raw.rated if raw is not None and raw.rated is not None else n.rated
        source_url = (raw.source_url if raw else None) or (
            h.get("Site") if detected and detected[0] == "lichess" else h.get("Link") if detected else None)

        game = Game(
            profile_id=self.profile_id,
            import_run_id=self.run_id,
            source_kind=self.source_kind,
            provider=provider,
            provider_game_id=provider_game_id,
            fingerprint=fp,
            source_url=source_url,
            white=n.white,
            black=n.black,
            white_rating=n.white_rating,
            black_rating=n.black_rating,
            result=n.result,
            player_color=color,
            player_name=n.white if is_white else n.black,
            player_rating=n.white_rating if is_white else n.black_rating,
            opponent_name=n.black if is_white else n.white,
            opponent_rating=n.black_rating if is_white else n.white_rating,
            player_result=player_result(color, n.result),
            played_at=played_at,
            date_raw=n.date_raw,
            time_control=n.time_control,
            time_class=time_class,
            rated=rated,
            variant="standard",
            initial_fen=parsed.initial_fen,
            moves_uci=" ".join(parsed.moves_uci),
            ply_count=len(parsed.moves_uci),
            has_clocks=parsed.has_clocks,
            eco=n.eco,
            opening=n.opening,
            termination=n.termination,
            pgn=parsed.pgn,
            headers=h,
        )
        self.s.add(game)
        try:
            self.counts["imported"] += 1
            self._save_progress()  # commits the game together with the counters
        except IntegrityError:
            # Another import stored the same game in the meantime.
            self.s.rollback()
            self.counts["imported"] -= 1
            self.counts["duplicates"] += 1
            self._save_progress()

    def finish(self, failed: bool = False) -> ImportRun:
        if self._kept_separate:
            self.notify("info", f"{self._kept_separate} game(s) had no players, date or game link, so they were "
                                "kept as separate games instead of being matched as duplicates.")
        run = self.run
        if failed:
            run.status = "failed" if self.counts["imported"] == 0 else "partial"
        elif self.counts["skipped"] or any(n["level"] in ("warning", "error") for n in self.notices):
            run.status = "completed_with_issues"
        else:
            run.status = "completed"
        run.finished_at = utcnow()
        self._save_progress()
        return run


def remember_identity(s: Session, profile_id: int, provider: str, username: str) -> None:
    exists = s.scalar(select(SourceIdentity.id).where(
        SourceIdentity.profile_id == profile_id, SourceIdentity.provider == provider,
        SourceIdentity.username == username))
    if exists is None:
        s.add(SourceIdentity(profile_id=profile_id, provider=provider, username=username))
        s.commit()


def execute_import(session_factory: Callable[[], Session], run_id: int, player_names: list[str],
                   source_kind: str, *, adapter_factory=None, pgn_text: str | None = None) -> None:
    """Run one import to completion. Safe to call from a background task."""
    with session_factory() as s:
        run = s.get(ImportRun, run_id)
        if run is None:
            return
        run.status = "running"
        s.commit()
        proc = ImportProcessor(s, run, player_names, source_kind)
        failed = False
        try:
            if pgn_text is not None:
                proc.add_pgn_text(pgn_text)
            else:
                adapter = adapter_factory(proc.notify)
                limit = (run.params or {}).get("max_games")
                for raw in adapter.iter_games():
                    proc.add_raw(raw)
                    if limit and proc.counts["seen"] >= limit:
                        break
        except ProviderError as e:
            failed = True
            proc.notify("error", str(e))
        except Exception:  # keep games already stored; report a generic error
            log.exception("Import %s crashed", run_id)
            failed = True
            s.rollback()
            proc.notify("error", "The import stopped because of an unexpected error. Games stored so far were kept.")
        proc.finish(failed=failed)
