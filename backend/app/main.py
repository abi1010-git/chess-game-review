"""FastAPI app: imports, game history, selection, the analysis queue, reports and the dashboard."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, compare, config, db, demo, jobs, limits, selection
from .dashboard import dashboard as build_dashboard
from .importers.base import ProviderError, RetryPolicy, make_client, validate_username
from .importers.chesscom import ChessComAdapter
from .importers.lichess import LichessAdapter
from .importers.service import execute_import, remember_identity
from .coach import service as coach_service
from .models import AnalysisBatch, AnalysisJob, AnalysisRun, Game, ImportRun, Study, WorkerHeartbeat, utcnow
from .pgn_parse import inspect_names
from .schemas import (
    AccountImportRequest,
    BatchCreate,
    BatchOut,
    GameDetail,
    GameOut,
    GamePage,
    ImportRunOut,
    SelectionRequest,
    StudyCreate,
    StudyNotes,
    SuggestRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db.engine is None:
        db.init_db()
    task = asyncio.create_task(_purge_loop()) if config.AUTH_MODE == "guest" else None
    yield
    if task:
        task.cancel()


log = logging.getLogger("chess_review.api")


async def _purge_loop():
    """Once at startup and then every 6 hours: delete guest spaces idle for the retention period."""
    while True:
        try:
            def run():
                with db.SessionLocal() as s:
                    return auth.purge_idle_guests(s)
            n = await asyncio.to_thread(run)
            if n:
                log.info("deleted %s idle guest space(s)", n)
        except Exception:
            log.exception("purging idle guest spaces failed")
        await asyncio.sleep(6 * 3600)


app = FastAPI(title="Chess Game Review API", version="0.1.0", lifespan=lifespan)
app.middleware("http")(limits.rate_limit_middleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

# Overridable in tests (fake HTTP transport, no real sleeping).
app.state.http_client_factory = make_client
app.state.retry_factory = RetryPolicy

SessionDep = Annotated[Session, Depends(db.get_session)]
ProfileDep = Annotated[int, Depends(auth.current_profile)]
ADAPTERS = {"lichess": LichessAdapter, "chesscom": ChessComAdapter}


def _dt(d: date | None, end: bool = False) -> datetime | None:
    if d is None:
        return None
    return datetime.combine(d, time.max if end else time.min)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Never show a stack trace; give the visitor a reference they can quote."""
    ref = uuid.uuid4().hex[:8]
    log.exception("unhandled error %s on %s %s", ref, request.method, request.url.path)
    return JSONResponse({"detail": f"Something went wrong on our side (reference {ref}). Please try again."},
                        status_code=500)


@app.get("/api/health")
def health(s: SessionDep):
    """Liveness plus a database check: 200 means the API can answer and reach the database."""
    s.execute(select(1))
    return {"status": "ok", "analysis_version": config.ANALYSIS_VERSION}


@app.get("/api/health/ready")
def ready(s: SessionDep):
    """Readiness for monitoring: database, analysis worker, queue depth and whether the model is on.

    Returns 503 when the worker is missing, so an uptime monitor notices that analysis has stopped.
    """
    s.execute(select(1))
    w = worker_status(s)
    queued = s.scalar(select(func.count(AnalysisJob.id)).where(AnalysisJob.status.in_(("queued", "running")))) or 0
    body = {"status": "ok" if w["worker_available"] else "degraded", "database": "ok",
            "worker": w["worker_available"], "queued_jobs": queued, "analysis_version": config.ANALYSIS_VERSION,
            "auth_mode": config.AUTH_MODE, "model_enabled": coach_service.get_router().enabled,
            "worker_error": w.get("worker_error")}
    return JSONResponse(body, status_code=200 if w["worker_available"] else 503)


# ---------------------------------------------------------------- your space: sample data, export, delete

def _owned_profile(s: Session, profile_id: int):
    from .models import Profile
    return s.get(Profile, profile_id) if profile_id else None


@app.get("/api/me")
def me(s: SessionDep, profile_id: ProfileDep):
    from .models import Profile
    p = _owned_profile(s, profile_id)
    n = s.scalar(select(func.count(Game.id)).where(Game.profile_id == profile_id)) or 0 if p else 0
    return {"mode": config.AUTH_MODE, "has_space": p is not None, "games": n, "sample_available": demo.sample_available(),
            "retention_days": config.GUEST_RETENTION_DAYS if config.AUTH_MODE == "guest" else None,
            "limits": {"max_games": config.MAX_GAMES_PER_PROFILE, "max_file_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
                       "max_queued": config.MAX_QUEUED_JOBS_PER_PROFILE}}


@app.post("/api/me/sample")
def load_sample_history(s: SessionDep, profile_id: ProfileDep):
    try:
        n = demo.load_sample(s, profile_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"games": n}


@app.get("/api/me/export")
def export_my_data(s: SessionDep, profile_id: ProfileDep):
    """Everything this space holds that the visitor created: games, import history, study notes."""
    from .models import CoachReview
    games = list(s.scalars(select(Game).where(Game.profile_id == profile_id).order_by(Game.id)))
    studies = list(s.scalars(select(Study).where(Study.profile_id == profile_id)))
    payload = {
        "exported_at": utcnow().isoformat(),
        "games": [{"id": g.id, "provider": g.provider, "source_url": g.source_url, "played_at": g.played_at,
                   "white": g.white, "black": g.black, "result": g.result, "player_color": g.player_color,
                   "pgn": g.pgn} for g in games],
        "studies": [{"game_id": st.game_id, "title": st.title, "notes": st.notes} for st in studies],
        "coach_reviews": [{"game_id": r.game_id, "summary": (r.result or {}).get("summary")}
                          for r in s.scalars(select(CoachReview).where(CoachReview.profile_id == profile_id))],
    }
    return JSONResponse(jsonable_encoder(payload),
                        headers={"Content-Disposition": 'attachment; filename="chess-game-review-export.json"'})


@app.delete("/api/me")
def delete_my_data(response: Response, s: SessionDep, profile_id: ProfileDep):
    """Permanently delete every game, analysis, note and review in this space (and the space itself)."""
    if not profile_id:
        return {"deleted": {"games": 0, "studies": 0}}
    guest = config.AUTH_MODE == "guest"
    counts = auth.erase_profile(s, profile_id, drop_profile=guest)
    if guest:
        response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"deleted": counts}


# ---------------------------------------------------------------- imports

@app.post("/api/imports/account/{provider}", response_model=ImportRunOut, status_code=202)
def import_account(provider: str, body: AccountImportRequest, background: BackgroundTasks,
                   s: SessionDep, profile_id: ProfileDep):
    if provider not in ADAPTERS:
        raise HTTPException(404, "Unknown provider. Use 'lichess' or 'chesscom'.")
    try:
        username = validate_username(body.username)
    except ProviderError as e:
        raise HTTPException(422, str(e))
    if body.since and body.until and body.since > body.until:
        raise HTTPException(422, "The start date must be before the end date.")
    if _active_imports(s, profile_id) >= 3:
        raise HTTPException(429, "Three imports are already running. Wait for one to finish.")
    _check_space(s, profile_id)

    name = "Lichess" if provider == "lichess" else "Chess.com"
    run = ImportRun(profile_id=profile_id, source=provider, label=f"{name}: {username}",
                    params={"username": username, "since": body.since and body.since.isoformat(),
                            "until": body.until and body.until.isoformat(), "max_games": body.max_games})
    s.add(run)
    s.commit()
    remember_identity(s, profile_id, provider, username)

    client_factory: Callable = app.state.http_client_factory
    retry_factory: Callable = app.state.retry_factory
    adapter_cls = ADAPTERS[provider]

    def run_import():
        with client_factory() as client:
            def factory(notify):
                return adapter_cls(username, client, notify, retry=retry_factory(),
                                   since=_dt(body.since), until=_dt(body.until, end=True),
                                   max_games=body.max_games)
            execute_import(db.SessionLocal, run.id, [username], "account", adapter_factory=factory)

    background.add_task(run_import)
    return run


def _check_space(s: Session, profile_id: int) -> None:
    n = s.scalar(select(func.count(Game.id)).where(Game.profile_id == profile_id)) or 0
    if n >= config.MAX_GAMES_PER_PROFILE:
        raise HTTPException(409, f"Your space holds {config.MAX_GAMES_PER_PROFILE} games, the most allowed. "
                                 "Delete your data from the menu to start again.")


def _active_imports(s: Session, profile_id: int) -> int:
    return s.scalar(select(func.count(ImportRun.id)).where(
        ImportRun.profile_id == profile_id, ImportRun.status.in_(("queued", "running")))) or 0


async def _read_pgn(file: UploadFile) -> str:
    data = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"PGN files are limited to {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise HTTPException(422, "Could not read the file as text.")


@app.post("/api/imports/pgn/inspect")
async def inspect_pgn(file: Annotated[UploadFile, File()]):
    text = await _read_pgn(file)
    return inspect_names(text)


@app.post("/api/imports/pgn", response_model=ImportRunOut, status_code=202)
async def import_pgn(background: BackgroundTasks, s: SessionDep, profile_id: ProfileDep,
                     file: Annotated[UploadFile, File()],
                     player_names: Annotated[str, Form(description="Comma-separated names that are you")]):
    names = [n.strip() for n in player_names.split(",") if n.strip()]
    if not names:
        raise HTTPException(422, "Choose which player in the PGN is you.")
    text = await _read_pgn(file)
    if _active_imports(s, profile_id) >= 3:
        raise HTTPException(429, "Three imports are already running. Wait for one to finish.")
    _check_space(s, profile_id)
    run = ImportRun(profile_id=profile_id, source="pgn", label=f"PGN upload: {file.filename or 'file'}",
                    params={"filename": file.filename, "player_names": names})
    s.add(run)
    s.commit()
    for n in names:
        remember_identity(s, profile_id, "pgn", n)
    background.add_task(execute_import, db.SessionLocal, run.id, names, "upload", pgn_text=text)
    return run


@app.get("/api/imports", response_model=list[ImportRunOut])
def list_imports(s: SessionDep, profile_id: ProfileDep, limit: int = Query(20, le=100)):
    return list(s.scalars(select(ImportRun).where(ImportRun.profile_id == profile_id)
                          .order_by(ImportRun.id.desc()).limit(limit)))


@app.get("/api/imports/{run_id}", response_model=ImportRunOut)
def get_import(run_id: int, s: SessionDep, profile_id: ProfileDep):
    run = s.get(ImportRun, run_id)
    if run is None or run.profile_id != profile_id:
        raise HTTPException(404, "Import not found.")
    return run


# ---------------------------------------------------------------- games

def game_filter(
    date_from: date | None = None,
    date_to: date | None = None,
    time_class: Annotated[list[str], Query()] = [],
    provider: Annotated[list[str], Query()] = [],
    color: str | None = None,
    rating_min: int | None = None,
    rating_max: int | None = None,
    rated: bool | None = None,
    result: Annotated[list[str], Query()] = [],
    analysis: str | None = None,
    suggested: bool = False,
) -> selection.GameFilter:
    return selection.GameFilter(date_from, date_to, time_class, provider, color, rating_min, rating_max,
                                rated, result, analysis, suggested)


FilterDep = Annotated[selection.GameFilter, Depends(game_filter)]


def _filtered(s: Session, stmt, f: selection.GameFilter, profile_id: int):
    stmt = selection.apply_filter(stmt, f, profile_id)
    if f.suggested:
        stmt = stmt.where(Game.id.in_(list(selection.default_suggestion(s, profile_id)["roles"]) or [-1]))
    return stmt


def _with_status(s: Session, games: list[Game], roles: dict | None = None) -> list[dict]:
    ids = [g.id for g in games]
    statuses = dict(s.execute(select(AnalysisJob.game_id, AnalysisJob.status).where(
        AnalysisJob.game_id.in_(ids), AnalysisJob.analysis_version == config.ANALYSIS_VERSION)).all()) if ids else {}
    # A game analyzed only under an older analysis version needs analyzing again to count.
    current = set(s.scalars(select(AnalysisRun.game_id).where(
        AnalysisRun.game_id.in_(ids), AnalysisRun.analysis_version == config.ANALYSIS_VERSION))) if ids else set()
    older = dict(s.execute(select(AnalysisRun.game_id, func.max(AnalysisRun.analysis_version)).where(
        AnalysisRun.game_id.in_(ids), AnalysisRun.analysis_version != config.ANALYSIS_VERSION)
        .group_by(AnalysisRun.game_id)).all()) if ids else {}
    out = []
    for g in games:
        d = GameOut.model_validate(g).model_dump()
        d["analysis_status"] = statuses.get(g.id, "none")
        d["outdated_version"] = older.get(g.id) if g.id not in current else None
        d["suggested"] = (roles or {}).get(g.id)
        out.append(d)
    return out


@app.get("/api/games", response_model=GamePage)
def list_games(s: SessionDep, profile_id: ProfileDep, f: FilterDep,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = s.scalar(_filtered(s, select(func.count(Game.id)), f, profile_id))
    stmt = _filtered(s, select(Game), f, profile_id)
    stmt = stmt.order_by(Game.played_at.desc().nulls_last(), Game.id.desc()).limit(limit).offset(offset)
    roles = selection.default_suggestion(s, profile_id)["roles"] if total else {}
    return {"total": total, "items": _with_status(s, list(s.scalars(stmt)), roles)}


@app.get("/api/games/ids")
def list_game_ids(s: SessionDep, profile_id: ProfileDep, f: FilterDep):
    """All ids matching the filter, for 'select the whole filtered set'."""
    ids = list(s.scalars(_filtered(s, select(Game.id), f, profile_id).limit(config.MAX_GAMES_PER_BATCH + 1)))
    return {"ids": ids[: config.MAX_GAMES_PER_BATCH], "truncated": len(ids) > config.MAX_GAMES_PER_BATCH}


@app.get("/api/games/facets")
def game_facets(s: SessionDep, profile_id: ProfileDep):
    return selection.facets(s, profile_id)


@app.post("/api/games/by-ids", response_model=list[GameOut])
def games_by_ids(body: SelectionRequest, s: SessionDep, profile_id: ProfileDep):
    games = list(s.scalars(select(Game).where(Game.profile_id == profile_id, Game.id.in_(body.game_ids))))
    order = {gid: i for i, gid in enumerate(body.game_ids)}
    games.sort(key=lambda g: order.get(g.id, 0))
    return _with_status(s, games)


@app.get("/api/games/{game_id}", response_model=GameDetail)
def get_game(game_id: int, s: SessionDep, profile_id: ProfileDep):
    g = s.get(Game, game_id)
    if g is None or g.profile_id != profile_id:
        raise HTTPException(404, "Game not found.")
    d = GameDetail.model_validate(g).model_dump()
    ws = _with_status(s, [g])[0]
    d["analysis_status"], d["outdated_version"] = ws["analysis_status"], ws["outdated_version"]
    return d


# ---------------------------------------------------------------- selection & analysis queue

@app.post("/api/analysis/preview")
def preview_selection(body: SelectionRequest, s: SessionDep, profile_id: ProfileDep):
    return jobs.preview(s, profile_id, body.game_ids)


@app.post("/api/selection/suggest")
def suggest_selection(body: SuggestRequest, s: SessionDep, profile_id: ProfileDep):
    if body.target_from > body.target_to:
        raise HTTPException(422, "The start date must be before the end date.")
    result = selection.suggest(s, profile_id, body.target_from, body.target_to,
                               rating_band=body.rating_band, max_comparison=body.max_comparison)
    all_ids = [gid for g in result["groups"] for gid in g["game_ids"]]
    result["preview"] = jobs.preview(s, profile_id, all_ids) if all_ids else None
    from .coach.advice import sample_advice
    result["advice"] = sample_advice(s, profile_id, result)
    return result


def _batch_out(s: Session, batch: AnalysisBatch, summary: dict | None = None) -> dict:
    return {"id": batch.id, "mode": batch.mode, "analysis_version": batch.analysis_version,
            "rationale": batch.rationale, "created_at": batch.created_at,
            "progress": jobs.batch_progress(s, batch), "summary": summary}


def _check_queue_room(s: Session, profile_id: int, adding: int) -> None:
    active = AnalysisJob.status.in_(("queued", "running"))
    mine = s.scalar(select(func.count(AnalysisJob.id)).where(AnalysisJob.profile_id == profile_id, active)) or 0
    if mine + adding > config.MAX_QUEUED_JOBS_PER_PROFILE:
        raise HTTPException(429, f"You can have at most {config.MAX_QUEUED_JOBS_PER_PROFILE} games waiting for analysis "
                                 f"({mine} already waiting). Pick fewer games or wait for some to finish.")
    total = s.scalar(select(func.count(AnalysisJob.id)).where(active)) or 0
    if total + adding > config.MAX_QUEUED_JOBS_TOTAL:
        raise HTTPException(503, "The analysis queue is full right now. Please try again in a little while.")


@app.post("/api/analysis/batches", response_model=BatchOut, status_code=201)
def create_batch(body: BatchCreate, s: SessionDep, profile_id: ProfileDep):
    if not jobs.preview(s, profile_id, body.game_ids)["found"]:
        raise HTTPException(404, "None of these games were found.")
    _check_queue_room(s, profile_id, jobs.preview(s, profile_id, body.game_ids)["will_queue"])
    for attempt in range(2):
        try:
            batch, summary = jobs.enqueue(s, profile_id, body.game_ids, body.mode, body.rationale, body.roles)
            break
        except IntegrityError:
            if attempt == 1:
                raise HTTPException(409, "Another request queued these games at the same time. Try again.")
    return _batch_out(s, batch, summary)


@app.get("/api/analysis/batches", response_model=list[BatchOut])
def list_batches(s: SessionDep, profile_id: ProfileDep, limit: int = Query(10, le=50)):
    batches = s.scalars(select(AnalysisBatch).where(AnalysisBatch.profile_id == profile_id)
                        .order_by(AnalysisBatch.id.desc()).limit(limit))
    return [_batch_out(s, b) for b in batches]


@app.get("/api/analysis/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, s: SessionDep, profile_id: ProfileDep):
    b = s.get(AnalysisBatch, batch_id)
    if b is None or b.profile_id != profile_id:
        raise HTTPException(404, "Batch not found.")
    return _batch_out(s, b)


@app.post("/api/analysis/batches/{batch_id}/cancel")
def cancel_batch(batch_id: int, s: SessionDep, profile_id: ProfileDep):
    b = s.get(AnalysisBatch, batch_id)
    if b is None or b.profile_id != profile_id:
        raise HTTPException(404, "Batch not found.")
    return {"cancelled": jobs.cancel_batch(s, profile_id, batch_id)}


@app.get("/api/analysis/queue")
def queue_summary(s: SessionDep, profile_id: ProfileDep):
    rows = s.execute(select(AnalysisJob.status, func.count()).where(
        AnalysisJob.profile_id == profile_id, AnalysisJob.analysis_version == config.ANALYSIS_VERSION)
        .group_by(AnalysisJob.status)).all()
    return {"analysis_version": config.ANALYSIS_VERSION, "counts": {k: v for k, v in rows}, **worker_status(s)}


def worker_status(s: Session) -> dict:
    recent = utcnow() - timedelta(seconds=30)
    beats = list(s.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen.desc()).limit(10)))
    live = [b for b in beats if b.last_seen >= recent and not b.error]
    err = next((b.error for b in beats if b.error and b.last_seen >= utcnow() - timedelta(hours=1)), None)
    return {"worker_available": bool(live), "workers": len(live),
            "engine": live[0].engine_name if live else None, "worker_error": None if live else err}


# ---------------------------------------------------------------- reports and dashboard

@app.get("/api/dashboard")
def get_dashboard(s: SessionDep, profile_id: ProfileDep):
    d = build_dashboard(s, profile_id)
    d.update(worker_status(s))
    return d


@app.get("/api/games/{game_id}/report")
def get_report(game_id: int, s: SessionDep, profile_id: ProfileDep):
    g = s.get(Game, game_id)
    if g is None or g.profile_id != profile_id:
        raise HTTPException(404, "Game not found.")
    game = GameDetail.model_validate(g).model_dump()
    ws = _with_status(s, [g])[0]
    game["analysis_status"], game["outdated_version"] = ws["analysis_status"], ws["outdated_version"]
    report = compare.game_report(s, g)
    return {"game": game, "report": report, **worker_status(s)}


# ---------------------------------------------------------------- phase 3: coach reviews and studies

def visitor_id(request: Request) -> str:
    """Who is asking, for the per-visitor model limit (first proxy hop on hosted deploys)."""
    return limits.client_ip(request)


VisitorDep = Annotated[str, Depends(visitor_id)]


def _own_game(s: Session, game_id: int, profile_id: int) -> Game:
    g = s.get(Game, game_id)
    if g is None or g.profile_id != profile_id:
        raise HTTPException(404, "Game not found.")
    return g


def _own_study(s: Session, study_id: int, profile_id: int) -> Study:
    st = s.get(Study, study_id)
    if st is None or st.profile_id != profile_id:
        raise HTTPException(404, "Study not found.")
    return st


@app.get("/api/coach/status")
def coach_status(visitor: VisitorDep):
    st = coach_service.coach_status(visitor)
    if config.AUTH_MODE == "guest":  # the public site never reveals which provider or model is behind it
        st = {"enabled": st["enabled"], "providers": [{"available": p["available"]} for p in st["providers"]],
              "visitor_runs_left_this_hour": st["visitor_runs_left_this_hour"]}
    return st


@app.get("/api/games/{game_id}/coach-review")
def get_coach_review(game_id: int, s: SessionDep, profile_id: ProfileDep):
    return {"review": coach_service.cached_review(s, _own_game(s, game_id, profile_id))}


@app.post("/api/games/{game_id}/coach-review")
def run_coach_review(game_id: int, s: SessionDep, profile_id: ProfileDep, visitor: VisitorDep,
                     mode: str = Query("auto", pattern="^(auto|rules)$")):
    g = _own_game(s, game_id, profile_id)
    try:
        return {"review": coach_service.review_game(s, g, visitor, mode)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/studies")
def list_studies(s: SessionDep, profile_id: ProfileDep):
    from .coach.study import study_picks
    return study_picks(s, profile_id)


@app.post("/api/studies", status_code=201)
def create_study(body: StudyCreate, s: SessionDep, profile_id: ProfileDep, visitor: VisitorDep):
    g = _own_game(s, body.game_id, profile_id)
    try:
        st = coach_service.create_study(s, g, body.theme, visitor)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return coach_service.study_out(st, g)


@app.get("/api/studies/{study_id}")
def get_study(study_id: int, s: SessionDep, profile_id: ProfileDep):
    st = _own_study(s, study_id, profile_id)
    return coach_service.study_out(st, s.get(Game, st.game_id))


def _checkpoint(st: Study, index: int) -> None:
    if not 0 <= index < len(st.checkpoints):
        raise HTTPException(404, "Checkpoint not found.")


@app.put("/api/studies/{study_id}/checkpoints/{index}")
def save_study_notes(study_id: int, index: int, body: StudyNotes, s: SessionDep, profile_id: ProfileDep):
    st = _own_study(s, study_id, profile_id)
    _checkpoint(st, index)
    try:
        coach_service.save_notes(s, st, index, body.plans, body.move, body.reply, body.text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return coach_service.study_out(st, s.get(Game, st.game_id))


@app.post("/api/studies/{study_id}/checkpoints/{index}/reveal")
def reveal_checkpoint(study_id: int, index: int, s: SessionDep, profile_id: ProfileDep, visitor: VisitorDep):
    st = _own_study(s, study_id, profile_id)
    _checkpoint(st, index)
    coach_service.reveal(s, st, index, visitor)
    return coach_service.study_out(st, s.get(Game, st.game_id))


@app.delete("/api/studies/{study_id}", status_code=204)
def delete_study(study_id: int, s: SessionDep, profile_id: ProfileDep):
    st = _own_study(s, study_id, profile_id)
    s.delete(st)
    s.commit()


# ---------------------------------------------------------------- built frontend (single-container deploys)

if (config.FRONTEND_DIST / "index.html").exists():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found.")
        f = (config.FRONTEND_DIST / path).resolve()
        if path and f.is_file() and config.FRONTEND_DIST.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(config.FRONTEND_DIST / "index.html")
