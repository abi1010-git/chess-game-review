""""Compare this game" as a LangGraph workflow of specialists with explicit conditional handoffs.

    reviewer ──► history ──┬─► history          (weak match: the specialist widens its own search)
                           ├─► reviewer_narrow  (still weak: the reviewer narrows the claim)
                           └─► writer ──► verifier ──► study_coach ──► END

The reviewer picks decisions and calls the engine-line and board-feature tools. The history
specialist searches earlier games, rates the match, and lists evidence gaps. The writer turns the
verified evidence into text (templates, or a model when one is available), and the verifier
checks every reference and every move named before anything is shown. The study coach then
recommends a whole game when a comparison points at something worth practising.

Handoffs, searches per decision and tool calls are all bounded.
"""
from __future__ import annotations

import json
import operator
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from ..analysis import MISTAKE
from . import study as study_mod
from .llm import Router
from .schemas import (
    CandidateDecision,
    Claim,
    HistoryResult,
    ModelPick,
    ModelWriting,
    Ref,
    ReviewResult,
    SearchRequest,
    TraceStep,
)
from .tools import (
    FEATURE_LABEL,
    GameContext,
    board_features,
    candidate_decisions,
    engine_lines,
    search_history,
    theme_label,
    verdict_for,
)

MAX_DECISIONS = 4
MAX_SEARCHES_PER_DECISION = 3
MAX_HANDOFFS = 8
MAX_TOOL_CALLS = 40

SAN_RE = re.compile(r"\b(?:O-O(?:-O)?|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h](?:x[a-h])?[1-8](?:=[QRBN])?)[+#]?")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


class State(TypedDict, total=False):
    candidates: list[dict]
    selected: list[int]
    pending: list[dict]  # SearchRequest dicts waiting for the history specialist
    results: dict  # str(ply) -> HistoryResult dict (latest search)
    searches: dict  # str(ply) -> number of searches run
    narrow: list[int]  # plies the reviewer must narrow
    narrowed: list[int]
    claims: list[dict]
    summary: str
    recommended_study: dict | None
    provider: str | None
    trace: Annotated[list, operator.add]
    fallbacks: Annotated[list, operator.add]
    handoffs: Annotated[int, operator.add]
    tool_calls: Annotated[int, operator.add]


def _step(actor: str, action: str, **detail) -> dict:
    return TraceStep(actor=actor, action=action, detail=detail).model_dump()


def _san(move: str | None) -> str:
    return (move or "").rstrip("+#")


class ReviewWorkflow:
    def __init__(self, ctx: GameContext, router: Router | None, session=None):
        self.ctx = ctx
        self.router = router if router is not None and router.enabled else None
        self.session = session
        self.graph = self._build()

    # ---------------------------------------------------------------- graph

    def _build(self):
        g = StateGraph(State)
        g.add_node("reviewer", self.reviewer)
        g.add_node("history", self.history)
        g.add_node("reviewer_narrow", self.reviewer_narrow)
        g.add_node("writer", self.writer)
        g.add_node("verifier", self.verifier)
        g.add_node("study_coach", self.study_coach)
        g.add_edge(START, "reviewer")
        g.add_conditional_edges("reviewer", self.after_reviewer, {"history": "history", "writer": "writer"})
        g.add_conditional_edges("history", self.after_history,
                                {"history": "history", "reviewer_narrow": "reviewer_narrow", "writer": "writer"})
        g.add_edge("reviewer_narrow", "writer")
        g.add_edge("writer", "verifier")
        g.add_edge("verifier", "study_coach")
        g.add_edge("study_coach", END)
        return g.compile()

    def run(self) -> tuple[ReviewResult, list[dict]]:
        out = self.graph.invoke({"trace": [], "fallbacks": [], "handoffs": 0, "tool_calls": 0, "results": {},
                                 "searches": {}, "narrow": [], "narrowed": [], "provider": None},
                                config={"recursion_limit": 30})
        claims = [Claim.model_validate(c) for c in out.get("claims", [])]
        used_model = any(c.source == "model" for c in claims) or out.get("provider") is not None
        return ReviewResult(
            game_id=self.ctx.game.id, mode="model" if used_model else "rules", provider=out.get("provider"),
            summary=out.get("summary", ""), claims=claims, recommended_study=out.get("recommended_study"),
            handoffs=out.get("handoffs", 0), tool_calls=out.get("tool_calls", 0),
            fallbacks=out.get("fallbacks", [])), out.get("trace", [])

    # ---------------------------------------------------------------- reviewer

    def reviewer(self, state: State) -> dict:
        trace, calls, fallbacks = [], 1, []
        cands = candidate_decisions(self.ctx)
        trace.append(_step("reviewer", "tool:candidate_decisions", found=len(cands),
                           plies=[c.ply for c in cands]))
        if not cands:
            return {"candidates": [], "selected": [], "pending": [], "trace": trace, "tool_calls": calls}

        selected = [c.ply for c in cands[:MAX_DECISIONS]]
        features = {c.ply: "exact" for c in cands}
        provider = None
        if self.router:
            menu = [{"ply": c.ply, "move": f"{c.move_number}{'.' if c.side == 'white' else '...'} {c.move}",
                     "phase": c.phase, "win_pct_lost": c.win_drop, "finding": c.finding_title, "kind": c.theme}
                    for c in cands]
            allowed = {c.ply for c in cands}
            res = self.router.complete(
                "choose",
                "You are the game reviewer in a chess coaching tool. Pick the decisions most worth comparing "
                "with the player's earlier games: prefer costly or instructive moves, and cover different "
                "phases. For each, choose how to search earlier games: 'exact' (same position), 'structure' "
                "(same material and pawns) or 'theme' (same kind of decision). Reply with JSON only: "
                '{"plies": [ply, ...], "features": {"<ply>": "exact|structure|theme"}}. At most 4 plies.',
                json.dumps({"candidates": menu}),
                ModelPick,
                check=lambda v: None if v.plies and set(v.plies) <= allowed else "picked a move that was not offered")
            if res:
                selected = list(dict.fromkeys(res.value.plies))[:MAX_DECISIONS]
                for k, v in res.value.features.items():
                    if k.isdigit() and int(k) in allowed and not (v == "theme" and not
                                                                  next(c for c in cands if c.ply == int(k)).theme):
                        features[int(k)] = v
                provider = res.provider
                trace.append(_step("router", "model:choose", provider=res.provider,
                                   attempts=[a.__dict__ for a in res.attempts]))
                trace.append(_step("reviewer", "model_selected", plies=selected,
                                   features={str(p): features[p] for p in selected}))
            else:
                fallbacks.append("The model could not pick decisions, so the usual ranking was used.")
                trace.append(_step("router", "model:choose_failed"))

        by_ply = {c.ply: c for c in cands}
        out_cands, pending = [], []
        for ply in selected:
            c = by_ply[ply]
            c.engine_line = engine_lines(self.ctx, ply)
            c.features = board_features(self.ctx, ply)
            calls += 2
            trace.append(_step("reviewer", "tool:engine_lines+board_features", ply=ply,
                               line=c.engine_line[:4], features=sorted(c.features)))
            out_cands.append(c.model_dump())
            pending.append(SearchRequest(ply=ply, feature=features[ply], theme=c.theme).model_dump())
        trace.append(_step("reviewer", "handoff:history", requests=pending))
        return {"candidates": out_cands, "selected": selected, "pending": pending, "trace": trace,
                "tool_calls": calls, "handoffs": 1, "fallbacks": fallbacks,
                **({"provider": provider} if provider else {})}

    def after_reviewer(self, state: State) -> str:
        return "history" if state.get("pending") else "writer"

    # ---------------------------------------------------------------- history specialist

    def history(self, state: State) -> dict:
        results = dict(state.get("results") or {})
        searches = dict(state.get("searches") or {})
        narrow = list(state.get("narrow") or [])
        trace, calls, nxt = [], 0, []
        budget = MAX_TOOL_CALLS - state.get("tool_calls", 0)
        for req in state.get("pending") or []:
            r = SearchRequest.model_validate(req)
            if calls >= budget:
                narrow.append(r.ply)
                trace.append(_step("history", "budget_exhausted", ply=r.ply))
                continue
            res = search_history(self.ctx, r.ply, r.feature, r.theme)
            calls += 1
            n = searches.get(str(r.ply), 0) + 1
            searches[str(r.ply)] = n
            results[str(r.ply)] = res.model_dump()
            trace.append(_step("history", "tool:search_history", ply=r.ply, feature=r.feature,
                               match_quality=res.match_quality, games=res.games, decisions=res.decisions,
                               gaps=res.gaps, next_step=res.next_step))
            if res.next_step == "widen" and n < MAX_SEARCHES_PER_DECISION:
                nxt.append(SearchRequest(ply=r.ply, feature=res.widen_to, theme=r.theme).model_dump())
                trace.append(_step("history", "request_different_search", ply=r.ply,
                                   reason=res.gaps[0] if res.gaps else "weak match", next_feature=res.widen_to))
            elif not res.strong_enough:
                narrow.append(r.ply)
                trace.append(_step("history", "handoff:reviewer_narrow_claim", ply=r.ply,
                                   reason=res.gaps[0] if res.gaps else "weak match"))
        return {"results": results, "searches": searches, "pending": nxt, "narrow": narrow, "trace": trace,
                "tool_calls": calls, "handoffs": 1}

    def after_history(self, state: State) -> str:
        if state.get("pending") and state.get("handoffs", 0) < MAX_HANDOFFS:
            return "history"
        if state.get("narrow"):
            return "reviewer_narrow"
        return "writer"

    # ---------------------------------------------------------------- reviewer narrows weak claims

    def reviewer_narrow(self, state: State) -> dict:
        narrowed = sorted(set(state.get("narrow") or []) | set(state.get("narrowed") or []))
        return {"narrowed": narrowed, "narrow": [], "handoffs": 1,
                "trace": [_step("reviewer", "narrowed_claims", plies=narrowed,
                                note="Claims limited to this game: not enough comparable earlier games.")]}

    # ---------------------------------------------------------------- writer

    def _rule_claim(self, c: CandidateDecision, res: HistoryResult | None, narrowed: bool, searches: int) -> Claim:
        mv = f"{c.move_number}{'.' if c.side == 'white' else '…'} {c.move}"
        refs = [Ref(game_id=self.ctx.game.id, ply=c.ply)]
        better = f" ({c.best_move} was stronger)" if c.best_move and _san(c.best_move) != _san(c.move) else ""
        cost = f"cost {c.win_drop:.0f} win-% points{better}" if c.win_drop >= 1 else "kept the evaluation"
        if res is None or narrowed or not res.strong_enough:
            gap = res.gaps[0] if res and res.gaps else "No comparable decisions in your other analyzed games."
            note = c.finding_summary or f"This move {cost}."
            return Claim(ply=c.ply, verdict="narrowed", headline=f"{mv}: judged within this game only",
                         text=f"{note} {gap} So this is not compared with your usual play yet.",
                         refs=refs, match_quality=res.match_quality if res else "none", searches=searches)
        refs += [Ref(game_id=m.game_id, ply=m.ply) for m in res.matches[:5]]
        verdict = verdict_for(c.win_drop, res)
        where = FEATURE_LABEL[res.feature]
        label = theme_label(c.theme).lower()
        mr = res.mistake_rate or 0
        sample = f"{res.decisions} decision{'s' if res.decisions != 1 else ''} in {res.games} earlier games with {where}"
        wrong = "you never went wrong" if mr == 0 else f"you went wrong {mr:.0%} of the time"
        text = {
            "worse": f"Worse than usual. This move {cost}, but across {sample} "
                     + ("you never went wrong." if mr == 0 else f"you went wrong only {mr:.0%} of the time."),
            "better": f"Better than usual. Across {sample} {wrong}; this time the move {cost}.",
            "mixed": f"Mixed record. Across {sample} {wrong}. Here the move {cost}.",
            "similar": f"In line with your usual play. Across {sample} {wrong}. Here the move {cost}.",
            "insufficient": f"Not enough evidence yet. {sample}.",
        }[verdict]
        if c.finding_summary and verdict in ("worse", "similar", "mixed"):
            text += f" {c.finding_summary}"
        heads = {"worse": "worse than usual", "better": "better than usual", "mixed": "mixed record",
                 "similar": "your usual pattern", "insufficient": "not enough evidence"}
        return Claim(ply=c.ply, verdict=verdict, headline=f"{mv}: {heads[verdict]} in {label}", text=text, refs=refs,
                     match_quality=res.match_quality, searches=searches)

    def writer(self, state: State) -> dict:
        cands = [CandidateDecision.model_validate(c) for c in state.get("candidates") or []]
        results = {int(k): HistoryResult.model_validate(v) for k, v in (state.get("results") or {}).items()}
        narrowed = set(state.get("narrowed") or [])
        searches = state.get("searches") or {}
        claims = [self._rule_claim(c, results.get(c.ply), c.ply in narrowed, searches.get(str(c.ply), 0))
                  for c in cands]
        summary = self._rule_summary(claims)
        trace = [_step("writer", "templates", claims=len(claims))]
        fallbacks, provider = [], state.get("provider")
        if self.router and claims:
            facts = self._facts(cands, results, claims)
            allowed = self._allowed_moves(cands, results)
            numbers = set(NUM_RE.findall(json.dumps(facts)))
            plies = {c.ply for c in claims}

            def check(v: ModelWriting) -> str | None:
                if {c.ply for c in v.claims} - plies:
                    return "wrote about a move that was not reviewed"
                for c in v.claims:
                    reason = self._unsupported(c.headline + " " + c.text, allowed.get(c.ply, set()), numbers)
                    if reason:
                        return reason
                return self._unsupported(v.summary, set().union(*allowed.values()) if allowed else set(), numbers)

            res = self.router.complete(
                "write",
                "You are a chess coach explaining a player's game review to them in plain, friendly language. "
                "Use ONLY the facts given. Never mention a move that is not in that claim's allowed_moves, and "
                "never write a number that is not in the facts. Keep each claim's verdict; when the evidence "
                "is weak say so plainly. Write to the player as 'you'. Reply with JSON only: "
                '{"summary": "...", "claims": [{"ply": <ply>, "headline": "...", "text": "..."}]}',
                json.dumps(facts, default=str), ModelWriting, check=check)
            if res:
                by = {c.ply: c for c in res.value.claims}
                for cl in claims:
                    m = by.get(cl.ply)
                    if m:
                        cl.headline, cl.text, cl.source = m.headline, m.text, "model"
                summary = res.value.summary or summary
                provider = provider or res.provider
                trace.append(_step("router", "model:write", provider=res.provider,
                                   attempts=[a.__dict__ for a in res.attempts]))
            else:
                fallbacks.append("The model was unavailable or its text could not be verified, "
                                 "so the standard explanations are shown.")
                trace.append(_step("router", "model:write_failed"))
        return {"claims": [c.model_dump() for c in claims], "summary": summary, "trace": trace,
                "fallbacks": fallbacks, "handoffs": 1, **({"provider": provider} if provider else {})}

    def _rule_summary(self, claims: list[Claim]) -> str:
        if not claims:
            return "No decisions in this game needed comparing: they were forced or the position was already decided."
        counts = {v: sum(c.verdict == v for c in claims) for v in ("worse", "better", "similar", "mixed", "narrowed")}
        parts = []
        if counts["worse"]:
            parts.append(f"{counts['worse']} decision{'s' if counts['worse'] > 1 else ''} worse than your usual play")
        if counts["better"]:
            parts.append(f"{counts['better']} better than usual")
        if counts["similar"] + counts["mixed"]:
            parts.append(f"{counts['similar'] + counts['mixed']} in line with your usual pattern")
        if counts["narrowed"]:
            parts.append(f"{counts['narrowed']} judged within this game only, for lack of comparable games")
        return f"Compared {len(claims)} key decision{'s' if len(claims) > 1 else ''}: " + ", ".join(parts) + "."

    def _facts(self, cands, results, claims) -> dict:
        out = []
        for c, cl in zip(cands, claims):
            r = results.get(c.ply)
            out.append({
                "ply": c.ply, "move": f"{c.move_number}{'.' if c.side == 'white' else '...'} {c.move}",
                "phase": c.phase, "verdict": cl.verdict, "win_pct_lost": round(c.win_drop),
                "engine_best": c.best_move, "engine_line": c.engine_line[:6], "finding": c.finding_summary,
                "kind_of_decision": theme_label(c.theme),
                "earlier_games": r.games if r else 0, "earlier_decisions": r.decisions if r else 0,
                "earlier_mistake_rate_pct": round((r.mistake_rate or 0) * 100) if r and r.mistake_rate is not None else None,
                "match": FEATURE_LABEL.get(r.feature) if r else None, "evidence_gaps": r.gaps if r else [],
                "allowed_moves": sorted(self._allowed_moves([c], results).get(c.ply, set())),
                "standard_text": cl.text,
            })
        return {"claims": out}

    def _allowed_moves(self, cands, results) -> dict[int, set[str]]:
        out = {}
        for c in cands:
            s = {_san(c.move), _san(c.best_move)} | {_san(x) for x in c.engine_line}
            r = results.get(c.ply)
            if r:
                s |= {_san(m.move) for m in r.matches} | {_san(m.best_move) for m in r.matches}
            out[c.ply] = {x for x in s if x}
        return out

    @staticmethod
    def _unsupported(text: str, allowed: set[str], numbers: set[str]) -> str | None:
        for tok in SAN_RE.findall(text):
            if _san(tok) not in allowed:
                return f"named a move not in the evidence ({tok})"
        for num in NUM_RE.findall(text):
            if num not in numbers and str(int(float(num))) not in numbers and float(num) > 3:
                return f"used a number not in the evidence ({num})"
        return None

    # ---------------------------------------------------------------- verifier

    def verifier(self, state: State) -> dict:
        from sqlalchemy import select

        from ..models import Game, MoveEval

        cands = {c["ply"]: CandidateDecision.model_validate(c) for c in state.get("candidates") or []}
        results = {int(k): HistoryResult.model_validate(v) for k, v in (state.get("results") or {}).items()}
        allowed = self._allowed_moves(list(cands.values()), results)
        claims = [Claim.model_validate(c) for c in state.get("claims") or []]
        known = {(self.ctx.game.id, p.ply) for p in self.ctx.plies}
        if self.session is not None:
            ref_set = {(r.game_id, r.ply) for c in claims for r in c.refs} - known
            if ref_set:
                rows = self.session.execute(
                    select(MoveEval.game_id, MoveEval.ply).join(Game, Game.id == MoveEval.game_id)
                    .where(Game.profile_id == self.ctx.game.profile_id,
                           MoveEval.game_id.in_({g for g, _ in ref_set}))).all()
                known |= {(g, p) for g, p in rows}
        else:
            known |= {(d.game_id, d.ply) for d in self.ctx.decisions}
        trace, fallbacks, checked = [], [], []
        for c in claims:
            bad_refs = [r for r in c.refs if (r.game_id, r.ply) not in known]
            c.refs = [r for r in c.refs if (r.game_id, r.ply) in known]
            problem = None
            if bad_refs:
                problem = f"{len(bad_refs)} reference(s) not found"
            if not c.refs:
                problem = "no supporting move"
            rule = self._rule_claim(cands[c.ply], results.get(c.ply), c.verdict == "narrowed", c.searches) \
                if c.ply in cands else None
            if c.source == "model" and rule is not None:
                # Numbers are checked against the evidence and the standard text, never the model's own text.
                facts = json.dumps(self._facts([cands[c.ply]], results, [rule]), default=str)
                reason = self._unsupported(c.headline + " " + c.text, allowed.get(c.ply, set()),
                                           set(NUM_RE.findall(facts)))
                if reason:
                    problem = reason
            if problem and c.source == "model" and rule is not None:
                fallbacks.append(f"Move {cands[c.ply].move_number}: the written explanation was replaced "
                                 f"with the standard one ({problem}).")
                c = rule
            elif problem and not c.refs:
                trace.append(_step("verifier", "dropped_claim", ply=c.ply, reason=problem))
                continue
            checked.append(c)
        trace.append(_step("verifier", "checked", claims=len(checked), refs=sum(len(c.refs) for c in checked)))
        return {"claims": [c.model_dump() for c in checked], "trace": trace, "fallbacks": fallbacks, "handoffs": 1}

    # ---------------------------------------------------------------- study coach handoff

    def study_coach(self, state: State) -> dict:
        claims = [Claim.model_validate(c) for c in state.get("claims") or []]
        cands = {c["ply"]: CandidateDecision.model_validate(c) for c in state.get("candidates") or []}
        target = next((c for c in claims if c.verdict in ("worse", "similar", "mixed")
                       and cands.get(c.ply) and cands[c.ply].theme and cands[c.ply].win_drop >= MISTAKE), None)
        if target is None:
            return {"recommended_study": None,
                    "trace": [_step("study_coach", "no_recommendation",
                                    reason="No compared mistake points at a pattern worth a whole-game study.")]}
        theme = cands[target.ply].theme
        pick, steps = study_mod.find_study_game(self.ctx.decisions, self.ctx.game.profile_id, theme,
                                                exclude={self.ctx.game.id}, session=self.session)
        rec = None
        if pick:
            rec = {"game_id": pick["game_id"], "theme": theme, "theme_label": theme_label(theme),
                   "reason": pick["reason"], "from_ply": target.ply}
        return {"recommended_study": rec, "handoffs": 1, "tool_calls": len(steps),
                "trace": steps + [_step("study_coach", "recommended" if rec else "no_suitable_game",
                                        theme=theme, game_id=rec and rec["game_id"])]}


def run_review(ctx: GameContext, router: Router | None, session=None) -> tuple[ReviewResult, list[dict]]:
    return ReviewWorkflow(ctx, router, session).run()
