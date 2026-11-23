"""Export the analyzed pilot player (PilotA from the evaluation) as the bundled sample history.

    python -m scripts.export_demo --db eval/results/eval.db

Writes app/demo_data.json.gz. The games are engine-played (see docs/evaluation.md), analysed with
the production engine settings, so visitors can try the whole workflow without waiting for Stockfish.
"""
import argparse
import gzip
import json
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "demo_data.json.gz"
SKIP = {"id", "profile_id", "import_run_id", "game_id", "run_id"}


def row(obj) -> dict:
    d = {}
    for c in obj.__table__.columns:
        if c.name in SKIP:
            continue
        v = getattr(obj, c.name)
        d[c.name] = v.isoformat() if hasattr(v, "isoformat") else v
    return d


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--profile", type=int, default=2)
    a = p.parse_args()
    os.environ["DATABASE_URL"] = f"sqlite:///{a.db.resolve().as_posix()}"
    from sqlalchemy import select
    from app import db
    from app.models import AnalysisRun, Finding, Game, MoveEval

    db.init_db()
    games = []
    with db.SessionLocal() as s:
        for g in s.scalars(select(Game).where(Game.profile_id == a.profile).order_by(Game.played_at, Game.id)):
            run = s.scalar(select(AnalysisRun).where(AnalysisRun.game_id == g.id))
            gd = row(g)
            gd["player_name"], gd["white"], gd["black"] = "Sample player", "Sample player", gd["opponent_name"]
            gd["pgn"] = gd["pgn"].replace("PilotA", "Sample player")
            gd["headers"] = {k: ("Sample player" if v == "PilotA" else v) for k, v in (gd["headers"] or {}).items()}
            games.append({
                "game": gd, "run": row(run),
                "evals": [row(m) for m in s.scalars(select(MoveEval).where(MoveEval.run_id == run.id).order_by(MoveEval.ply))],
                "findings": [row(f) for f in s.scalars(select(Finding).where(Finding.run_id == run.id))],
            })
    OUT.write_bytes(gzip.compress(json.dumps({"label": "Sample history: 24 engine-played games", "games": games},
                                              separators=(",", ":")).encode(), 9))
    print(len(games), "games ->", OUT, OUT.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
