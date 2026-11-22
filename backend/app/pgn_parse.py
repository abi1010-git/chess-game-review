"""PGN parsing and normalization shared by every import source.

Everything here is pure (no database), so it is easy to test.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import chess
import chess.pgn

SUPPORTED_VARIANTS = {"", "standard", "chess", "from position", "fromposition"}

_LICHESS_RE = re.compile(r"https?://(?:www\.)?lichess\.org/([A-Za-z0-9]{8})(?:[A-Za-z0-9]{4})?(?:[/?#]|$)")
_CHESSCOM_RE = re.compile(r"https?://(?:www\.)?chess\.com/(?:game/)?(live|daily)(?:/game)?/(\d+)")


@dataclass
class ParsedGame:
    index: int  # 1-based position in the source
    label: str
    headers: dict[str, str]
    initial_fen: str
    moves_uci: list[str]
    has_clocks: bool
    pgn: str


@dataclass
class ParseSkip:
    index: int
    label: str
    reason: str


@dataclass
class NormalizedHeaders:
    white: str | None
    black: str | None
    white_rating: int | None
    black_rating: int | None
    result: str
    played_at: datetime | None
    date_raw: str | None
    time_control: str | None
    time_class: str
    rated: bool | None
    eco: str | None
    opening: str | None
    termination: str | None


# ---------------------------------------------------------------- helpers

def clean(value: str | None) -> str | None:
    """Return None for PGN 'unknown' markers ("?", "????.??.??"), keeping unknowns unknown."""
    if value is None:
        return None
    v = value.strip()
    if not v or re.fullmatch(r"[?.]+", v):
        return None
    return v


def parse_int(value: str | None) -> int | None:
    v = clean(value)
    if v is None:
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


def parse_played_at(headers: dict[str, str]) -> datetime | None:
    date = clean(headers.get("UTCDate")) or clean(headers.get("Date"))
    if not date:
        return None
    m = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})", date)
    if not m:
        return None
    time = clean(headers.get("UTCTime")) or clean(headers.get("StartTime")) or "00:00:00"
    t = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", time)
    hh, mm, ss = (int(t[1]), int(t[2]), int(t[3])) if t else (0, 0, 0)
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), hh, mm, ss)
    except ValueError:
        return None


def time_class_from_control(tc: str | None) -> str:
    """Classify like Lichess: estimated duration = base + 40 * increment."""
    if tc is None:
        return "unknown"
    tc = tc.strip()
    if tc == "-" or "/" in tc:  # correspondence / Chess.com daily ("1/259200")
        return "correspondence"
    m = re.fullmatch(r"(\d+)(?:\+(\d+))?", tc)
    if not m:
        return "unknown"
    est = int(m[1]) + 40 * int(m[2] or 0)
    if est < 30:
        return "ultrabullet"
    if est < 180:
        return "bullet"
    if est < 480:
        return "blitz"
    if est < 1500:
        return "rapid"
    return "classical"


def rated_from_event(event: str | None) -> bool | None:
    if not event:
        return None
    e = event.lower()
    if "casual" in e or "unrated" in e:
        return False
    if "rated" in e:
        return True
    return None


def normalize_headers(h: dict[str, str]) -> NormalizedHeaders:
    tc_raw = (h.get("TimeControl") or "").strip()
    time_control = tc_raw if tc_raw and tc_raw != "?" else None
    result = (h.get("Result") or "*").strip()
    if result not in {"1-0", "0-1", "1/2-1/2", "*"}:
        result = "*"
    return NormalizedHeaders(
        white=clean(h.get("White")),
        black=clean(h.get("Black")),
        white_rating=parse_int(h.get("WhiteElo")),
        black_rating=parse_int(h.get("BlackElo")),
        result=result,
        played_at=parse_played_at(h),
        date_raw=clean(h.get("UTCDate")) or clean(h.get("Date")),
        time_control=time_control,
        time_class=time_class_from_control(time_control),
        rated=rated_from_event(h.get("Event")),
        eco=clean(h.get("ECO")),
        opening=clean(h.get("Opening")),
        termination=clean(h.get("Termination")),
    )


def player_result(color: str, result: str) -> str:
    if result == "1/2-1/2":
        return "draw"
    if result == "1-0":
        return "win" if color == "white" else "loss"
    if result == "0-1":
        return "win" if color == "black" else "loss"
    return "unknown"


def detect_platform(*urls: str | None) -> tuple[str, str] | None:
    """Return (provider, provider_game_id) from a game URL, if recognizable."""
    for url in urls:
        if not url:
            continue
        m = _LICHESS_RE.search(url)
        if m:
            return "lichess", m[1]
        m = _CHESSCOM_RE.search(url)
        if m:
            return "chesscom", f"{m[1]}/{m[2]}"
    return None


def has_identifying_metadata(h: dict[str, str]) -> bool:
    """Enough metadata to tell two games with identical moves apart."""
    players = clean(h.get("White")) and clean(h.get("Black"))
    date = clean(h.get("UTCDate")) or clean(h.get("Date"))
    if date and "?" in date:
        date = None
    site = clean(h.get("Site"))
    return bool(players or date or (site and detect_platform(site, h.get("Link"))))


def fingerprint(initial_fen: str, moves_uci: list[str], h: dict[str, str], salt: str = "") -> str:
    keys = ("White", "Black", "Date", "UTCDate", "UTCTime", "StartTime", "Result",
            "Site", "Link", "Event", "Round", "TimeControl")
    meta = [(clean(h.get(k)) or "?").lower() for k in keys]
    payload = "\n".join([initial_fen, " ".join(moves_uci), *meta, salt])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe(h: dict[str, str], index: int) -> str:
    w = clean(h.get("White")) or "?"
    b = clean(h.get("Black")) or "?"
    d = clean(h.get("UTCDate")) or clean(h.get("Date"))
    return f"Game {index}: {w} vs {b}" + (f" ({d})" if d else "")


# ---------------------------------------------------------------- parsing

class _QuietLog(logging.Filter):
    def filter(self, record):  # python-chess logs every illegal move; we report them ourselves
        return False


logging.getLogger("chess.pgn").addFilter(_QuietLog())


def _parse_game(game: chess.pgn.Game, index: int) -> ParsedGame | ParseSkip:
    h = {k: v for k, v in game.headers.items()}
    label = describe(h, index)

    variant = (h.get("Variant") or "").strip().lower()
    if variant not in SUPPORTED_VARIANTS:
        return ParseSkip(index, label, f"Unsupported variant: {h.get('Variant')} (only standard chess for now)")

    if game.errors:
        return ParseSkip(index, label, f"Unreadable or illegal move: {game.errors[0]}")

    try:
        board = game.board()
    except ValueError as e:
        return ParseSkip(index, label, f"Invalid starting position: {e}")
    if not board.is_valid():
        return ParseSkip(index, label, "Invalid starting position")

    moves = [m.uci() for m in game.mainline_moves()]
    if not moves:
        return ParseSkip(index, label, "No moves were played")

    has_clocks = any(node.clock() is not None for node in game.mainline())
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    pgn_text = game.accept(exporter)
    return ParsedGame(index, label, h, board.fen(), moves, has_clocks, pgn_text)


def iter_pgn(text: str, start_index: int = 1) -> Iterator[ParsedGame | ParseSkip]:
    """Yield each game in a (possibly multi-game) PGN, or a skip with the reason."""
    stream = io.StringIO(text.replace("\r\n", "\n").lstrip("﻿"))
    index = start_index - 1
    while True:
        pos = stream.tell()
        try:
            game = chess.pgn.read_game(stream)
        except Exception as e:  # defensive: python-chess is lenient, but never let one game kill a batch
            index += 1
            yield ParseSkip(index, f"Game {index}", f"Could not read PGN: {e}")
            if stream.tell() == pos:
                break
            continue
        if game is None:
            break
        index += 1
        yield _parse_game(game, index)


def inspect_names(text: str) -> dict:
    """Count player names in a PGN so the user can say which one is them."""
    counts: Counter[str] = Counter()
    canonical: dict[str, str] = {}
    valid = invalid = 0
    for item in iter_pgn(text):
        if isinstance(item, ParseSkip):
            invalid += 1
            continue
        valid += 1
        seen_here = set()
        for key in ("White", "Black"):
            name = clean(item.headers.get(key))
            if name and name.lower() not in seen_here:
                seen_here.add(name.lower())
                canonical.setdefault(name.lower(), name)
                counts[name.lower()] += 1
    names = [{"name": canonical[k], "games": c} for k, c in counts.most_common()]
    in_every = [n["name"] for n in names if n["games"] == valid and valid > 0]
    suggested = in_every[0] if len(in_every) == 1 else None
    return {
        "games_found": valid,
        "invalid_games": invalid,
        "names": names,
        "suggested_player": suggested,
        "needs_choice": suggested is None,
    }
