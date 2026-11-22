"""'How I've Been Playing': recent games, trends and recurring findings, all from stored evidence."""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .analysis import MISTAKE
from .compare import THEME_LABELS, load_decisions, theme_recurrence
from .models import AnalysisJob, AnalysisRun, Finding, Game

FINDING_LABELS = {
    "tactic.hanging_piece": "Leaving pieces undefended",
    "tactic.fork": "Allowing double attacks",
    "tactic.material_loss": "Allowing tactics",
    "tactic.missed_material": "Missing material wins",
    "tactic.missed_fork": "Missing double attacks",
    "tactic.missed_mate": "Missing forced mates",
    "tactic.allowed_mate": "Allowing forced mates",
    "tactic.found_mate": "Finding forced mates",
    "opening.development": "Delaying development",
    "opening.king_exposure": "Exposing the king early",
    "middlegame.pawn_break": "Pawn breaks",
    "middlegame.weakness": "Creating pawn weaknesses",
    "middlegame.piece_activity": "Passive piece placement",
    "endgame.passed_pawn": "Passed-pawn decisions",
    "endgame.king_activity": "Passive king in the endgame",
    "endgame.conversion": "Converting winning endgames",
    "endgame.defense": "Defending endgames",
    "opening.only_move": "Finding only moves (opening)",
    "middlegame.only_move": "Finding only moves (middlegame)",
    "endgame.only_move": "Finding only moves (endgame)",
}
SITUATION_OF = {  # finding theme → the situation it is counted against
    "opening.development": "opening.development", "opening.king_exposure": "opening.king_safety",
    "middlegame.pawn_break": "middlegame.pawn_break", "middlegame.piece_activity": "middlegame.piece_activity",
    "endgame.passed_pawn": "endgame.passed_pawn", "endgame.king_activity": "endgame.king_activity",
    "endgame.conversion": "endgame.conversion", "endgame.defense": "endgame.defense",
}


def dashboard(s: Session, profile_id: int, version: str | None = None) -> dict:
    version = version or config.ANALYSIS_VERSION
    total_games = s.scalar(select(func.count(Game.id)).where(Game.profile_id == profile_id)) or 0
    job_counts = dict(s.execute(select(AnalysisJob.status, func.count()).where(
        AnalysisJob.profile_id == profile_id, AnalysisJob.analysis_version == version)
        .group_by(AnalysisJob.status)).all())
    runs = s.execute(select(AnalysisRun, Game).join(Game, Game.id == AnalysisRun.game_id).where(
        AnalysisRun.profile_id == profile_id, AnalysisRun.analysis_version == version)).all()
    coverage = {"games": total_games, "analyzed": len(runs), "queued": job_counts.get("queued", 0),
                "running": job_counts.get("running", 0), "failed": job_counts.get("failed", 0)}
    if not runs:
        return {"analysis_version": version, "coverage": coverage, "recent": [], "trends": [],
                "phases": {}, "recurring": [], "strengths": [], "uncertain_findings": 0}

    findings = list(s.scalars(select(Finding).where(Finding.profile_id == profile_id,
                                                    Finding.analysis_version == version)))
    f_by_game = defaultdict(list)
    for f in findings:
        f_by_game[f.game_id].append(f)

    ordered = sorted(runs, key=lambda rg: (rg[1].played_at is not None, rg[1].played_at, rg[1].id), reverse=True)
    recent = []
    for run, g in ordered[:12]:
        fs = sorted(f_by_game.get(g.id, []), key=lambda f: (f.polarity != "worse", f.confidence == "uncertain",
                                                             -(f.evidence or {}).get("win_drop", 0)))
        sm = run.summary or {}
        recent.append({
            "game_id": g.id, "played_at": g.played_at, "date_raw": g.date_raw, "opponent": g.opponent_name,
            "opponent_rating": g.opponent_rating, "player_rating": g.player_rating, "color": g.player_color,
            "result": g.player_result, "time_class": g.time_class, "provider": g.provider,
            "opening": g.opening, "accuracy": sm.get("accuracy"),
            "mistakes": sum(v for k, v in (sm.get("classifications") or {}).items() if k in ("mistake", "blunder")),
            "decisions": sm.get("meaningful_decisions"),
            "short": (sm.get("meaningful_decisions") or 0) < 6,
            "top_finding": ({"title": fs[0].title, "ply": fs[0].ply, "confidence": fs[0].confidence,
                             "polarity": fs[0].polarity} if fs else None),
        })

    # Monthly trends, with sample sizes so thin months read as thin.
    months = defaultdict(list)
    for run, g in runs:
        if g.played_at:
            months[g.played_at.strftime("%Y-%m")].append((run, g))
    trends = []
    for m in sorted(months):
        rows = months[m]
        accs = [r.summary.get("accuracy") for r, _ in rows if r.summary.get("accuracy") is not None]
        decisions = sum(r.summary.get("meaningful_decisions") or 0 for r, _ in rows)
        mistakes = sum(sum(v for k, v in (r.summary.get("classifications") or {}).items()
                           if k in ("mistake", "blunder")) for r, _ in rows)
        phase_drop = {}
        for ph in ("opening", "middlegame", "endgame"):
            vals = [(r.summary.get("by_phase") or {}).get(ph, {}) for r, _ in rows]
            n = sum(v.get("decisions") or 0 for v in vals)
            tot = sum((v.get("avg_drop") or 0) * (v.get("decisions") or 0) for v in vals)
            phase_drop[ph] = round(tot / n, 1) if n else None
        trends.append({"month": m, "games": len(rows), "decisions": decisions,
                       "accuracy": round(mean(accs), 1) if accs else None,
                       "mistakes_per_game": round(mistakes / len(rows), 2),
                       "mistake_rate": round(mistakes / decisions, 3) if decisions else None,
                       "avg_drop_by_phase": phase_drop,
                       "results": {r: sum(1 for _, g in rows if g.player_result == r) for r in ("win", "draw", "loss")},
                       "thin": len(rows) < 3})

    decisions = load_decisions(s, profile_id, version)
    phases = {}
    for ph in ("opening", "middlegame", "endgame"):
        ds = [d for d in decisions if d.phase == ph and not d.low_info]
        phases[ph] = {"decisions": len(ds), "games": len({d.game_id for d in ds}),
                      "avg_drop": round(mean(d.drop for d in ds), 1) if ds else None,
                      "mistake_rate": round(sum(d.drop >= MISTAKE for d in ds) / len(ds), 3) if ds else None}

    recurring, strengths = [], []
    groups = defaultdict(list)
    for f in findings:
        groups[(f.theme, f.polarity)].append(f)
    result_of = {g.id: g.player_result for _, g in runs}
    for (theme, polarity), fs in groups.items():
        game_ids = {f.game_id for f in fs}
        solid = [f for f in fs if f.confidence != "uncertain"]
        by_result = {r: sum(1 for gid in game_ids if result_of.get(gid) == r) for r in ("win", "draw", "loss")}
        situation = "tactic" if theme.startswith("tactic.") else SITUATION_OF.get(theme)
        opp = theme_recurrence(decisions, situation) if situation else None
        share = len(game_ids) / opp["games"] if opp and opp["games"] else None
        if polarity == "worse":
            # A pattern needs repeats AND a real share of the games where the situation came up.
            if len(game_ids) >= 3 and len(solid) >= 2 and share is not None and share >= 0.2:
                status = "recurring"
            elif len(game_ids) >= 2:
                status = "emerging"
            else:
                status = "single"
        else:
            status = "strength" if len(game_ids) >= 2 else "single"
        latest = max(fs, key=lambda f: f.game_id)
        item = {"theme": theme, "label": FINDING_LABELS.get(theme, theme), "polarity": polarity, "status": status,
                "occurrences": len(fs), "games": len(game_ids), "uncertain": len(fs) - len(solid),
                "share_of_games": round(share, 2) if share is not None else None,
                "by_result": by_result, "phase": fs[0].phase,
                "situation": situation, "situation_label": THEME_LABELS.get(situation or "", None),
                "opportunities": opp and {"decisions": opp["decisions"], "games": opp["games"],
                                          "mistake_rate": opp["mistake_rate"]},
                "examples": [{"game_id": f.game_id, "ply": f.ply, "title": f.title, "confidence": f.confidence}
                             for f in sorted(fs, key=lambda f: -f.game_id)[:5]],
                "latest_game_id": latest.game_id}
        (recurring if polarity == "worse" else strengths).append(item)
    rank = {"recurring": 0, "emerging": 1, "strength": 0, "single": 2}
    recurring.sort(key=lambda x: (rank[x["status"]], -x["games"], -x["occurrences"]))
    strengths.sort(key=lambda x: (rank[x["status"]], -x["games"]))

    return {"analysis_version": version, "coverage": coverage, "recent": recent, "trends": trends,
            "phases": phases, "recurring": recurring[:10], "strengths": strengths[:6],
            "uncertain_findings": sum(1 for f in findings if f.confidence == "uncertain")}
