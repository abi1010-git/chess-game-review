"""FastAPI app: imports, game history, selection, the analysis queue, reports and the dashboard."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import compare, config, db, jobs, selection
from .dashboard import dashboard as build_dashboard
from .importers.base import ProviderError, RetryPolicy, make_client, validate_username
from .importers.chesscom import ChessComAdapter
from .importers.lichess import LichessAdapter
from .importers.service import execute_import, remember_identity
from .coach import service as coach_service
from .models import AnalysisBatch, AnalysisJob, Game, ImportRun, Study, WorkerHeartbeat, utcnow
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
    yield


app = FastAPI(title="Chess Game Review API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

# Overridable in tests (fake HTTP transport, no real sleeping).
app.state.http_client_factory = make_client
app.state.retry_factory = RetryPolicy

SessionDep = Annotated[Session, Depends(db.get_session)]
ProfileDep = Annotated[int, Depends(db.get_profile_id)]
ADAPTERS = {"lichess": LichessAdapter, "chesscom": ChessComAdapter}


def _dt(d: date | None, end: bool = False) -> datetime | None:
    if d is None:
        return None
    return datetime.combine(d, time.max if end else time.min)


@app.get("/api/health")
def health(s: SessionDep):
    s.execute(select(1))
    return {"status": "ok", "analysis_version": config.ANALYSIS_VERSION}


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
) -> selection.GameFilter:
    return selection.GameFilter(date_from, date_to, time_class, provider, color, rating_min, rating_max,
                                rated, result, analysis)


FilterDep = Annotated[selection.GameFilter, Depends(game_filter)]


def _with_status(s: Session, games: list[Game]) -> list[dict]:
    ids = [g.id for g in games]
    statuses = dict(s.execute(select(AnalysisJob.game_id, AnalysisJob.status).where(
        AnalysisJob.game_id.in_(ids), AnalysisJob.analysis_version == config.ANALYSIS_VERSION)).all()) if ids else {}
    out = []
    for g in games:
        d = GameOut.model_validate(g).model_dump()
        d["analysis_status"] = statuses.get(g.id, "none")
        out.append(d)
    return out


@app.get("/api/games", response_model=GamePage)
def list_games(s: SessionDep, profile_id: ProfileDep, f: FilterDep,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = s.scalar(selection.apply_filter(select(func.count(Game.id)), f, profile_id))
    stmt = selection.apply_filter(select(Game), f, profile_id)
    stmt = stmt.order_by(Game.played_at.desc().nulls_last(), Game.id.desc()).limit(limit).offset(offset)
    return {"total": total, "items": _with_status(s, list(s.scalars(stmt)))}


@app.get("/api/games/ids")
def list_game_ids(s: SessionDep, profile_id: ProfileDep, f: FilterDep):
    """All ids matching the filter, for 'select the whole filtered set'."""
    ids = list(s.scalars(selection.apply_filter(select(Game.id), f, profile_id)
                         .limit(config.MAX_GAMES_PER_BATCH + 1)))
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
    d["analysis_status"] = _with_status(s, [g])[0]["analysis_status"]
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


@app.post("/api/analysis/batches", response_model=BatchOut, status_code=201)
def create_batch(body: BatchCreate, s: SessionDep, profile_id: ProfileDep):
    if not jobs.preview(s, profile_id, body.game_ids)["found"]:
        raise HTTPException(404, "None of these games were found.")
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
    game["analysis_status"] = _with_status(s, [g])[0]["analysis_status"]
    report = compare.game_report(s, g)
    return {"game": game, "report": report, **worker_status(s)}


# ---------------------------------------------------------------- phase 3: coach reviews and studies

def visitor_id(request: Request) -> str:
    """Who is asking, for the per-visitor model limit (first proxy hop on hosted deploys)."""
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "local")


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
    return coach_service.coach_status(visitor)


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
