"""Phase 3: the specialist workflow, the model router, and guided study.

Uses the fake engine for analysis and a fake HTTP transport for model providers, so no real
model or key is needed.
"""
import json
from datetime import datetime

import httpx
import pytest

from app import db, jobs
from app.coach import service
from app.coach.llm import Provider, Router, providers_from_env, set_router
from app.coach.schemas import ModelPick
from app.coach.study import find_study_game
from app.compare import Decision
from app.models import Game

from .test_analysis import add_game, analyze_all

# Black falls for the same mate in four games. Different knight shuffles first, so the games are
# not duplicates of each other but reach exactly the same position before the mistake (...Nf6??).
SHUFFLES = ["Nf3 Nc6 Ng1 Nb8", "Nh3 Nc6 Ng1 Nb8", "Nf3 Na6 Ng1 Nb8", "Nh3 Na6 Ng1 Nb8"]
TRAP = "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#"


def trap_game(s, i: int, day: int):
    return add_game(s, f"{SHUFFLES[i % 4]} {SHUFFLES[(i + 1) % 4]} {SHUFFLES[(i + 2) % 4]} {TRAP}", color="black",
                    result="loss", played_at=datetime(2025, 3, day))


@pytest.fixture(autouse=True)
def no_model():
    set_router(Router([]))  # rules only unless a test installs fake providers
    service.limiter.hits.clear()
    yield
    set_router(None)


def setup_games(n: int) -> list[int]:
    with db.SessionLocal() as s:
        ids = [trap_game(s, i, i + 1).id for i in range(n)]
        jobs.enqueue(s, 1, ids, "manual")
    analyze_all()
    return ids


def mistake_claim(review):
    return next(c for c in review["claims"] if c["headline"].startswith("10…") or "Nf6" in c["headline"])


# ---------------------------------------------------------------- specialists, rule-based mode

def test_strong_history_gives_a_verdict_and_recommends_a_study_game(client):
    ids = setup_games(4)
    r = client.post(f"/api/games/{ids[-1]}/coach-review").json()["review"]
    assert r["mode"] == "rules" and r["provider"] is None
    c = mistake_claim(r)
    assert c["verdict"] in ("similar", "worse") and c["match_quality"] == "exact" and c["searches"] == 1
    # Every claim points at real moves: this game plus the earlier games it was compared with.
    assert {ref["game_id"] for ref in c["refs"]} >= {ids[-1]} and len({ref["game_id"] for ref in c["refs"]}) >= 4
    rec = r["recommended_study"]
    assert rec and rec["game_id"] in ids[:-1] and rec["theme"] == "tactic"
    actions = [t["action"] for t in r["trace"]]
    assert actions[0] == "tool:candidate_decisions" and "handoff:history" in actions and "recommended" in actions
    assert r["handoffs"] <= 8 and r["tool_calls"] <= 40


def test_weak_history_triggers_a_second_search_and_then_a_narrower_claim(client):
    ids = setup_games(1)
    r = client.post(f"/api/games/{ids[0]}/coach-review").json()["review"]
    c = mistake_claim(r)
    assert c["verdict"] == "narrowed" and c["searches"] >= 2
    assert "judged within this game" in c["headline"]
    acts = [t["action"] for t in r["trace"]]
    assert "request_different_search" in acts
    assert "handoff:reviewer_narrow_claim" in acts and "narrowed_claims" in acts
    features = [t["detail"]["feature"] for t in r["trace"] if t["action"] == "tool:search_history"
                and t["detail"]["ply"] == c["ply"]]
    assert features[:2] == ["exact", "structure"]  # widened after the exact search came back empty


def test_review_is_saved_and_unanalyzed_games_are_refused(client):
    ids = setup_games(2)
    a = client.post(f"/api/games/{ids[0]}/coach-review").json()["review"]
    b = client.get(f"/api/games/{ids[0]}/coach-review").json()["review"]
    assert a["id"] == b["id"] and a["claims"] == b["claims"]
    with db.SessionLocal() as s:
        gid = add_game(s, "d4 d5").id
    assert client.post(f"/api/games/{gid}/coach-review").status_code == 409
    assert client.get("/api/games/99999/coach-review").status_code == 404


def test_study_coach_asks_for_another_example_when_a_game_is_too_forced():
    def dec(gid, ply, drop, themes=("tactic",), low=False):
        return Decision(game_id=gid, ply=ply, move_number=ply // 2 + 1, side="white", san="e4", best_san="d4",
                        drop=drop, win_best=50, classification="mistake", phase="middlegame", position_key=f"{gid}{ply}",
                        material_key="m", pawn_key="p", balance=0, themes=list(themes), low_info=low,
                        played_at=datetime(2025, 1, gid), result="loss", time_class="blitz", provider="pgn",
                        rating=1500, game_hash=str(gid))
    short = [dec(2, 10, 30)] + [dec(2, i, 0, low=True) for i in range(12, 30, 2)]  # recent, but forced
    long = [dec(1, 10, 30)] + [dec(1, i, 1) for i in range(12, 30, 2)]
    pick, steps = find_study_game(short + long, 1, "tactic", exclude=set())
    assert pick and pick["game_id"] == 1
    acts = [s["action"] for s in steps]
    assert acts.index("ask_another_example") < acts.index("accept")
    assert "too forced" in next(s for s in steps if s["action"] == "ask_another_example")["detail"]["reason"]


# ---------------------------------------------------------------- model router

def chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def fake_providers(handlers: dict) -> Router:
    """A router whose providers answer through `handlers[name](request_json)`."""
    def transport(request: httpx.Request) -> httpx.Response:
        name = request.url.host.split(".")[0]
        return handlers[name](json.loads(request.content))
    ps = [Provider(n, n, f"https://{n}.test/v1", "m", "k", 100, 1000, q, q) for q, n in enumerate(handlers, 1)]
    return Router(ps, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(transport)))


def honest_model(body: dict) -> httpx.Response:
    system, user = body["messages"][0]["content"], json.loads(body["messages"][1]["content"])
    if "game reviewer" in system:
        plies = [c["ply"] for c in user["candidates"]][:2]
        return chat(json.dumps({"plies": plies, "features": {str(p): "exact" for p in plies}}))
    if "game review" in system:
        return chat("```json\n" + json.dumps({"summary": "Here is how this game compares with your others.", "claims": [
            {"ply": c["ply"], "headline": f"About {c['move']}", "text": c["standard_text"]} for c in user["claims"]]}) + "\n```")
    if "study game" in system:
        return chat(json.dumps({"title": "Spot the trap", "rationale": "You keep meeting this early attack.",
                                "objective": "Defend against early queen attacks.",
                                "prompts": ["What is your opponent threatening?"] * len(user["checkpoints"])}))
    return chat(json.dumps({"feedback": "Good thinking about the threat to your king. " +
                            user["standard_feedback"]}))


def test_router_fails_over_past_rate_limits_and_made_up_moves(client):
    ids = setup_games(4)
    calls = {"a": 0, "b": 0}

    def rate_limited(body):
        calls["a"] += 1
        return httpx.Response(429, headers={"retry-after": "120"}, json={"error": "slow down"})

    def inventive(body):
        calls["b"] += 1
        if "game review" in body["messages"][0]["content"] and "game reviewer" not in body["messages"][0]["content"]:
            u = json.loads(body["messages"][1]["content"])
            return chat(json.dumps({"summary": "Bad luck.", "claims": [
                {"ply": c["ply"], "headline": "Blunder", "text": "You should have played Qh4 instead."}
                for c in u["claims"]]}))
        return honest_model(body)

    set_router(fake_providers({"a": rate_limited, "b": inventive, "c": honest_model}))
    r = client.post(f"/api/games/{ids[-1]}/coach-review").json()["review"]
    assert r["mode"] == "model"
    assert all(c["source"] == "model" for c in r["claims"])
    writes = [t for t in r["trace"] if t["action"] == "model:write"]
    assert writes and writes[0]["detail"]["provider"] == "c"
    outcomes = [(a["provider"], a["outcome"]) for a in writes[0]["detail"]["attempts"]]
    assert ("b", "invalid") in outcomes  # Qh4 is not in the evidence, so b's text was rejected
    assert calls["a"] == 1  # after the 429, provider a cooled down and was skipped
    assert ("a", "skipped") in outcomes
    # The second request comes back from the saved review without calling any model.
    before = dict(calls)
    assert client.post(f"/api/games/{ids[-1]}/coach-review").json()["review"]["id"] == r["id"]
    assert calls == before


def test_model_failure_falls_back_to_the_standard_explanations(client):
    ids = setup_games(2)
    set_router(fake_providers({"a": lambda b: chat("I think you played well!"),
                               "b": lambda b: httpx.Response(500)}))
    r = client.post(f"/api/games/{ids[0]}/coach-review").json()["review"]
    assert r["mode"] == "rules" and all(c["source"] == "rules" for c in r["claims"])
    assert any("standard" in f for f in r["fallbacks"])


def test_router_respects_its_own_budgets_and_validation():
    clock = [0.0]
    r = fake_providers({"a": honest_model})
    r.clock = lambda: clock[0]
    r.providers[0].rpm = 1
    body = json.dumps({"candidates": [{"ply": 3}]})
    first = r.complete("choose", "You are the game reviewer.", body, ModelPick)
    assert first and first.value.plies == [3]
    assert r.complete("choose", "You are the game reviewer.", body, ModelPick) is None  # minute budget used
    clock[0] = 61
    assert r.complete("choose", "You are the game reviewer.", body, ModelPick,
                      check=lambda v: "nope") is None  # rejected by the caller's check
    assert "k" not in json.dumps(r.status())  # keys are never reported


def test_providers_come_only_from_environment_keys():
    ps = providers_from_env({"GROQ_API_KEY": "x", "GEMINI_API_KEY": "y", "LLM_ORDER": "groq,gemini",
                             "GEMINI_MODEL": "custom"})
    assert [p.name for p in sorted(ps, key=lambda p: p.quality)] == ["groq", "gemini"]
    assert next(p for p in ps if p.name == "gemini").model == "custom"
    assert providers_from_env({}) == []


def test_visitor_limit_switches_to_rules(client):
    ids = setup_games(2)
    set_router(fake_providers({"a": honest_model}))
    service.limiter.per_hour = 0
    try:
        r = client.post(f"/api/games/{ids[0]}/coach-review").json()["review"]
    finally:
        service.limiter.per_hour = service.MODEL_RUNS_PER_HOUR
    assert r["mode"] == "rules" and "limit" in r["fallbacks"][0]


# ---------------------------------------------------------------- guided study

def test_study_hides_answers_until_revealed_and_keeps_notes(client):
    ids = setup_games(4)
    picks = client.get("/api/studies").json()
    assert picks["picks"], "a recurring mistake should produce a study pick"
    pick = picks["picks"][0]
    assert "key_ply" not in pick and "move_number" not in pick and "Nf6" not in pick["reason"]

    st = client.post("/api/studies", json={"game_id": pick["game_id"], "theme": pick["theme"]}).json()
    raw = json.dumps(st)
    assert "answer" not in raw and "feedback" not in raw and "best" not in raw
    cp = st["checkpoints"][0]
    assert st["positions"][cp["ply"]] == cp["fen"]

    bad = client.put(f"/api/studies/{st['id']}/checkpoints/0", json={"move": "Ke2"})
    assert bad.status_code == 422 and "not a legal move" in bad.json()["detail"]
    ok = client.put(f"/api/studies/{st['id']}/checkpoints/0",
                    json={"plans": "Defend f7", "move": "g6", "reply": "Qf3", "text": "The queen eyes f7."})
    assert ok.status_code == 200

    again = client.get(f"/api/studies/{st['id']}").json()  # reload: notes persist, still no answers
    assert again["checkpoints"][0]["notes"]["move"] == "g6" and again["checkpoints"][0]["notes"]["plans"] == "Defend f7"
    assert "answer" not in json.dumps(again)

    shown = client.post(f"/api/studies/{st['id']}/checkpoints/0/reveal").json()
    c0 = shown["checkpoints"][0]
    assert c0["revealed"] and c0["answer"]["played"] and c0["feedback"]["source"] == "rules"
    assert all("answer" not in c for c in shown["checkpoints"][1:])
    assert client.get("/api/studies").json()["saved"][0]["revealed"] == 1


def test_model_written_study_text_never_names_a_move(client):
    ids = setup_games(4)

    def spoiler(body):
        if "study game" in body["messages"][0]["content"]:
            return chat(json.dumps({"title": "Watch out", "rationale": "Play g6 here.", "objective": "x", "prompts": []}))
        return honest_model(body)
    set_router(fake_providers({"a": spoiler, "b": honest_model}))
    st = client.post("/api/studies", json={"game_id": ids[0], "theme": "tactic"}).json()
    assert st["mode"] == "model" and st["provider"] == "b" and st["title"] == "Spot the trap"
    client.put(f"/api/studies/{st['id']}/checkpoints/0", json={"plans": "Cover f7", "move": "g6"})
    fb = client.post(f"/api/studies/{st['id']}/checkpoints/0/reveal").json()["checkpoints"][0]["feedback"]
    assert fb["source"] == "model" and fb["text"].startswith("Good thinking")


def test_suggest_a_set_includes_evidence_advice(client):
    with db.SessionLocal() as s:
        for i in range(3):
            add_game(s, "e4 e5", played_at=datetime(2025, 1, i + 1))
        add_game(s, "d4 d5", played_at=datetime(2025, 6, 1))
    r = client.post("/api/selection/suggest", json={"target_from": "2025-06-01", "target_to": "2025-06-30",
                                                    "rating_band": 100, "max_comparison": 10}).json()
    assert r["advice"] and r["advice"][0]["level"] == "thin" and r["advice"][0]["comparisons"] == 3
