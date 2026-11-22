import json

import httpx

LICHESS = "https://lichess.org/api/games/user/"
CHESSCOM = "https://api.chess.com/pub/player/"


def lichess_game(gid, white, black, result="1-0", speed="blitz", variant="standard", created=1735689600000,
                 moves="1. e4 e5 2. Nf3 Nc6", welo=1500, belo=1490, tc="180+2"):
    pgn = (f'[Event "Rated {speed.title()} game"]\n[Site "https://lichess.org/{gid}"]\n[Date "2025.01.01"]\n'
           f'[White "{white}"]\n[Black "{black}"]\n[Result "{result}"]\n[UTCDate "2025.01.01"]\n'
           f'[UTCTime "00:00:00"]\n[WhiteElo "{welo}"]\n[BlackElo "{belo}"]\n[TimeControl "{tc}"]\n'
           f'[Variant "{variant.title()}"]\n\n'
           f'1. e4 {{ [%clk 0:03:00] }} 1... e5 {{ [%clk 0:03:00] }} {moves[12:] if len(moves) > 12 else ""} {result}\n')
    return {"id": gid, "rated": True, "variant": variant, "speed": speed, "createdAt": created,
            "players": {"white": {"user": {"name": white}}, "black": {"user": {"name": black}}}, "pgn": pgn}


def ndjson(*games):
    return httpx.Response(200, content="\n".join(json.dumps(g) for g in games).encode(),
                          headers={"Content-Type": "application/x-ndjson"})


def run_import(client, path, **body):
    r = client.post(path, json=body)
    assert r.status_code == 202, r.text
    run = client.get(f"/api/imports/{r.json()['id']}").json()
    assert run["status"] not in ("queued", "running")
    return run


def upload(client, data: bytes, names: str, filename="games.pgn"):
    r = client.post("/api/imports/pgn", files={"file": (filename, data, "application/x-chess-pgn")},
                    data={"player_names": names})
    assert r.status_code == 202, r.text
    return client.get(f"/api/imports/{r.json()['id']}").json()


# ---------------------------------------------------------------- PGN upload

def test_multi_game_upload_with_duplicates_and_invalid_entries(client, mixed_pgn):
    inspect = client.post("/api/imports/pgn/inspect", files={"file": ("g.pgn", mixed_pgn)}).json()
    assert inspect["games_found"] == 5
    assert inspect["invalid_games"] == 3
    assert inspect["names"][0]["name"] == "MeTheTester"
    assert inspect["needs_choice"] is True  # one valid game doesn't include the player

    run = upload(client, mixed_pgn, "MeTheTester")
    assert run["seen"] == 8
    assert run["imported"] == 3  # A, B and the same-moves-but-different game
    assert run["duplicates"] == 1
    assert run["skipped"] == 4  # illegal move, Chess960, no moves, player not in game
    reasons = " | ".join(s["reason"] for s in run["skips"])
    assert "illegal" in reasons.lower() and "variant" in reasons.lower()
    assert "did not play" in reasons
    assert run["status"] == "completed_with_issues"

    games = client.get("/api/games").json()
    assert games["total"] == 3
    by_opp = {g["opponent_name"]: g for g in games["items"]}
    assert by_opp["OpponentOne"]["player_color"] == "white"
    assert by_opp["OpponentOne"]["player_result"] == "win"
    assert by_opp["OpponentTwo"]["player_color"] == "black"
    assert by_opp["OpponentTwo"]["player_result"] == "loss"
    assert by_opp["OpponentTwo"]["player_rating"] is None  # unknown stays unknown
    assert by_opp["OpponentTwo"]["time_class"] == "rapid"
    assert by_opp["OpponentThree"]["player_result"] == "draw"


def test_reupload_is_fully_deduplicated(client, mixed_pgn):
    upload(client, mixed_pgn, "MeTheTester")
    again = upload(client, mixed_pgn, "methetester")  # case-insensitive name
    assert again["imported"] == 0
    assert again["duplicates"] == 4
    assert client.get("/api/games").json()["total"] == 3


def test_upload_requires_player_and_rejects_ambiguous_side(client):
    pgn = b'[White "Alias1"]\n[Black "Alias2"]\n[Date "2025.01.01"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'
    r = client.post("/api/imports/pgn", files={"file": ("g.pgn", pgn)}, data={"player_names": " "})
    assert r.status_code == 422
    run = upload(client, pgn, "Alias1,Alias2")
    assert run["imported"] == 0
    assert "ambiguous" in run["skips"][0]["reason"]


def test_games_without_identifying_metadata_are_not_merged_on_moves_alone(client):
    pgn = b'[White "Me"]\n[Black "?"]\n[Result "*"]\n\n1. e4 e5 *\n\n[White "Me"]\n[Black "?"]\n[Result "*"]\n\n1. e4 e5 *\n'
    run = upload(client, pgn, "Me")
    assert run["imported"] == 2
    assert any("kept as separate" in n["message"] for n in run["notices"])


def test_upload_size_cap(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)
    r = client.post("/api/imports/pgn", files={"file": ("g.pgn", b"x" * 200)}, data={"player_names": "Me"})
    assert r.status_code == 413


# ---------------------------------------------------------------- Lichess

def test_lichess_import_and_side_detection(client, web):
    web.add(LICHESS + "Tester", ndjson(
        lichess_game("AAAA1111", "Tester", "Opp1"),
        lichess_game("BBBB2222", "Opp2", "tester", result="0-1", speed="rapid", tc="600+0"),
        lichess_game("CCCC3333", "Tester", "Opp3", variant="chess960"),
    ))
    run = run_import(client, "/api/imports/account/lichess", username="Tester", max_games=50)
    assert run["imported"] == 2 and run["skipped"] == 1
    assert "chess960" in run["skips"][0]["reason"]
    req = web.requests[0]
    assert req.headers["Accept"] == "application/x-ndjson"
    assert req.url.params["max"] == "50" and req.url.params["pgnInJson"] == "true"

    games = {g["provider_game_id"]: g for g in client.get("/api/games").json()["items"]}
    assert games["AAAA1111"]["player_color"] == "white"
    assert games["BBBB2222"]["player_color"] == "black"
    assert games["BBBB2222"]["player_result"] == "win"
    assert games["BBBB2222"]["time_class"] == "rapid"
    assert games["AAAA1111"]["has_clocks"] is True
    assert games["AAAA1111"]["source_url"] == "https://lichess.org/AAAA1111"

    # Importing again (e.g. a duplicate request) adds nothing.
    again = run_import(client, "/api/imports/account/lichess", username="Tester")
    assert again["imported"] == 0 and again["duplicates"] == 2


def test_lichess_upload_of_already_imported_game_is_duplicate(client, web):
    g = lichess_game("DDDD4444", "Tester", "Opp1")
    web.add(LICHESS + "Tester", ndjson(g))
    run_import(client, "/api/imports/account/lichess", username="Tester")
    # Same game downloaded from the website later: matched by its Lichess game ID.
    run = upload(client, g["pgn"].replace('[Event "Rated Blitz game"]', '[Event "Downloaded"]').encode(), "Tester")
    assert run["duplicates"] == 1 and run["imported"] == 0


def test_lichess_throttling_is_retried_and_reported(client, web):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return ndjson(lichess_game("EEEE5555", "Tester", "Opp1"))

    web.add(LICHESS + "Tester", handler)
    run = run_import(client, "/api/imports/account/lichess", username="Tester")
    assert run["imported"] == 1
    assert web.sleeps == [60.0]  # Lichess asks for a full minute after a 429
    assert any("throttling" in n["message"] for n in run["notices"])
    assert run["status"] == "completed_with_issues"


def test_lichess_unknown_user_and_bad_username(client, web):
    run = run_import(client, "/api/imports/account/lichess", username="NoSuchUser")
    assert run["status"] == "failed"
    assert "not found" in run["notices"][0]["message"]
    r = client.post("/api/imports/account/lichess", json={"username": "../admin"})
    assert r.status_code == 422


def test_lichess_persistent_server_error_fails_with_bounded_retries(client, web):
    web.add(LICHESS + "Tester", httpx.Response(503))
    run = run_import(client, "/api/imports/account/lichess", username="Tester")
    assert run["status"] == "failed"
    assert len(web.requests) == 4
    assert web.sleeps == [1.0, 2.0, 4.0]


# ---------------------------------------------------------------- Chess.com

def chesscom_game(gid, white, black, wres="win", bres="checkmated", end=1735700000, rules="chess",
                  time_class="blitz", pgn_moves="1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#"):
    result = "1-0" if wres == "win" else "0-1" if bres == "win" else "1/2-1/2"
    pgn = (f'[Event "Live Chess"]\n[Site "Chess.com"]\n[Date "2025.01.01"]\n[White "{white}"]\n[Black "{black}"]\n'
           f'[Result "{result}"]\n[WhiteElo "1200"]\n[BlackElo "1210"]\n[TimeControl "300"]\n'
           f'[Link "https://www.chess.com/game/live/{gid}"]\n\n{pgn_moves} {result}\n')
    return {"url": f"https://www.chess.com/game/live/{gid}", "pgn": pgn, "time_control": "300", "end_time": end,
            "rated": True, "time_class": time_class, "rules": rules,
            "white": {"username": white, "rating": 1200, "result": wres},
            "black": {"username": black, "rating": 1210, "result": bres}}


def test_chesscom_import_walks_archives_and_survives_a_bad_month(client, web):
    base = CHESSCOM + "tester/games"
    web.add(base + "/archives", httpx.Response(200, json={"archives": [
        f"{base}/2024/12", f"{base}/2025/01", f"{base}/2025/02"]}))
    web.add(base + "/2024/12", httpx.Response(200, json={"games": [chesscom_game(1, "Tester", "Opp", end=1733000000)]}))
    web.add(base + "/2025/01", httpx.Response(500))
    web.add(base + "/2025/02", httpx.Response(200, json={"games": [
        chesscom_game(2, "OppB", "Tester", wres="resigned", bres="win", end=1738500000),
        chesscom_game(3, "Tester", "OppC", rules="bughouse", end=1738600000),
        {**chesscom_game(4, "Tester", "OppD", end=1738700000), "pgn": None},
    ]}))
    run = run_import(client, "/api/imports/account/chesscom", username="Tester")
    assert run["imported"] == 2
    assert run["skipped"] == 2
    assert any("2025-01" in n["message"] for n in run["notices"])
    assert run["status"] == "completed_with_issues"
    games = {g["provider_game_id"]: g for g in client.get("/api/games").json()["items"]}
    assert games["live/2"]["player_color"] == "black" and games["live/2"]["player_result"] == "win"
    assert games["live/1"]["time_class"] == "blitz"
    assert all(r.headers["User-Agent"] for r in web.requests)


def test_chesscom_date_range_limits_months(client, web):
    base = CHESSCOM + "tester/games"
    web.add(base + "/archives", httpx.Response(200, json={"archives": [f"{base}/2024/12", f"{base}/2025/02"]}))
    web.add(base + "/2025/02", httpx.Response(200, json={"games": [chesscom_game(9, "Tester", "Opp", end=1738500000)]}))
    run = run_import(client, "/api/imports/account/chesscom", username="Tester", since="2025-02-01", until="2025-02-28")
    assert run["imported"] == 1
    assert not any("/2024/12" in str(r.url) for r in web.requests)


def test_chesscom_unknown_user(client, web):
    run = run_import(client, "/api/imports/account/chesscom", username="ghost")
    assert run["status"] == "failed"
    assert "not found" in run["notices"][0]["message"]
