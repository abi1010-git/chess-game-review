"""Game filtering ("Choose games") and comparison-sample suggestions ("Suggest a set")."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import median

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from . import config
from .models import AnalysisJob, Game

POOL_LABELS = {"lichess": "Lichess", "chesscom": "Chess.com", "pgn": "Uploaded PGN"}


@dataclass
class GameFilter:
    date_from: date | None = None
    date_to: date | None = None
    time_classes: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    color: str | None = None
    rating_min: int | None = None
    rating_max: int | None = None
    rated: bool | None = None
    results: list[str] = field(default_factory=list)
    analysis: str | None = None  # analyzed | not_analyzed | queued


def _start(d: date) -> datetime:
    return datetime.combine(d, time.min)


def apply_filter(stmt: Select, f: GameFilter, profile_id: int, version: str | None = None) -> Select:
    version = version or config.ANALYSIS_VERSION
    conds = [Game.profile_id == profile_id]
    if f.date_from:
        conds.append(Game.played_at >= _start(f.date_from))
    if f.date_to:
        conds.append(Game.played_at < _start(f.date_to + timedelta(days=1)))
    if f.time_classes:
        conds.append(Game.time_class.in_(f.time_classes))
    if f.providers:
        conds.append(Game.provider.in_(f.providers))
    if f.color in ("white", "black"):
        conds.append(Game.player_color == f.color)
    if f.rating_min is not None:
        conds.append(Game.player_rating >= f.rating_min)
    if f.rating_max is not None:
        conds.append(Game.player_rating <= f.rating_max)
    if f.rated is not None:
        conds.append(Game.rated == f.rated)
    if f.results:
        conds.append(Game.player_result.in_(f.results))
    if f.analysis:
        job_status = (select(AnalysisJob.status)
                      .where(AnalysisJob.game_id == Game.id, AnalysisJob.analysis_version == version)
                      .scalar_subquery())
        if f.analysis == "analyzed":
            conds.append(job_status == "done")
        elif f.analysis == "queued":
            conds.append(job_status.in_(("queued", "running")))
        elif f.analysis == "not_analyzed":
            conds.append(func.coalesce(job_status, "none").not_in(("done", "queued", "running")))
    return stmt.where(and_(*conds))


def facets(s: Session, profile_id: int) -> dict:
    base = Game.profile_id == profile_id
    row = s.execute(select(func.count(Game.id), func.min(Game.played_at), func.max(Game.played_at),
                           func.min(Game.player_rating), func.max(Game.player_rating)).where(base)).one()
    return {
        "total": row[0],
        "date_min": row[1].date().isoformat() if row[1] else None,
        "date_max": row[2].date().isoformat() if row[2] else None,
        "rating_min": row[3],
        "rating_max": row[4],
        "time_classes": sorted(s.scalars(select(Game.time_class).where(base).distinct())),
        "providers": sorted(s.scalars(select(Game.provider).where(base).distinct())),
    }


# ---------------------------------------------------------------- suggestion

def pool_key(g: Game) -> tuple[str, str, bool | None]:
    return (g.provider, g.time_class, g.rated)


def pool_label(key: tuple[str, str, bool | None]) -> str:
    provider, tc, rated = key
    rated_txt = "rated" if rated else "casual" if rated is False else "rated/casual unknown"
    return f"{POOL_LABELS.get(provider, provider)} {tc} ({rated_txt})"


def suggest(s: Session, profile_id: int, target_from: date, target_to: date, *, rating_band: int = 100,
            max_comparison: int = 40, min_comparison: int = 10) -> dict:
    """Propose target games plus older comparison games from the same pool at a similar rating."""
    games = list(s.scalars(select(Game).where(Game.profile_id == profile_id)))
    start, end = _start(target_from), _start(target_to + timedelta(days=1))
    undated = [g for g in games if g.played_at is None]
    targets = [g for g in games if g.played_at and start <= g.played_at < end]
    warnings: list[str] = []
    if undated:
        warnings.append(f"{len(undated)} game(s) have no recorded date and were left out of the suggestion.")
    if not targets:
        return {"groups": [], "warnings": warnings + ["No games were played in the chosen date range."]}

    by_pool: dict[tuple, list[Game]] = {}
    for g in targets:
        by_pool.setdefault(pool_key(g), []).append(g)

    groups: list[dict] = []
    quota_left = max_comparison
    pools = sorted(by_pool.items(), key=lambda kv: -len(kv[1]))
    for i, (key, pool_targets) in enumerate(pools):
        # Split the comparison budget across pools in proportion to how many target games each has.
        remaining_targets = sum(len(t) for _, t in pools[i:])
        quota = max(1, round(quota_left * len(pool_targets) / remaining_targets)) if quota_left > 0 else 0
        label = pool_label(key)
        ratings = [g.player_rating for g in pool_targets if g.player_rating is not None]
        center = int(median(ratings)) if ratings else None

        groups.append({
            "key": f"target:{key}",
            "role": "target",
            "pool": label,
            "title": f"Target games: {label}",
            "reason": (f"Played between {target_from.isoformat()} and {target_to.isoformat()}"
                       + (f"; median rating {center}." if center else "; no rating recorded.")),
            "game_ids": [g.id for g in sorted(pool_targets, key=lambda g: g.played_at, reverse=True)],
        })
        if quota == 0:
            continue

        older = [g for g in games if g.played_at and g.played_at < start and pool_key(g) == key]
        older.sort(key=lambda g: g.played_at, reverse=True)  # most recent first
        if not older:
            warnings.append(f"No earlier games in {label} to compare against.")
            continue

        chosen: list[Game] = []
        if center is not None:
            band = rating_band
            rated_older = [g for g in older if g.player_rating is not None]
            while True:
                chosen = [g for g in rated_older if abs(g.player_rating - center) <= band][:quota]
                if len(chosen) >= min(min_comparison, quota) or band >= 4 * rating_band:
                    break
                band *= 2
            if chosen:
                note = "" if band == rating_band else f" (widened from ±{rating_band} to find enough games)"
                groups.append({
                    "key": f"comparison:{key}",
                    "role": "comparison",
                    "pool": label,
                    "title": f"Comparison games: {label}",
                    "reason": (f"Most recent earlier games in the same platform, time control and rating pool, "
                               f"with your rating within ±{band} of {center}{note}."),
                    "game_ids": [g.id for g in chosen],
                })
            short = quota - len(chosen)
            unrated = [g for g in older if g.player_rating is None][:short] if short > 0 else []
            if unrated:
                groups.append({
                    "key": f"comparison_unrated:{key}",
                    "role": "comparison",
                    "pool": label,
                    "title": f"Comparison games without a rating: {label}",
                    "reason": "Same platform and time control, but no rating was recorded, so rating similarity "
                              "could not be checked. Added because too few rated matches were found.",
                    "game_ids": [g.id for g in unrated],
                })
                chosen += unrated
        else:
            chosen = older[:quota]
            warnings.append(f"The target games in {label} have no recorded rating, so comparisons were matched on "
                            "platform and time control only.")
            groups.append({
                "key": f"comparison:{key}",
                "role": "comparison",
                "pool": label,
                "title": f"Comparison games: {label}",
                "reason": "Most recent earlier games in the same platform and time control (no rating to match).",
                "game_ids": [g.id for g in chosen],
            })
        if len(chosen) < min_comparison:
            warnings.append(f"Only {len(chosen)} comparison game(s) found for {label}; "
                            "conclusions from this pool will be limited.")
        quota_left -= len(chosen)

    return {"groups": groups, "warnings": warnings}
