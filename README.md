---
title: Chess Game Review
emoji: ♟️
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: See how your chess decisions change over time
---

# Chess Game Review

A web app that shows how a player's decisions have changed over time and picks whole games from their own history to study.
It is being built in the five phases of the build guide. **Phase 1 (import and select games) and phase 2 (analyze and compare decisions) are complete.**

## What works now (phase 1)

- **Import** from a Lichess username, a Chess.com username, or a PGN file with one or many games.
  - Platform requests run on the server with a limited number of retries and backoff. Throttling, missing months and unknown users are reported in the import result.
  - For PGN uploads, the app lists the names it found and asks which one is you. Each game is then stored from your side.
  - Duplicates are caught by platform game ID, or by a fingerprint built from the starting position, the moves and the game metadata. A game is never merged with another on moves alone.
  - Invalid moves, unsupported variants, games with no moves and games you didn't play are skipped with a reason, and the rest of the batch continues.
  - Unknown metadata, such as a `?` rating, is stored as unknown.
- **Game history**: imported games appear right away. You can filter by date, time control, platform, color, rating, result and analysis status, then select individual games or the whole filtered set.
- **Suggest a set**: pick a target date range. The app proposes those games plus your earlier games from the same platform, time control and rating pool at a similar rating, and explains each group. It widens the rating band only when it has to, and warns about thin pools and missing ratings. You can edit the set before queueing.
- **Analysis queue**: the app shows what will be queued, what is already queued and what is already analyzed before you queue anything. Each game gets one job per analysis version, so a repeated request never creates duplicate work. Jobs use leases with a limited number of retries, which makes worker restarts safe. Cancelled jobs are never marked complete.

## What works now (phase 2)

- **Analysis worker** (`python -m app.worker`): claims queued jobs with a lease, replays each game from its starting position through Stockfish, and stores the evidence for every move: the position before it, the move, scores from your side of the board, three candidate lines, and the engine's expected continuation.
  - Fixed engine and budget per analysis version (v1: Stockfish 18, one thread, 150k nodes per position). The worker refuses a different engine build unless you start a new analysis version.
  - A second, deeper pass (600k nodes) re-checks your close or critical decisions, scoring the move you played directly. A verdict that changes between passes is marked **uncertain**. Mate scores are handled separately from centipawns.
  - Results are written in the same transaction that marks the job done, so a cancelled or taken-over job never shows results. A clean shutdown (Ctrl+C) hands the job back without using up an attempt.
- **Findings** for the opening (delayed development, early king exposure), the middlegame (hanging pieces, double attacks, missed or allowed tactics and mates, pawn breaks, pawn weaknesses, passive pieces) and the endgame (passed pawns, king activity, converting and defending). Each one names its evidence: an engine line, a board feature, or both. Tactical and strategic findings can apply to the same move, and good decisions ("found the only defence") are recorded too.
- **Low-information moves are not judged**: forced moves, obvious recaptures, taking a loose piece, and moves in already decided positions.
- **Compare this game**: each key decision is matched against your own earlier decisions, first by exact position, then by the same material and pawn structure, then by the same kind of decision in the same phase with similar material. The target game and duplicates of it are excluded. Each verdict (better, worse, similar, mixed evidence, not enough evidence) shows its sample size and match quality, and links to the earlier positions.
- **Lost games** separate the earlier decision that narrowed your options from the decisive mistake and from any later concrete chance you missed. Something is only called a recurring weakness after counting it across comparable games, including wins and draws.
- **Short games** and games with only forced moves stay in the history, without an improvement verdict. The report shows the strongest observation instead.
- **Home**: *How I've Been Playing* (accuracy by month with sample sizes, per-phase rates, recurring findings and strengths, recent games) beside *What to Study Next*. Whole-game study picks come in phase 3.
- **Game report**: board, move list, expected-score graph, findings by phase, comparisons and the loss explanation.

## Running it locally

Requirements: Python 3.12 or newer, and Node 20 or newer.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

In a second terminal, start the analysis worker (it needs Stockfish, see below):

```bash
cd backend
.venv/Scripts/python -m app.worker            # --threads 2 by default; --once exits when the queue is empty
```

In a third terminal:

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:5173. The development server forwards `/api` to the backend on port 8000.

Run the tests:

```bash
cd backend && .venv/Scripts/python -m pytest -q
```

`tests/test_milestone_engine.py` runs hand-reviewed positions through real Stockfish and is skipped when no engine is found. The other tests use a small deterministic fake engine.

### Stockfish

Analysis version v1 expects **Stockfish 18**. Download it from https://stockfishchess.org/download/ and either put the binary in `backend/engines/` (git-ignored), put `stockfish` on your PATH, or set `STOCKFISH_PATH`.

## Running it as one container

The `Dockerfile` builds the website, installs Python and the official Stockfish 18 build, and starts the API and the analysis worker together on port 7860:

```bash
docker build -t chess-game-review .
docker run -p 7860:7860 chess-game-review
```

### Hosting on Hugging Face Spaces

Every push to `main` is deployed to a Hugging Face Space by `.github/workflows/deploy-space.yml`. One-time setup:

1. Create a Space with the **Docker** SDK (blank template).
2. Create a Hugging Face access token with **write** permission.
3. In this GitHub repo, add the token as the secret `HF_TOKEN`, and add the variable `HF_SPACE` set to `<your-hf-username>/<space-name>`.

On the free Space hardware the database lives inside the container, so imported games and analyses are lost when the Space restarts or redeploys. There are no logins yet (phase 5): anyone with the link shares the same profile.

## Configuration (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | SQLite at `backend/data/app.db` | Set to `postgresql+psycopg://…` for PostgreSQL (install `psycopg[binary]`) |
| `ANALYSIS_VERSION` | `v1` | Change it when engine settings change, so games show as needing new analysis |
| `MAX_UPLOAD_BYTES` | 5 MB | Largest PGN file accepted |
| `MAX_GAMES_PER_IMPORT` | 2000 | Most games one account import can fetch |
| `MAX_GAMES_PER_BATCH` | 500 | Most games in one analysis batch |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed frontend origins |
| `STOCKFISH_PATH` | PATH, then `backend/engines/` | Stockfish binary for the worker |
| `WORKER_THREADS` | 2 | Parallel single-threaded engines in one worker |
| `WORKER_LEASE_SECONDS` | 300 | How long a job stays claimed without a heartbeat |
| `ENGINE_ALLOW_MISMATCH` | off | Set to `1` to run a different engine build than the profile names |

## Layout

```
backend/app/
  pgn_parse.py        PGN parsing, normalization, fingerprints, time-control classes
  importers/          lichess.py, chesscom.py (platform adapters), service.py (store + dedup)
  selection.py        filters and the "Suggest a set" algorithm
  jobs.py             idempotent enqueue, lease-based claim/heartbeat/complete/fail/cancel
  engine.py           Stockfish over UCI behind a small interface
  analysis.py         two-pass engine analysis, per-move evidence, low-information flags
  features.py         board features (material, pawn structure, king safety, mobility, passed pawns)
  findings.py         finding detectors with their evidence
  compare.py          matching like decisions, verdicts, loss explanation, game report
  dashboard.py        "How I've Been Playing"
  worker.py           the analysis worker process
  main.py             FastAPI routes
backend/tests/        unit and API tests (fake HTTP transport, no network)
frontend/src/         React screens: Home, Game report, Import, Game history, Suggest a set, Analysis queue
```

## Not yet done

- Phase 3: study recommendations, guided review with hidden answers, and the optional specialist coordination.
- Database migrations (Alembic) are planned before deployment. For now the tables are created at startup.
- Authentication and per-user isolation (phase 5). A single local profile is used for now.
