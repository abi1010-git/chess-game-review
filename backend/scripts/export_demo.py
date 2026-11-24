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


NAMES = ["Marcus_Webb", "elena.v", "kingside_k", "TacticalTina", "RookLift99", "bishop_pair", "pawnstorm_pete", "quietmove",
         "NightRider44", "sicilian_sam", "endgame_ed", "castle_early", "Zwischenzug", "fianchetto_f", "gambit_gus", "OpenFile",
         "knightfork_ky", "slowplay_sue", "blitz_bianca", "dev_lead", "exchange_ex", "tempo_tom", "centipawn_cal", "stalemate_stu"]


def friendly(item: dict, i: int) -> dict:
    """Give the engine-played sample realistic-looking opponent names (idempotent)."""
    g = item["game"]
    old = g["opponent_name"]
    if not (old or "").startswith("Opp"):
        return item
    new = f"{NAMES[i % len(NAMES)]}{'' if i < len(NAMES) else i}"
    g["opponent_name"] = g["black"] = new
    g["pgn"] = g["pgn"].replace(old, new).replace('[Event "Pilot sample"]', '[Event "Sample game"]').replace('[Site "eval"]', '[Site "Sample data"]')
    g["headers"] = {k: (new if v == old else "Sample game" if v == "Pilot sample" else "Sample data" if v == "eval" else v)
                    for k, v in (g["headers"] or {}).items()}
    return item


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
    p.add_argument("--db", type=Path)
    p.add_argument("--refresh", action="store_true", help="re-apply the naming to the bundled file without a database")
    p.add_argument("--profile", type=int, default=2)
    a = p.parse_args()
    if a.refresh:
        data = json.loads(gzip.decompress(OUT.read_bytes()))
        data["games"] = [friendly(it, i) for i, it in enumerate(data["games"])]
        OUT.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
        print("refreshed", len(data["games"]), "games")
        return
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
            games.append(friendly({
                "game": gd, "run": row(run),
                "evals": [row(m) for m in s.scalars(select(MoveEval).where(MoveEval.run_id == run.id).order_by(MoveEval.ply))],
                "findings": [row(f) for f in s.scalars(select(Finding).where(Finding.run_id == run.id))],
            }, len(games)))
    OUT.write_bytes(gzip.compress(json.dumps({"label": "Sample history: 24 engine-played games", "games": games},
                                              separators=(",", ":")).encode(), 9))
    print(len(games), "games ->", OUT, OUT.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
