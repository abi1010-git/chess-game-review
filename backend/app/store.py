"""Write one game's analysis (run, per-move evidence, findings) replacing any earlier copy."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .analysis import GameAnalysis
from .models import AnalysisRun, Finding, Game, MoveEval


def write_analysis(s: Session, game: Game, version: str, result: GameAnalysis) -> AnalysisRun:
    old = s.scalars(select(AnalysisRun.id).where(AnalysisRun.game_id == game.id,
                                                  AnalysisRun.analysis_version == version)).all()
    if old:
        s.execute(delete(Finding).where(Finding.run_id.in_(old)))
        s.execute(delete(MoveEval).where(MoveEval.run_id.in_(old)))
        s.execute(delete(AnalysisRun).where(AnalysisRun.id.in_(old)))
    run = AnalysisRun(profile_id=game.profile_id, game_id=game.id, analysis_version=version,
                      engine_name=result.engine_name, settings=result.settings, summary=result.summary)
    s.add(run)
    s.flush()
    common = {"run_id": run.id, "game_id": game.id, "profile_id": game.profile_id, "analysis_version": version}
    s.add_all(MoveEval(**common, **p) for p in result.plies)
    s.add_all(Finding(**common, **f) for f in result.findings)
    s.flush()
    return run
