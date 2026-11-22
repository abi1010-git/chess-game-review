"""Shared types and HTTP retry logic for platform adapters."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import httpx

from .. import config

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

Notify = Callable[[str, str], None]  # (level, message)
Sleep = Callable[[float], None]


class ProviderError(Exception):
    """A user-facing problem talking to a platform (not found, throttled, unavailable)."""


@dataclass
class RawGame:
    """One game as delivered by an adapter, before normalization."""
    pgn: str | None
    provider: str
    provider_game_id: str | None = None
    source_url: str | None = None
    time_class: str | None = None
    rated: bool | None = None
    played_at: datetime | None = None
    variant: str | None = None  # platform's variant name; None = trust the PGN
    label: str | None = None


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )


class RetryPolicy:
    """Bounded retries with exponential backoff for temporary failures."""

    def __init__(self, sleep: Sleep = time.sleep, max_attempts: int = 4, base_delay: float = 1.0,
                 max_delay: float = 60.0, throttle_delay: float = 60.0):
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.throttle_delay = throttle_delay

    def backoff(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))

    @staticmethod
    def retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ProviderError("Usernames may only contain letters, numbers, '_' and '-' (up to 50 characters).")
    return username
