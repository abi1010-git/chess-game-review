"""Phase 4 evaluation: the coach against baselines, on the pilot sample in eval/data.

    python -m eval.run_eval analyze  [--db PATH] [--threads N]   # import + Stockfish analysis (slow, once)
    python -m eval.run_eval evaluate [--db PATH] [--model]        # reviews, scoring -> eval/results/results.json

The sample and the pass thresholds below were fixed before the first run (see docs/evaluation.md).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DATA, RESULTS = HERE / "data", HERE / "results"
DEFAULT_DB = Path(os.environ.get("EVAL_DB", HERE / "results" / "eval.db"))

# ---- pre-registered thresholds (defined before running; see docs/evaluation.md) --------------
THRESHOLDS = {
    "evidence_accuracy": 1.00,         # share of cited moves that exist, are the player's, and match as claimed
    "overclaims": 0,                   # verdicts stated from fewer than 3 earlier games
    "later_games_cited": 0,            # cited "earlier" games that were actually played later
    "planted_decision_selected": 0.80, # planted-weakness games where the coach picked the planted move
    "planted_pattern_found": 0.80,     # ...and compared it with earlier games (not "judged in this game only")
    "control_false_alarms": 0,         # recurring-weakness claims on the steady (control) player
    "sparse_declined": 1.00,           # sparse player: share of claims that decline to compare
    "study_pick_on_target": 0.90,      # recommended study game shows the same weakness
    "evidence_speedup": 3.0,           # games a reader opens to find 3 supporting examples: baseline / app
    "coordination_runtime_ratio": 3.0, # graph runtime / fixed workflow runtime, at most
}
PLAYERS = {"PilotA": 2, "PilotB": 3, "PilotC": 4}  # name -> profile id


def setup_env(db_path: Path) -> None:
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"


# ------------------------------------------------------------------ analyze

def analyze(db_path: Path, threads: int) -> None:
    setup_env(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from app import db, jobs
    from app.importers.service import execute_import
    from app.models import ImportRun, Profile, Game
    from sqlalchemy import select

    db.init_db()
    with db.SessionLocal() as s:
        for name, pid in PLAYERS.items():
            if s.get(Profile, pid) is None:
                s.add(Profile(id=pid, name=name))
        s.commit()
        for name, pid in PLAYERS.items():
            if s.scalar(select(Game.id).where(Game.profile_id == pid).limit(1)):
                continue
            run = ImportRun(profile_id=pid, source="pgn", label=name, params={})
            s.add(run)
            s.commit()
            execute_import(db.SessionLocal, run.id, [name], "upload",
                           pgn_text=(DATA / f"{name}.pgn").read_text(encoding="utf-8"))
        for name, pid in PLAYERS.items():
            ids = list(s.scalars(select(Game.id).where(Game.profile_id == pid)))
            jobs.enqueue(s, pid, ids, "manual")
            print(name, len(ids), "games queued")
    t0 = time.time()
    subprocess.run([sys.executable, "-m", "app.worker", "--once", "--threads", str(threads)], check=True,
                   env=os.environ.copy(), cwd=HERE.parent)
    print(f"analysis finished in {time.time() - t0:.0f}s")


# ------------------------------------------------------------------ scoring helpers

def planted(game) -> bool:
    return game.moves_uci.split()[6:7] == ["e1e2"]


def check_refs(s, claim, target_game_id) -> tuple[int, int]:
    """(valid, total) over a claim's cited moves, checked against the stored evidence."""
    from app import config
    from app.models import MoveEval
    get = lambda gid, ply: s.scalar(select_move(gid, ply))  # noqa: E731
    from sqlalchemy import select

    def select_move(gid, ply):
        return select(MoveEval).where(MoveEval.game_id == gid, MoveEval.ply == ply,
                                      MoveEval.analysis_version == config.ANALYSIS_VERSION)
    target = get(target_game_id, claim.ply)
    valid = total = 0
    for r in claim.refs:
        total += 1
        m = get(r.game_id, r.ply)
        if m is None or not m.is_player or m.low_info:
            continue
        if r.game_id != target_game_id:
            q = claim.match_quality
            if q == "exact" and m.position_key != target.position_key:
                continue
            if q == "structure" and (m.material_key != target.material_key or m.pawn_key != target.pawn_key):
                continue
            if q in ("theme", "phase", "structure") and m.phase != target.phase and q != "phase":
                continue
        valid += 1
    return valid, total


OVERCLAIM_VERDICTS = {"worse", "better", "similar", "mixed"}


def overclaim(claim, game_id) -> bool:
    earlier = {r.game_id for r in claim.refs if r.game_id != game_id}
    return claim.verdict in OVERCLAIM_VERDICTS and len(earlier) < 3


# ------------------------------------------------------------------ the three coordination variants

def variant_fixed(ctx):
    """Fixed workflow: top 4 candidates, one exact-position search each, no re-planning."""
    from app.coach.schemas import Claim, Ref
    from app.coach.tools import candidate_decisions, search_history, verdict_for
    claims = []
    for c in candidate_decisions(ctx)[:4]:
        res = search_history(ctx, c.ply, "exact", c.theme)
        v = verdict_for(c.win_drop, res) if res.strong_enough else "insufficient"
        refs = [Ref(game_id=ctx.game.id, ply=c.ply)] + [Ref(game_id=m.game_id, ply=m.ply) for m in res.matches[:5]]
        claims.append(Claim(ply=c.ply, verdict=v, headline="", text="", refs=refs, match_quality=res.match_quality))
    return claims


def variant_single(ctx):
    """Single specialist, same tools: tries every search level and takes the first one that finds
    anything, then states a verdict from it. It does not check whether the match is strong enough."""
    from app.analysis import INACCURACY, MISTAKE
    from app.coach.schemas import Claim, Ref
    from app.coach.tools import candidate_decisions, search_history
    claims = []
    for c in candidate_decisions(ctx)[:4]:
        res = None
        for feat in ("exact", "structure", "theme", "phase"):
            res = search_history(ctx, c.ply, feat, c.theme)
            if res.decisions:
                break
        if not res or not res.decisions:
            v = "insufficient"
        else:
            mr = res.mistake_rate or 0
            v = ("worse" if c.win_drop >= MISTAKE and mr <= 0.25 else "better" if c.win_drop < INACCURACY and mr >= 0.4
                 else "similar")
        refs = [Ref(game_id=ctx.game.id, ply=c.ply)] + ([Ref(game_id=m.game_id, ply=m.ply) for m in res.matches[:5]]
                                                          if res else [])
        claims.append(Claim(ply=c.ply, verdict=v, headline="", text="", refs=refs,
                            match_quality=res.match_quality if res else "none"))
    return claims


def variant_graph(ctx, router=None):
    from app.coach.review import run_review
    result, _trace = run_review(ctx, router)
    return result.claims, result


# ------------------------------------------------------------------ evaluate

def evaluate(db_path: Path, use_model: bool) -> dict:
    setup_env(db_path)
    from sqlalchemy import select
    from app import db
    from app.analysis import INACCURACY
    from app.coach.llm import Router, providers_from_env
    from app.coach.tools import load_context
    from app.dashboard import dashboard
    from app.models import AnalysisRun, Game, MoveEval

    db.init_db()
    out: dict = {"thresholds": THRESHOLDS, "players": {}, "coordination": {}, "model": None}
    with db.SessionLocal() as s:
        games = {n: list(s.scalars(select(Game).where(Game.profile_id == p).order_by(Game.played_at, Game.id)))
                 for n, p in PLAYERS.items()}
        for n, gs in games.items():
            assert gs and all(s.scalar(select(AnalysisRun.id).where(AnalysisRun.game_id == g.id)) for g in gs), \
                f"{n}: games are not all analyzed; run `analyze` first"

        # ---- baseline 1: recent-accuracy summary; baseline 2: usual single-game review (top-3 mistakes)
        for n in ("PilotA", "PilotB"):
            gs = games[n]
            half = len(gs) // 2
            acc = {g.id: s.scalar(select(AnalysisRun).where(AnalysisRun.game_id == g.id)).summary.get("accuracy")
                   for g in gs}
            early = statistics.mean(acc[g.id] for g in gs[:half] if acc[g.id] is not None)
            recent = statistics.mean(acc[g.id] for g in gs[half:] if acc[g.id] is not None)
            top3 = {}
            for g in gs:
                rows = list(s.scalars(select(MoveEval).where(MoveEval.game_id == g.id, MoveEval.is_player.is_(True),
                                                             MoveEval.low_info.is_(False))
                                      .order_by(MoveEval.win_drop.desc()).limit(3)))
                top3[g.id] = {r.ply for r in rows}
            rec_planted = [g for g in gs[half:] if planted(g)]
            dash = dashboard(s, PLAYERS[n])
            king = next((r for r in dash["recurring"] if r["theme"].startswith("opening.king")), None)
            # evidence effort: games a reader opens (newest first) before finding 3 other games with the weakness
            effort = []
            for g in rec_planted:
                opened = 0
                found = 0
                for other in reversed([x for x in gs if x.id != g.id]):
                    opened += 1
                    if planted(other) and 6 in top3[other.id]:
                        found += 1
                        if found == 3:
                            break
                effort.append(opened if found == 3 else None)
            out["players"][n] = {
                "games": len(gs), "planted_early": sum(planted(g) for g in gs[:half]),
                "planted_recent": len(rec_planted),
                "baseline_accuracy": {"early": round(early, 1), "recent": round(recent, 1),
                                      "delta": round(recent - early, 1), "names_a_pattern": False,
                                      "cites_positions": 0},
                "usual_review": {"planted_move_in_top3": f"{sum(6 in top3[g.id] for g in rec_planted)}/{len(rec_planted)}",
                                 "says_it_repeats": False, "games_to_open_for_3_examples": effort},
                "app_dashboard": {"king_safety_status": king["status"] if king else None,
                                  "games": king["games"] if king else 0,
                                  "occurrences": king["occurrences"] if king else 0},
            }

        # ---- coordination, evidence checks, model comparison: every review variant on the same games
        review_set = [(n, g) for n in ("PilotA", "PilotB") for g in games[n][len(games[n]) // 2:]] + \
                     [("PilotC", g) for g in games["PilotC"]]
        variants = {"graph": defaultdict(list), "fixed": defaultdict(list), "single": defaultdict(list)}
        router = Router(providers_from_env()) if use_model else None
        model_rows = []
        samples = {}
        for n, g in review_set:
            ctx = load_context(s, g)
            for name in ("graph", "fixed", "single"):
                t0 = time.perf_counter()
                if name == "graph":
                    claims, result = variant_graph(ctx)
                else:
                    claims = variant_fixed(ctx) if name == "fixed" else variant_single(ctx)
                dt = time.perf_counter() - t0
                v = variants[name][n]
                for c in claims:
                    valid, total = check_refs(s, c, g.id)
                    later = sum(1 for r in c.refs if r.game_id != g.id and
                                (s.get(Game, r.game_id).played_at, r.game_id) > (g.played_at, g.id))
                    row = db_row = s.scalar(select(MoveEval).where(MoveEval.game_id == g.id, MoveEval.ply == c.ply,
                                                                    MoveEval.analysis_version == "v1"))
                    v.append({"game": g.id, "planted": planted(g), "ply": c.ply, "verdict": c.verdict,
                              "valid": valid, "total": total, "over": overclaim(c, g.id), "later": later,
                              "drop": row.win_drop if row else None, "secs": dt / max(1, len(claims))})
                if name == "graph":
                    study = result.recommended_study
                    sg = s.get(Game, study["game_id"]) if study else None
                    v.append({"study": True, "game": g.id, "planted": planted(g),
                              "study_on_target": bool(sg and planted(sg)), "has_study": bool(study)})
                    if g.id == games["PilotA"][-1].id or (n == "PilotC" and g is games["PilotC"][-1]):
                        samples[n] = [{"headline": c.headline, "verdict": c.verdict, "text": c.text,
                                       "refs": [(r.game_id, r.ply) for r in c.refs[:4]]} for c in claims[:3]]
                    if router and router.enabled:
                        _c2, res2 = variant_graph(ctx, router)
                        model_rows.append({"game": g.id, "mode": res2.mode, "provider": res2.provider,
                                           "fallbacks": res2.fallbacks,
                                           "valid": sum(check_refs(s, c, g.id)[0] for c in _c2),
                                           "total": sum(check_refs(s, c, g.id)[1] for c in _c2),
                                           "claims": [(c.verdict, c.text) for c in _c2[:2]]})
            variants["graph"][n + "_secs"].append(dt)
        out["samples"] = samples

        def summarize(name):
            rows = [r for n in variants[name] for r in variants[name][n] if isinstance(r, dict) and "ply" in r]
            total = sum(r["total"] for r in rows)
            ok = sum(r["valid"] for r in rows)
            by = lambda n: [r for r in variants[name][n] if "ply" in r]  # noqa: E731
            pa = [r for r in by("PilotA") if r["planted"]]
            games_planted = {r["game"] for r in pa}
            selected = {r["game"] for r in pa if r["ply"] == 6}
            found = {r["game"] for r in pa if r["ply"] == 6 and r["verdict"] not in ("narrowed", "insufficient")}
            sparse = by("PilotC")
            ctrl = [r for r in by("PilotB")]
            return {
                "claims": len(rows), "evidence_accuracy": round(ok / total, 3) if total else None,
                "overclaims": sum(r["over"] for r in rows),
                "cites_later_games_as_earlier": sum(r["later"] for r in rows),
                "useful_selection": round(sum((r["drop"] or 0) >= INACCURACY for r in rows) / len(rows), 3),
                "planted_decision_selected": f"{len(selected)}/{len(games_planted)}",
                "planted_pattern_found": f"{len(found)}/{len(games_planted)}",
                "sparse_declined": f"{sum(r['verdict'] in ('narrowed', 'insufficient') for r in sparse)}/{len(sparse)}",
                "steady_player_verdicts": dict(defaultdict(int, {v: sum(r['verdict'] == v for r in ctrl)
                                                                  for v in {r['verdict'] for r in ctrl}})),
                "mean_secs_per_review": round(statistics.mean(
                    sum(r["secs"] for r in variants[name][n] if "ply" in r and r["game"] == gid)
                    for n in variants[name] if not n.endswith("_secs")
                    for gid in {r["game"] for r in variants[name][n] if "ply" in r}), 4),
            }
        out["coordination"] = {n: summarize(n) for n in variants}
        st = [r for n in variants["graph"] for r in variants["graph"][n] if isinstance(r, dict) and r.get("study")]
        pl = [r for r in st if r["planted"] and r["has_study"]]
        out["study"] = {"planted_games_with_pick": f"{len(pl)}/{sum(r['planted'] for r in st)}",
                        "pick_shows_weakness": f"{sum(r['study_on_target'] for r in pl)}/{len(pl)}",
                        "no_pick_when_sparse": all(not r["has_study"] for r in variants['graph']['PilotC']
                                                   if r.get("study"))}
        if model_rows:
            out["model"] = {"reviews": len(model_rows), "written_by_model": sum(r["mode"] == "model" for r in model_rows),
                            "evidence_valid": f"{sum(r['valid'] for r in model_rows)}/{sum(r['total'] for r in model_rows)}",
                            "fallbacks": sum(len(r["fallbacks"]) for r in model_rows),
                            "examples": model_rows[:2]}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "results.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out, indent=1, default=str))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=["analyze", "evaluate"])
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--threads", type=int, default=3)
    p.add_argument("--model", action="store_true", help="also run the model-backed mode (needs a provider key in the environment)")
    a = p.parse_args()
    analyze(a.db, a.threads) if a.stage == "analyze" else evaluate(a.db, a.model)


if __name__ == "__main__":
    main()
