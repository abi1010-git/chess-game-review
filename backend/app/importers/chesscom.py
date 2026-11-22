"""Chess.com adapter: walks the player's monthly public game archives.

https://www.chess.com/news/view/published-data-api
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator

import httpx

from ..pgn_parse import detect_platform
from .base import Notify, ProviderError, RawGame, RetryPolicy, validate_username

BASE_URL = "https://api.chess.com/pub"
CLASS_MAP = {"bullet": "bullet", "blitz": "blitz", "rapid": "rapid", "daily": "correspondence"}
_MONTH_RE = re.compile(r"/(\d{4})/(\d{2})/?$")


class ChessComAdapter:
    provider = "chesscom"

    def __init__(self, username: str, client: httpx.Client, notify: Notify, retry: RetryPolicy | None = None,
                 since: datetime | None = None, until: datetime | None = None, max_games: int | None = None):
        self.username = validate_username(username).lower()
        self.client = client
        self.notify = notify
        self.retry = retry or RetryPolicy(throttle_delay=10.0)
        self.since = since
        self.until = until
        self.max_games = max_games

    def _get_json(self, url: str, what: str) -> dict:
        last_problem = "unknown error"
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                resp = self.client.get(url)
            except httpx.TransportError as e:
                last_problem = f"network error ({type(e).__name__})"
                if attempt < self.retry.max_attempts:
                    self.retry.sleep(self.retry.backoff(attempt))
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise ProviderError(f"Chess.com sent an unreadable response for {what}.")
            if resp.status_code in (404, 410):
                raise ProviderError(f"Chess.com has no public data for {what}.")
            if resp.status_code == 429:
                wait = self.retry.retry_after(resp) or self.retry.backoff(attempt) * 5
                self.notify("warning", f"Chess.com is throttling requests; waiting {int(wait)}s before retrying.")
                last_problem = "throttled by Chess.com (HTTP 429)"
                if attempt < self.retry.max_attempts:
                    self.retry.sleep(wait)
                continue
            if resp.status_code >= 500:
                last_problem = f"Chess.com returned HTTP {resp.status_code}"
                if attempt < self.retry.max_attempts:
                    self.retry.sleep(self.retry.backoff(attempt))
                continue
            raise ProviderError(f"Chess.com rejected the request for {what} (HTTP {resp.status_code}).")
        raise ProviderError(f"Could not fetch {what} from Chess.com after {self.retry.max_attempts} attempts: {last_problem}.")

    def _month_in_range(self, url: str) -> bool:
        m = _MONTH_RE.search(url)
        if not m:
            return True
        y, mo = int(m[1]), int(m[2])
        if self.since and (y, mo) < (self.since.year, self.since.month):
            return False
        if self.until and (y, mo) > (self.until.year, self.until.month):
            return False
        return True

    def iter_games(self) -> Iterator[RawGame]:
        try:
            archives = self._get_json(f"{BASE_URL}/player/{self.username}/games/archives",
                                      f"user '{self.username}'").get("archives", [])
        except ProviderError as e:
            if "no public data" in str(e):
                raise ProviderError(f"Chess.com user '{self.username}' was not found.") from e
            raise
        months = [u for u in archives if self._month_in_range(u)]
        if not months:
            self.notify("info", "Chess.com has no public game archives for this user and date range.")
            return
        delivered = 0
        for url in reversed(months):  # newest month first, so a game cap keeps recent games
            m = _MONTH_RE.search(url)
            month_label = f"{m[1]}-{m[2]}" if m else url
            try:
                games = self._get_json(url, f"the {month_label} archive").get("games", [])
            except ProviderError as e:
                self.notify("warning", f"Skipped the {month_label} archive: {e}")
                continue
            games.sort(key=lambda g: g.get("end_time") or 0, reverse=True)
            for g in games:
                ended = g.get("end_time")
                ended_dt = datetime.fromtimestamp(ended, tz=timezone.utc).replace(tzinfo=None) if ended else None
                if ended_dt and self.since and ended_dt < self.since:
                    continue
                if ended_dt and self.until and ended_dt > self.until:
                    continue
                delivered += 1
                yield self._to_raw(g, ended_dt)
                if self.max_games is not None and delivered >= self.max_games:
                    return
        if delivered == 0:
            self.notify("info", "Chess.com returned no public games for this user and date range.")

    def _to_raw(self, g: dict, ended: datetime | None) -> RawGame:
        url = g.get("url")
        found = detect_platform(url)
        gid = found[1] if found else g.get("uuid")
        rules = g.get("rules", "chess")
        return RawGame(
            pgn=g.get("pgn"),
            provider=self.provider,
            provider_game_id=gid,
            source_url=url,
            time_class=CLASS_MAP.get(g.get("time_class", "")),
            rated=g.get("rated"),
            played_at=ended,
            variant="standard" if rules == "chess" else rules,
            label=f"Chess.com game {gid}",
        )
