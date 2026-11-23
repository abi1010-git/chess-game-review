"""Print real outputs for docs/evaluation.md: representative reviews and cases where the app declines.

    python -m eval.examples [--db PATH]     # uses the analyzed pilot database from `run_eval analyze`
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

HERE = Path(__file__).parent


def show_review(title, claims, extra=""):
    print(f"\n### {title}\n{extra}")
    for c in claims:
        refs = ", ".join(f"game {r.game_id} move {r.ply // 2 + 1}{'.' if r.ply % 2 == 0 else '...'}" for r in c.refs[:4])
        print(f"- **{c.headline}** [{c.verdict}, match: {c.match_quality}]\n  {c.text}\n  cites: {refs}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=HERE / "results" / "eval.db")
    a = p.parse_args()
    os.environ["DATABASE_URL"] = f"sqlite:///{a.db.as_posix()}"
    from sqlalchemy import select
    from app import db
    from app.coach.review import run_review
    from app.coach.tools import load_context
    from app.models import Game

    db.init_db()
    with db.SessionLocal() as s:
        gs = list(s.scalars(select(Game).where(Game.profile_id == 2).order_by(Game.played_at, Game.id)))
        sparse = list(s.scalars(select(Game).where(Game.profile_id == 4).order_by(Game.played_at, Game.id)))
        for title, g in (("PilotA, first game: no earlier games", gs[0]),
                         ("PilotA, game 3: only two earlier games", gs[2]),
                         ("PilotA, last game: 23 earlier games", gs[-1]),
                         ("PilotC (2 games), second game", sparse[-1])):
            r, _ = run_review(load_context(s, g), None)
            show_review(title, r.claims[:3], f"_{r.summary}_")
            if r.recommended_study:
                print(f"  study pick: game {r.recommended_study['game_id']}")
            else:
                print("  study pick: none (the coach declined to pick a game)")


if __name__ == "__main__":
    main()
