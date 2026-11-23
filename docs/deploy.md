# Launching the public site

This is the checklist for the public release. Anything marked **you decide** costs money, creates an
account or changes live settings, so it is left to you.

## What runs

One container (see `Dockerfile`): the API and website (`uvicorn`) plus the Stockfish worker (`start.sh`),
talking to one PostgreSQL database. Visitors get a private space tied to their browser (`AUTH_MODE=guest`,
set in the Dockerfile). The sample history is bundled, so the demo needs no engine time.

## Decisions for you

| Decision | Options | Notes |
| --- | --- | --- |
| **Database** | A free PostgreSQL tier from Neon or Supabase, or a paid Render Postgres | The free Render disk is wiped on every deploy, so SQLite there loses all data. Render's own free Postgres expires, so it is not suitable. Check each provider's current free-tier limits and whether they pause idle databases. |
| **Host / compute** | Keep Render's free web service (512 MB, one engine, sleeps when idle), or a small paid plan | The worker is bounded: one engine thread, 64 MB hash, 150k nodes per position (about 30 to 60 seconds a game). A paid instance with more memory lets you set `WORKER_THREADS=2`. Free plans sleep, so the first visit after idle takes about a minute. |
| **Model** | Off (`LLM_DISABLED=1`) or on with free-tier keys | Zero paid AI APIs is a hard rule; the free providers have daily limits and the app falls back to rule-based text. Keys live only in the host's environment settings, never in the repo or the browser. |

## Environment variables to set on the host (you set the secrets)

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | the PostgreSQL connection string (`postgresql://…`; the app adds the right driver) |
| `AUTH_MODE` | `guest` (already the Docker default) |
| `CORS_ORIGINS` | the public site's own origin, or leave unset (the site and API share one origin) |
| `TRUSTED_PROXY_HOPS` | `1` on Render (number of proxies appending to `X-Forwarded-For`; used for rate limits) |
| `LLM_DISABLED` | `1` to turn the model off; or set provider keys (`GEMINI_API_KEY`, …) and leave it unset |
| `MAX_GAMES_PER_PROFILE`, `MAX_QUEUED_JOBS_PER_PROFILE`, `MAX_QUEUED_JOBS_TOTAL`, `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_IMPORTS_PER_HOUR`, `GUEST_RETENTION_DAYS` | optional; defaults are 1000, 60, 400, 240, 20, 30 |

HTTPS is provided by the host's edge (Render serves HTTPS and forwards `X-Forwarded-Proto`); the app sets the
session cookie `Secure` and sends HSTS when it sees it.

## Database: migrations and backups

- **Migrations** run automatically at startup (`alembic upgrade head`, guarded by a database lock). To run them by hand: `cd backend && alembic upgrade head`.
- **Backups:** use the provider's automatic backups or point-in-time restore if the tier has them, and keep your own copy with `scripts/backup_db.sh` (a `pg_dump`). Test a restore once into a scratch database before relying on it.
- **Retention:** guest spaces idle for `GUEST_RETENTION_DAYS` are deleted automatically. Visitors can download or delete their data at any time from the bar at the top of the site.

## Monitoring

- `GET /api/health` is the liveness check (database reachable). Point the host's health check at it.
- `GET /api/health/ready` also reports the worker and queue and returns **503 when no worker has reported in the last 30 seconds**. Point an uptime monitor at it to find out when analysis stops.
- Unexpected errors return a short reference (`reference ab12cd34`) and log the full trace on the server.

## Deploy and verify

1. Deploy (Render: Manual Deploy, Deploy latest commit).
2. Run the smoke test against the public URL from any machine:

   ```
   python scripts/smoke_test.py https://YOUR-SITE
   python scripts/smoke_test.py https://YOUR-SITE --llm-disabled      # when LLM_DISABLED=1 is set
   ```

   It acts as a brand-new visitor and checks the public-URL list below, then deletes its own space.
3. Check by hand in a fresh private browser window (the smoke test covers the API; this covers the screens).

## Public-URL checklist

- [ ] Public demo: a new visitor can press **Try a sample history** and see the dashboard
- [ ] Lichess import and Chess.com import
- [ ] Batch PGN upload, and uploading it again adds no duplicates
- [ ] Both selection modes (**Pick games** and **Suggest a set**), with a preview
- [ ] Progress updates while a game is analyzed
- [ ] One comparison from each phase: opening, middlegame, endgame
- [ ] Independent review (open a game report) and guided review (**Study this game**, answers hidden until revealed)
- [ ] Download my data and Delete my data
- [ ] Repeat all of the above with `LLM_DISABLED=1`
- [ ] `/api/health` and `/api/health/ready` respond; smoke test passes after every deploy
