"""Route model calls across several free LLM providers, best first, with quota tracking and failover.

Every provider here speaks the OpenAI-style chat completions format, so one request shape works
for all of them. A provider is used only when its key is set. For each request the router walks
the providers in order of preference for the task, skipping any that are cooling down after a
rate limit or have used up their own minute/day budget, and moves on when a call fails, times out
or returns something that does not validate. When none can answer, it returns None and the
caller falls back to the rule-based text.

Keys come only from environment variables and are never logged or returned by any API.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
Task = Literal["choose", "write"]


@dataclass
class Provider:
    name: str
    label: str
    base_url: str
    model: str
    key: str
    rpm: int  # our own per-minute budget, kept under the provider's free limit
    rpd: int  # our own per-day budget
    quality: int  # 1 = best writing
    speed: int  # 1 = fastest
    minute: list[float] = field(default_factory=list)
    day_count: int = 0
    day: str = ""
    cooldown_until: float = 0.0
    last_error: str | None = None
    calls: int = 0
    failures: int = 0
    discovered: bool = False  # asked the provider for its model list after a 404

    def has_room(self, now: float) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day != today:
            self.day, self.day_count = today, 0
        self.minute = [t for t in self.minute if now - t < 60]
        return now >= self.cooldown_until and len(self.minute) < self.rpm and self.day_count < self.rpd

    def note_call(self, now: float) -> None:
        self.minute.append(now)
        self.day_count += 1
        self.calls += 1


# name, label, base URL, default model, rpm, rpd, quality, speed. Defaults stay below each free tier;
# override the model with <NAME>_MODEL and the budgets with LLM_<NAME>_RPM / LLM_<NAME>_RPD.
CATALOG = [
    ("gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", 8, 200, 1, 3),
    ("groq", "Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", 25, 900, 2, 1),
    ("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "llama-3.3-70b", 25, 900, 3, 2),
    ("mistral", "Mistral", "https://api.mistral.ai/v1", "mistral-small-latest", 2, 500, 4, 5),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free", 15, 45, 5, 4),
    ("github", "GitHub Models", "https://models.github.ai/inference", "openai/gpt-4.1-mini", 10, 45, 6, 6),
]
KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "cerebras": "CEREBRAS_API_KEY",
           "mistral": "MISTRAL_API_KEY", "openrouter": "OPENROUTER_API_KEY", "github": "GITHUB_MODELS_TOKEN"}


def providers_from_env(env: dict | None = None) -> list[Provider]:
    env = os.environ if env is None else env
    out = []
    for name, label, url, model, rpm, rpd, q, sp in CATALOG:
        key = (env.get(KEY_ENV[name]) or "").strip()
        if not key:
            continue
        up = name.upper()
        out.append(Provider(name, label, url, env.get(f"{up}_MODEL") or model, key,
                            int(env.get(f"LLM_{up}_RPM") or rpm), int(env.get(f"LLM_{up}_RPD") or rpd), q, sp))
    order = [n.strip() for n in (env.get("LLM_ORDER") or "").split(",") if n.strip()]
    if order:  # explicit preference replaces the built-in quality ranking
        rank = {n: i for i, n in enumerate(order)}
        for p in out:
            p.quality = rank.get(p.name, len(rank) + p.quality)
    return out


@dataclass
class Attempt:
    provider: str
    outcome: str  # ok | rate_limited | error | timeout | invalid | skipped
    ms: int = 0
    note: str | None = None


@dataclass
class RouterResult:
    value: BaseModel
    provider: str
    attempts: list[Attempt]


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


class Router:
    def __init__(self, providers: list[Provider], client_factory: Callable[[], httpx.Client] | None = None,
                 timeout: float = 25.0, clock: Callable[[], float] = time.monotonic):
        self.providers = providers
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=timeout))
        self.timeout = timeout
        self.clock = clock
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def ordered(self, task: Task) -> list[Provider]:
        key = (lambda p: (p.speed, p.quality)) if task == "choose" else (lambda p: (p.quality, p.speed))
        return sorted(self.providers, key=key)

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            return {"enabled": self.enabled, "providers": [
                {"name": p.name, "label": p.label, "model": p.model, "available": p.has_room(now),
                 "cooling_down": now < p.cooldown_until, "used_today": p.day_count, "daily_budget": p.rpd,
                 "calls": p.calls, "failures": p.failures, "last_error": p.last_error}
                for p in self.ordered("write")]}

    def complete(self, task: Task, system: str, user: str, schema: type[T],
                 check: Callable[[T], str | None] | None = None, max_attempts: int = 3) -> RouterResult | None:
        """Ask the best available provider for JSON matching `schema`; fail over on any problem.

        `check` may reject a parsed answer (returning a reason), which also moves on to the next
        provider. Returns None when no provider produced an acceptable answer.
        """
        attempts: list[Attempt] = []
        tried = 0
        for p in self.ordered(task):
            if tried >= max_attempts:
                break
            now = self.clock()
            with self.lock:
                if not p.has_room(now):
                    attempts.append(Attempt(p.name, "skipped", note="cooling down" if now < p.cooldown_until
                                            else "budget used"))
                    continue
                p.note_call(now)
            tried += 1
            t0 = time.monotonic()
            outcome, note, value = self._call(p, system, user, schema)
            if value is not None and check is not None:
                reason = check(value)
                if reason:
                    outcome, note, value = "invalid", reason, None
            ms = int((time.monotonic() - t0) * 1000)
            attempts.append(Attempt(p.name, outcome, ms, note))
            if value is not None:
                p.last_error = None
                return RouterResult(value, p.name, attempts)
            with self.lock:
                p.failures += 1
                p.last_error = f"{outcome}: {note}" if note else outcome
        return None

    def _call(self, p: Provider, system: str, user: str, schema: type[T]) -> tuple[str, str | None, T | None]:
        outcome, note, value = self._call_once(p, system, user, schema)
        if note == "HTTP 404" and not p.discovered:
            # The model was probably retired. Ask the provider what it serves now and retry once.
            p.discovered = True
            newer = self._discover(p)
            if newer and newer != p.model:
                p.model = newer
                outcome, note, value = self._call_once(p, system, user, schema)
        return outcome, note, value

    def _discover(self, p: Provider) -> str | None:
        try:
            with self.client_factory() as client:
                r = client.get(f"{p.base_url}/models", headers={"Authorization": f"Bearer {p.key}"}, timeout=self.timeout)
            ids = [m.get("id", "") for m in r.json().get("data", [])] if r.status_code < 400 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        return pick_model(p.model, ids)

    def _call_once(self, p: Provider, system: str, user: str, schema: type[T]) -> tuple[str, str | None, T | None]:
        body = {"model": p.model, "temperature": 0.2, "max_tokens": 900,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {p.key}", "Content-Type": "application/json"}
        if p.name == "openrouter":
            headers["X-Title"] = "Chess Game Review"
        try:
            with self.client_factory() as client:
                r = client.post(f"{p.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException:
            return "timeout", None, None
        except httpx.HTTPError as e:
            return "error", type(e).__name__, None
        self._read_limits(p, r)
        if r.status_code == 429:
            return "rate_limited", None, None
        if r.status_code >= 400:
            return "error", f"HTTP {r.status_code}", None
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            return "invalid", "unexpected response shape", None
        data = _extract_json(content)
        if data is None:
            return "invalid", "no JSON in reply", None
        try:
            return "ok", None, schema.model_validate(data)
        except ValidationError as e:
            return "invalid", f"schema: {e.error_count()} errors", None

    def _read_limits(self, p: Provider, r: httpx.Response) -> None:
        """Respect what the provider says about its own limits."""
        now = self.clock()
        h = {k.lower(): v for k, v in r.headers.items()}
        wait = None
        if r.status_code == 429:
            wait = 60.0
            ra = h.get("retry-after")
            if ra:
                try:
                    wait = max(1.0, float(ra))
                except ValueError:
                    pass
        remaining = h.get("x-ratelimit-remaining-requests") or h.get("x-ratelimit-remaining")
        if remaining is not None:
            try:
                if int(float(remaining)) <= 0:
                    reset = h.get("x-ratelimit-reset-requests") or h.get("x-ratelimit-reset")
                    wait = max(wait or 0, _seconds(reset) or 60.0)
            except ValueError:
                pass
        if wait:
            with self.lock:
                p.cooldown_until = max(p.cooldown_until, now + wait)


_SPECIAL = ("lite", "tts", "audio", "image", "live", "embed", "preview", "exp", "thinking", "native",
            "computer", "robotics", "vision", "guard", "search", "nano", "8b", "learnlm")


def _version(model_id: str) -> tuple[float, ...]:
    m = re.search(r"(\d+(?:\.\d+)*)", model_id)
    return tuple(float(x) for x in m.group(1).split(".")) if m else ()


def pick_model(current: str, ids: list[str]) -> str | None:
    """From a provider's model list, the newest model of the same family as `current`
    (e.g. gemini-2.5-flash -> the newest plain gemini-*-flash), avoiding special variants."""
    words = [w for w in re.split(r"[-_/.]", current.lower()) if w and not re.fullmatch(r"[\d.]+", w)]
    names = [i.removeprefix("models/") for i in ids if i]
    fam = [n for n in names if all(w in n.lower() for w in words)]
    plain = [n for n in fam if not any(x in n.lower() and x not in current.lower() for x in _SPECIAL)]
    pool = plain or [n for n in fam if not any(x in n.lower() and x not in current.lower()
                                               for x in _SPECIAL if x not in ("preview", "exp"))]
    return max(pool, key=lambda n: (_version(n), -len(n)), default=None)


def _seconds(v: str | None) -> float | None:
    """Parse reset hints like '7.5s', '2m30s', '120' or an epoch in milliseconds."""
    if not v:
        return None
    try:
        x = float(v)
        if x > 1e11:  # epoch ms (OpenRouter)
            return max(1.0, x / 1000 - time.time())
        return x
    except ValueError:
        pass
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|s|m|h)", v):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


_router: Router | None = None


def get_router() -> Router:
    """The process-wide router, built from environment variables on first use."""
    global _router
    if _router is None:
        disabled = os.environ.get("LLM_DISABLED", "") == "1"
        _router = Router([] if disabled else providers_from_env())
    return _router


def set_router(r: Router | None) -> None:
    """Replace the router (tests, or after changing keys)."""
    global _router
    _router = r
