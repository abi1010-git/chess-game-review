#!/usr/bin/env python3
"""Post-deployment smoke test: walks the public workflow in a fresh session and exits non-zero on failure.

    python scripts/smoke_test.py https://your-app.example.com
    python scripts/smoke_test.py https://your-app.example.com --llm-disabled      # site deployed with LLM_DISABLED=1
    python scripts/smoke_test.py http://127.0.0.1:8000 --skip-providers --analysis-timeout 120

Standard library only. It uses its own cookie jar (a brand-new visitor), creates a private guest
space, and deletes it again at the end, so it leaves nothing behind. Checks:
  health and the site itself; the sample history (public demo); batch PGN upload and duplicates;
  Lichess and Chess.com imports; both selection modes; analysis progress; one comparison from each
  phase (opening, middlegame, endgame); independent (guided) and coach review; delete my data.
"""
import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

PGN = """[Event "Smoke"]
[Site "smoke"]
[Date "2025.04.01"]
[White "SmokeTester"]
[Black "Opp1"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 1-0

[Event "Smoke"]
[Site "smoke"]
[Date "2025.04.02"]
[White "Opp2"]
[Black "SmokeTester"]
[Result "0-1"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 Nbd7 7. Rc1 c6 8. Bd3 dxc4 9. Bxc4 Nd5 10. Bxe7 Qxe7 0-1
"""

failures: list[str] = []


class Session:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, files=None, raw=False):
        headers, data = {}, None
        if files:
            boundary = uuid.uuid4().hex
            parts = []
            for k, v in (body or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
            for k, (fname, content) in files.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                             f'Content-Type: application/x-chess-pgn\r\n\r\n{content}\r\n')
            data = ("".join(parts) + f"--{boundary}--\r\n").encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=90) as r:
                payload = r.read()
                return r.status, (payload if raw else (json.loads(payload) if payload else None))
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload.decode(errors="replace")[:200]


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--llm-disabled", action="store_true", help="the site runs with LLM_DISABLED=1: expect rule-based coaching only")
    ap.add_argument("--skip-providers", action="store_true", help="skip the Lichess and Chess.com account imports")
    ap.add_argument("--lichess", default="DrNykterstein")
    ap.add_argument("--chesscom", default="hikaru")
    ap.add_argument("--analysis-timeout", type=int, default=300, help="seconds to wait for one queued game (0 skips)")
    ap.add_argument("--require-worker", action="store_true", help="fail when /api/health/ready reports no analysis worker")
    a = ap.parse_args()
    s = Session(a.url)
    t0 = time.time()

    print("Health and site")
    st, body = s.call("GET", "/api/health")
    check("GET /api/health is ok", st == 200 and body.get("status") == "ok", st)
    st, body = s.call("GET", "/api/health/ready")
    check("GET /api/health/ready reports the database", isinstance(body, dict) and body.get("database") == "ok", st)
    if a.require_worker:
        check("analysis worker is running", st == 200 and body.get("worker"), body)
    elif st == 503:
        print("  note  no analysis worker is running (analysis will queue but not progress)")
    st, html = s.call("GET", "/", raw=True)
    check("site serves the web app over the given URL", st == 200 and b"<div id=\"root\"" in html, st)
    check("a new visitor sees an empty space", s.call("GET", "/api/dashboard")[1]["coverage"]["games"] == 0)
    st, me = s.call("GET", "/api/me")
    check("guest spaces are on (public mode)", st == 200 and me["mode"] == "guest", me)

    print("Public demo: sample history")
    st, body = s.call("POST", "/api/me/sample")
    check("sample history loads", st == 200 and body.get("games", 0) >= 10, body)
    dash = s.call("GET", "/api/dashboard")[1]
    check("dashboard shows analyzed games, trends and a recurring weakness",
          dash["coverage"]["analyzed"] >= 10 and dash["trends"] and any(r["status"] == "recurring" for r in dash["recurring"]))
    games = s.call("GET", "/api/games?limit=200")[1]["items"]
    check("history lists the games with filters", s.call("GET", "/api/games?color=white&result=win")[1]["total"] > 0)

    print("One comparison from each phase")
    seen = {}
    for g in games:
        rep = s.call("GET", f"/api/games/{g['id']}/report")[1]["report"]
        for d in rep["decisions"]:
            if d["comparison"] and d["phase"] not in seen:
                seen[d["phase"]] = (g["id"], d["ply"], d["comparison"]["verdict"])
        if len(seen) == 3:
            break
    for phase in ("opening", "middlegame", "endgame"):
        check(f"{phase}: a decision compared with earlier games", phase in seen, seen.get(phase))

    print("Coach: independent and guided review")
    last = games[0]["id"]
    st, cs = s.call("GET", "/api/coach/status")
    check("coach status is available and names no provider", st == 200 and "model" not in json.dumps(cs), cs)
    if a.llm_disabled:
        check("model is disabled (LLM_DISABLED=1)", cs["enabled"] is False, cs)
    st, rv = s.call("POST", f"/api/games/{last}/coach-review?mode=auto")
    rv = rv.get("review") if isinstance(rv, dict) else None
    check("coach review returns claims with cited moves", st == 200 and rv and rv["claims"] and all(c["refs"] for c in rv["claims"]), rv)
    if a.llm_disabled and rv:
        check("review is rule-based when the model is off", rv["mode"] == "rules")
    picks = s.call("GET", "/api/studies")[1]
    check("study recommendations are offered", bool(picks["picks"]))
    if picks["picks"]:
        p = picks["picks"][0]
        st, study = s.call("POST", "/api/studies", {"game_id": p["game_id"], "theme": p["theme"]})
        check("guided study opens with answers hidden", st == 201 and "answer" not in json.dumps(study) and study["checkpoints"])
        st, ok = s.call("PUT", f"/api/studies/{study['id']}/checkpoints/0", {"plans": "smoke", "move": None, "reply": None, "text": "note"})
        again = s.call("GET", f"/api/studies/{study['id']}")[1]
        check("notes persist across a refresh with answers still hidden",
              st == 200 and again["checkpoints"][0]["notes"].get("plans") == "smoke" and "answer" not in json.dumps(again))
        shown = s.call("POST", f"/api/studies/{study['id']}/checkpoints/0/reveal")[1]
        check("reveal shows the answer for that checkpoint only",
              shown["checkpoints"][0].get("answer") and all("answer" not in c for c in shown["checkpoints"][1:]))

    print("Batch PGN upload and selection modes")
    ids = []
    st, run = s.call("POST", "/api/imports/pgn", {"player_names": "SmokeTester"}, files={"file": ("smoke.pgn", PGN)})
    check("batch PGN upload accepted", st == 202, run)
    for _ in range(30):
        run = s.call("GET", f"/api/imports/{run['id']}")[1]
        if run["status"] not in ("queued", "running"):
            break
        time.sleep(1)
    check("both games imported", run["imported"] == 2, run)
    st, run2 = s.call("POST", "/api/imports/pgn", {"player_names": "SmokeTester"}, files={"file": ("smoke.pgn", PGN)})
    for _ in range(30):
        run2 = s.call("GET", f"/api/imports/{run2['id']}")[1]
        if run2["status"] not in ("queued", "running"):
            break
        time.sleep(1)
    check("uploading the same file again adds no duplicates", run2["imported"] == 0 and run2["duplicates"] == 2, run2)
    uploaded = [g["id"] for g in s.call("GET", "/api/games?provider=pgn&limit=200")[1]["items"] if g["provider_game_id"] is None]
    pv = s.call("POST", "/api/analysis/preview", {"game_ids": uploaded})[1]
    check("manual selection shows a preview", pv["found"] == len(uploaded) and pv["will_queue"] == len(uploaded), pv)
    sug = s.call("POST", "/api/selection/suggest", {"target_from": "2025-03-01", "target_to": "2025-04-30"})[1]
    check("suggested selection explains its groups", "groups" in sug and sug.get("advice") is not None, sug)

    print("Lichess and Chess.com imports")
    if a.skip_providers:
        print("  skipped")
    else:
        for provider, name in (("lichess", a.lichess), ("chesscom", a.chesscom)):
            st, run = s.call("POST", f"/api/imports/account/{provider}", {"username": name, "max_games": 3})
            ok = st == 202
            for _ in range(60 if ok else 0):
                run = s.call("GET", f"/api/imports/{run['id']}")[1]
                if run["status"] not in ("queued", "running"):
                    break
                time.sleep(2)
            check(f"{provider} import of {name} finished with games", ok and run["imported"] + run["duplicates"] > 0, run)
    st, bad = s.call("POST", "/api/imports/account/lichess", {"username": "no such user!"})
    check("a bad username gets a clear message", st == 422 and "username" in json.dumps(bad).lower(), bad)

    print("Analysis progress")
    if a.analysis_timeout and uploaded:
        st, batch = s.call("POST", "/api/analysis/batches", {"game_ids": uploaded[:1], "mode": "manual"})
        check("analysis queued", st == 201, batch)
        states, deadline = [], time.time() + a.analysis_timeout
        while st == 201 and time.time() < deadline:
            b = s.call("GET", f"/api/analysis/batches/{batch['id']}")[1]
            cur = b["progress"]["games"][0]["status"]
            if not states or states[-1] != cur:
                states.append(cur)
            if cur in ("done", "failed", "cancelled"):
                break
            time.sleep(3)
        check("analysis progresses to done (progress was visible)", states and states[-1] == "done", states)
    else:
        print("  skipped")

    print("Delete my data")
    st, gone = s.call("DELETE", "/api/me")
    check("data deleted", st == 200 and gone["deleted"]["games"] > 0, gone)
    check("the space is empty afterwards", s.call("GET", "/api/games")[1]["total"] == 0)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed'} in {time.time() - t0:.0f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
