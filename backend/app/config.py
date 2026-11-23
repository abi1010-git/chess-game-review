"""Runtime settings, read from environment variables with local-dev defaults."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CHESS_REVIEW_DATA_DIR", BASE_DIR / "data"))

# SQLite for local development; set DATABASE_URL=postgresql+psycopg://... for PostgreSQL.
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"

# Heroku-style postgres:// URLs, and bare postgresql://, should use the psycopg 3 driver.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]
# SQLite normally skips migrations (tables are created directly); set 1 to use them there too.
MIGRATE_SQLITE = os.environ.get("MIGRATE_SQLITE", "") == "1"

# Bump when the engine settings or finding logic change, so games show as needing re-analysis.
ANALYSIS_VERSION = os.environ.get("ANALYSIS_VERSION", "v1")

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 5 * 1024 * 1024))
MAX_GAMES_PER_IMPORT = int(os.environ.get("MAX_GAMES_PER_IMPORT", 2000))
DEFAULT_GAMES_PER_IMPORT = int(os.environ.get("DEFAULT_GAMES_PER_IMPORT", 300))
MAX_GAMES_PER_BATCH = int(os.environ.get("MAX_GAMES_PER_BATCH", 500))

# Generic client name for public chess APIs (no personal contact details).
USER_AGENT = os.environ.get("CHESS_REVIEW_USER_AGENT", "chess-game-review/0.1 (self-hosted)")

CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

# ---------------------------------------------------------------- engine analysis (phase 2)
# Path to a Stockfish binary. When unset, the worker looks on PATH, then in backend/engines/.
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH") or None
ENGINE_DIR = BASE_DIR / "engines"

# Each analysis version is tied to one engine profile: a fixed engine and a fixed node budget.
# Threads=1 and a node limit keep results reproducible on the same engine build. Change the
# profile only together with ANALYSIS_VERSION.
ENGINE_PROFILES: dict[str, dict] = {
    "v1": {
        "engine": "Stockfish 18",
        "threads": 1,
        "hash_mb": 64,
        "first_nodes": 150_000,   # every position, both sides
        "multipv": 3,             # candidate continuations stored per position
        "deep_nodes": 600_000,    # second pass on candidate moments
        "deep_max": 6,            # most deep checks per game
        "stable_win_delta": 10.0, # first vs deep pass may differ by this many win-% points
    },
}
ENGINE_PROFILE = ENGINE_PROFILES.get(ANALYSIS_VERSION) or ENGINE_PROFILES["v1"]
# Allow a different engine build than the profile names (results then are not comparable).
ENGINE_ALLOW_MISMATCH = os.environ.get("ENGINE_ALLOW_MISMATCH", "") == "1"
WORKER_LEASE_SECONDS = int(os.environ.get("WORKER_LEASE_SECONDS", 300))

# Built frontend (frontend/dist) served by the API in single-container deployments.
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", BASE_DIR.parent / "frontend" / "dist"))

# ---------------------------------------------------------------- phase 5: public launch
# single: one local profile, no login (local use). guest: every browser gets its own private space,
# found through a random cookie, so uploads, notes, reports and jobs are isolated per visitor.
AUTH_MODE = os.environ.get("AUTH_MODE", "single")
GUEST_RETENTION_DAYS = int(os.environ.get("GUEST_RETENTION_DAYS", 30))   # idle spaces are deleted after this
MAX_GAMES_PER_PROFILE = int(os.environ.get("MAX_GAMES_PER_PROFILE", 1000))
MAX_QUEUED_JOBS_PER_PROFILE = int(os.environ.get("MAX_QUEUED_JOBS_PER_PROFILE", 60))
MAX_QUEUED_JOBS_TOTAL = int(os.environ.get("MAX_QUEUED_JOBS_TOTAL", 400))
MAX_GUEST_PROFILES_PER_IP_PER_HOUR = int(os.environ.get("MAX_GUEST_PROFILES_PER_IP_PER_HOUR", 10))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", 240))   # all /api requests, per IP
RATE_LIMIT_IMPORTS_PER_HOUR = int(os.environ.get("RATE_LIMIT_IMPORTS_PER_HOUR", 20))  # imports + uploads, per IP
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "1") != "0"
COOKIE_NAME = "cgr_session"
