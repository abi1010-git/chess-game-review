"""The history specialist's advice on a suggested game sample, before anything is analyzed.

It reads the suggestion and the games already analyzed, and says how much evidence each pool
will give: enough for most comparisons, enough only for common situations, or too little.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import AnalysisRun
from .tools import MIN_GAMES


def sample_advice(s: Session, profile_id: int, suggestion: dict) -> list[dict]:
    analyzed = set(s.scalars(select(AnalysisRun.game_id).where(
        AnalysisRun.profile_id == profile_id, AnalysisRun.analysis_version == config.ANALYSIS_VERSION)))
    pools: dict[str, dict] = {}
    for g in suggestion.get("groups", []):
        p = pools.setdefault(g["pool"], {"target": [], "comparison": []})
        p[g["role"]] += g["game_ids"]
    out = []
    for pool, ids in pools.items():
        comp = ids["comparison"]
        done = sum(1 for i in ids["target"] + comp if i in analyzed)
        if len(comp) >= 10:
            level, text = "good", (f"{len(comp)} comparison games: enough for most comparisons, including less "
                                   "common situations such as endgames.")
        elif len(comp) >= MIN_GAMES:
            level, text = "thin", (f"{len(comp)} comparison games: enough for common situations like tactics, "
                                   "but endgame and pawn-structure comparisons may say 'not enough evidence'.")
        else:
            level, text = "weak", (f"Only {len(comp)} comparison game{'s' if len(comp) != 1 else ''}: comparisons "
                                   f"need at least {MIN_GAMES}. Widen the rating band or import more history.")
        if done:
            text += f" {done} of these games are already analyzed, so they won't be queued again."
        out.append({"pool": pool, "level": level, "text": text, "targets": len(ids["target"]),
                    "comparisons": len(comp)})
    return out
