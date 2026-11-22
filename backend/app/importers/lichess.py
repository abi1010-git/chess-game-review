"""Lichess adapter: streams a user's games from the by-user export endpoint (NDJSON).

https://lichess.org/api#tag/Games/operation/apiGamesUser
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import httpx

from .base import Notify, ProviderError, RawGame, RetryPolicy, validate_username

BASE_URL = "https://lichess.org"
SPEED_TO_CLASS = {
    "ultraBullet": "ultrabullet",
    "bullet": "bullet",
    "blitz": "blitz",
    "rapid": "rapid",
    "classical": "classical",
    "correspondence": "correspondence",
}
SUPPORTED = {"standard", "fromPosition"}
MAX_RESUMES = 3


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class LichessAdapter:
    provider = "lichess"

    def __init__(self, username: str, client: httpx.Client, notify: Notify, retry: RetryPolicy | None = None,
                 since: datetime | None = None, until: datetime | None = None, max_games: int | None = None):
        self.username = validate_username(username)
        self.client = client
        self.notify = notify
        self.retry = retry or RetryPolicy()
        self.since = since
        self.until = until
        self.max_games = max_games

    @contextmanager
    def _open(self, params: dict):
        url = f"{BASE_URL}/api/games/user/{self.username}"
        headers = {"Accept": "application/x-ndjson"}
        last_problem = "unknown error"
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                req = self.client.build_request("GET", url, params=params, headers=headers)
                resp = self.client.send(req, stream=True)
            except httpx.TransportError as e:
                last_problem = f"network error ({type(e).__name__})"
                if attempt < self.retry.max_attempts:
                    self.retry.sleep(self.retry.backoff(attempt))
                continue
            if resp.status_code == 200:
                try:
                    yield resp
                finally:
                    resp.close()
                return
            resp.close()
            if resp.status_code == 404:
                raise ProviderError(f"Lichess user '{self.username}' was not found.")
            if resp.status_code == 429:
                # Lichess asks clients to wait a full minute after a 429.
                wait = self.retry.retry_after(resp) or self.retry.throttle_delay
                self.notify("warning", f"Lichess is throttling requests; waiting {int(wait)}s before retrying.")
                last_problem = "throttled by Lichess (HTTP 429)"
                if attempt < self.retry.max_attempts:
                    self.retry.sleep(wait)
                continue
            if resp.status_code >= 500:
                last_problem = f"Lichess returned HTTP {resp.status_code}"
                if attempt < self.retry.max_attempts:
                    self.retry.sleep(self.retry.backoff(attempt))
                continue
            raise ProviderError(f"Lichess rejected the request (HTTP {resp.status_code}).")
        raise ProviderError(f"Could not reach Lichess after {self.retry.max_attempts} attempts: {last_problem}.")

    def iter_games(self) -> Iterator[RawGame]:
        until_ms = _ms(self.until) if self.until else None
        remaining = self.max_games
        resumes = 0
        delivered = 0
        while True:
            params: dict = {"pgnInJson": "true", "clocks": "true", "opening": "true", "sort": "dateDesc"}
            if self.since:
                params["since"] = _ms(self.since)
            if until_ms is not None:
                params["until"] = until_ms
            if remaining is not None:
                params["max"] = remaining
            last_created: int | None = None
            try:
                with self._open(params) as resp:
                    for line in resp.iter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            self.notify("warning", "Lichess sent an unreadable line; it was ignored.")
                            continue
                        last_created = data.get("createdAt") or last_created
                        delivered += 1
                        yield self._to_raw(data)
                        if remaining is not None:
                            remaining -= 1
                            if remaining <= 0:
                                return
                if delivered == 0:
                    self.notify("info", "Lichess returned no public games for this user and date range.")
                return
            except (httpx.TransportError, httpx.StreamError):
                resumes += 1
                if last_created is None or resumes > MAX_RESUMES:
                    raise ProviderError(
                        f"The Lichess download stopped early after {delivered} games. "
                        "Games received so far were kept; run the import again to continue.")
                self.notify("warning", "Connection to Lichess dropped; resuming from the last game received.")
                until_ms = last_created - 1
                self.retry.sleep(self.retry.backoff(resumes))

    def _to_raw(self, d: dict) -> RawGame:
        gid = d.get("id")
        created = d.get("createdAt")
        return RawGame(
            pgn=d.get("pgn"),
            provider=self.provider,
            provider_game_id=gid,
            source_url=f"{BASE_URL}/{gid}" if gid else None,
            time_class=SPEED_TO_CLASS.get(d.get("speed", ""), None),
            rated=d.get("rated"),
            played_at=datetime.fromtimestamp(created / 1000, tz=timezone.utc).replace(tzinfo=None) if created else None,
            variant="standard" if d.get("variant") in SUPPORTED else d.get("variant"),
            label=f"Lichess game {gid}",
        )
