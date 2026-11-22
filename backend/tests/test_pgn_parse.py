from app.pgn_parse import (
    ParsedGame,
    ParseSkip,
    detect_platform,
    fingerprint,
    inspect_names,
    iter_pgn,
    normalize_headers,
    time_class_from_control,
)


def test_iter_pgn_reports_each_invalid_game_and_keeps_going(mixed_pgn):
    items = list(iter_pgn(mixed_pgn.decode()))
    assert len(items) == 8
    parsed = [i for i in items if isinstance(i, ParsedGame)]
    skips = {i.index: i.reason for i in items if isinstance(i, ParseSkip)}
    assert len(parsed) == 5
    assert "illegal" in skips[5].lower()
    assert "variant" in skips[6].lower()
    assert "no moves" in skips[7].lower()


def test_unknown_metadata_stays_unknown(mixed_pgn):
    second = list(iter_pgn(mixed_pgn.decode()))[1]
    n = normalize_headers(second.headers)
    assert n.black_rating is None  # "?" is preserved as unknown, not 0
    assert n.white_rating == 1520
    assert n.time_class == "rapid"
    assert n.rated is True


def test_fingerprint_needs_more_than_moves(mixed_pgn):
    items = [i for i in iter_pgn(mixed_pgn.decode()) if isinstance(i, ParsedGame)]
    a, _, a_again, same_moves_other_game = items[0], items[1], items[2], items[3]
    fp = lambda g: fingerprint(g.initial_fen, g.moves_uci, g.headers)
    assert fp(a) == fp(a_again)
    assert a.moves_uci == same_moves_other_game.moves_uci
    assert fp(a) != fp(same_moves_other_game)


def test_time_classes():
    assert time_class_from_control("60+0") == "bullet"
    assert time_class_from_control("180+2") == "blitz"
    assert time_class_from_control("600+0") == "rapid"
    assert time_class_from_control("1800+30") == "classical"
    assert time_class_from_control("15+0") == "ultrabullet"
    assert time_class_from_control("1/259200") == "correspondence"
    assert time_class_from_control("-") == "correspondence"
    assert time_class_from_control(None) == "unknown"


def test_detect_platform():
    assert detect_platform("https://lichess.org/AbCd1234") == ("lichess", "AbCd1234")
    assert detect_platform("https://lichess.org/AbCd1234wxyz") == ("lichess", "AbCd1234")
    assert detect_platform("https://www.chess.com/game/live/123456") == ("chesscom", "live/123456")
    assert detect_platform("https://www.chess.com/game/daily/987") == ("chesscom", "daily/987")
    assert detect_platform("Club night") is None


def test_inspect_names_suggests_the_common_player():
    pgn = """[White "Me"]\n[Black "A"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n\n[White "B"]\n[Black "me"]\n[Result "0-1"]\n\n1. d4 d5 0-1\n"""
    info = inspect_names(pgn)
    assert info["games_found"] == 2
    assert info["suggested_player"] == "Me"
    assert info["needs_choice"] is False


def test_inspect_names_asks_when_a_single_game_has_two_identities():
    pgn = """[White "Me"]\n[Black "A"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n"""
    info = inspect_names(pgn)
    assert info["needs_choice"] is True
    assert {n["name"] for n in info["names"]} == {"Me", "A"}
