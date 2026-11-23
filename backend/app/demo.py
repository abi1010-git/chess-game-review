"""The bundled sample history: copy it into a visitor's own space so they can try every screen."""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .jobs import job_key
from .models import AnalysisJob, AnalysisRun, Finding, Game, MoveEval, SourceIdentity, utcnow

DATA = Path(__file__).parent / "demo_data.json.gz"
_DATES = {"played_at", "imported_at", "created_at"}


def _load() -> dict:
    return json.loads(gzip.decompress(DATA.read_bytes()))


def _fix(d: dict) -> dict:
    return {k: (datetime.fromisoformat(v) if k in _DATES and v else v) for k, v in d.items()}


def sample_available() -> bool:
    return DATA.exists()


def load_sample(s: Session, profile_id: int) -> int:
    """Copy the sample games and their stored analysis into this profile. Returns the game count.

    Raises ValueError if the space already holds games, so a visitor's own data is never mixed in.
    """
    if (s.scalar(select(func.count(Game.id)).where(Game.profile_id == profile_id)) or 0) > 0:
        raise ValueError("Sample history can only be loaded into an empty space.")
    data = _load()
    version = config.ANALYSIS_VERSION
    for i, item in enumerate(data["games"]):
        g = Game(profile_id=profile_id, **_fix(item["game"]))
        g.provider_game_id = f"sample{i + 1}"
        s.add(g)
        s.flush()
        run = AnalysisRun(profile_id=profile_id, game_id=g.id, **_fix(item["run"]))
        s.add(run)
        s.flush()
        s.add_all(MoveEval(profile_id=profile_id, game_id=g.id, run_id=run.id, **e) for e in item["evals"])
        s.add_all(Finding(profile_id=profile_id, game_id=g.id, run_id=run.id, **f) for f in item["findings"])
        now = utcnow()
        s.add(AnalysisJob(profile_id=profile_id, game_id=g.id, analysis_version=run.analysis_version,
                          job_key=job_key(g.id, run.analysis_version), status="done", attempts=1,
                          started_at=now, finished_at=now))
    s.add(SourceIdentity(profile_id=profile_id, provider="pgn", username="Sample player"))
    s.commit()
    return len(data["games"])
