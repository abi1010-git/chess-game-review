"""Public-launch protections: per-IP rate limits, and the real client address behind a proxy."""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config

# Number of proxies in front of the app that append to X-Forwarded-For (Render: 1). The client
# address is the entry that many places from the right; anything further left is client-supplied.
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", 1))


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - TRUSTED_PROXY_HOPS)]
    return request.client.host if request.client else "local"


class SlidingWindow:
    """At most `limit` events per `window` seconds per key. In memory, so per process."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.hits: dict[tuple, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: tuple, limit: int, window: float) -> tuple[bool, int]:
        now = self.clock()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(q[0] + window - now) + 1)
            q.append(now)
            if len(self.hits) > 20000:  # drop idle keys so memory stays bounded
                for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - 3700][:5000]:
                    self.hits.pop(k, None)
            return True, 0


windows = SlidingWindow()


def _too_many(retry: int, what: str) -> JSONResponse:
    return JSONResponse({"detail": f"Too many {what}. Please wait about {retry} seconds and try again."},
                        status_code=429, headers={"Retry-After": str(retry)})


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if config.RATE_LIMIT_ENABLED and path.startswith("/api/") and not path.startswith("/api/health"):
        ip = client_ip(request)
        ok, retry = windows.allow(("api", ip), config.RATE_LIMIT_PER_MINUTE, 60)
        if not ok:
            return _too_many(retry, "requests")
        if request.method == "POST" and path.startswith("/api/imports/") and not path.endswith("/inspect"):
            ok, retry = windows.allow(("import", ip), config.RATE_LIMIT_IMPORTS_PER_HOUR, 3600)
            if not ok:
                return _too_many(retry, "imports from this address")
    return await call_next(request)
