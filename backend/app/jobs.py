"""Analysis job queue: idempotent enqueue plus lease-based claiming.

A job is unique per (game, analysis version) via job_key, so repeated requests and
worker restarts never create duplicate work. A worker claims a job with a time-limited
lease; if it dies, the lease expires and another worker can take the job over.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .models import AnalysisBatch, AnalysisJob, BatchGame, Game, utcnow

ACTIVE = ("queued", "running")


def job_key(game_id: int, version: str) -> str:
    return f"{game_id}:{version}"


def preview(s: Session, profile_id: int, game_ids: list[int], version: str | None = None) -> dict:
    version = version or config.ANALYSIS_VERSION
    ids = set(_owned_ids(s, profile_id, game_ids))
    statuses = dict(s.execute(
        select(AnalysisJob.game_id, AnalysisJob.status).where(
            AnalysisJob.game_id.in_(ids), AnalysisJob.analysis_version == version)).all()) if ids else {}
    done = [g for g in ids if statuses.get(g) == "done"]
    active = [g for g in ids if statuses.get(g) in ACTIVE]
    to_queue = sorted(ids - set(done) - set(active))
    return {
        "requested": len(game_ids),
        "found": len(ids),
        "already_analyzed": len(done),
        "already_queued": len(active),
        "will_queue": len(to_queue),
        "analysis_version": version,
    }


def _owned_ids(s: Session, profile_id: int, game_ids: list[int]) -> list[int]:
    if not game_ids:
        return []
    return list(s.scalars(select(Game.id).where(Game.profile_id == profile_id, Game.id.in_(set(game_ids)))))


def enqueue(s: Session, profile_id: int, game_ids: list[int], mode: str, rationale: dict | None = None,
            roles: dict[int, str] | None = None, version: str | None = None) -> tuple[AnalysisBatch, dict]:
    version = version or config.ANALYSIS_VERSION
    ids = _owned_ids(s, profile_id, game_ids)
    batch = AnalysisBatch(profile_id=profile_id, mode=mode, analysis_version=version, rationale=rationale or {})
    s.add(batch)
    s.flush()
    summary = Counter()
    for gid in ids:
        s.add(BatchGame(batch_id=batch.id, game_id=gid, role=(roles or {}).get(gid, "selected")))
        key = job_key(gid, version)
        job = s.scalar(select(AnalysisJob).where(AnalysisJob.job_key == key))
        if job is None:
            s.add(AnalysisJob(profile_id=profile_id, game_id=gid, analysis_version=version, job_key=key))
            try:
                s.flush()
                summary["queued"] += 1
            except IntegrityError:  # a concurrent request created it first
                s.rollback()
                raise
        elif job.status in ("failed", "cancelled"):
            job.status, job.attempts, job.last_error = "queued", 0, None
            job.lease_owner = job.lease_expires_at = None
            summary["requeued"] += 1
        elif job.status == "done":
            summary["already_analyzed"] += 1
        else:
            summary["already_queued"] += 1
    s.commit()
    return batch, {k: summary.get(k, 0) for k in ("queued", "requeued", "already_queued", "already_analyzed")}


def _claimable(now):
    return or_(
        AnalysisJob.status == "queued",
        and_(AnalysisJob.status == "running", AnalysisJob.lease_expires_at < now),
    )


def expire_exhausted(s: Session) -> int:
    """Running jobs whose lease expired after the last allowed attempt become failed."""
    now = utcnow()
    res = s.execute(
        update(AnalysisJob)
        .where(AnalysisJob.status == "running", AnalysisJob.lease_expires_at < now,
               AnalysisJob.attempts >= AnalysisJob.max_attempts)
        .values(status="failed", lease_owner=None, lease_expires_at=None, finished_at=now,
                last_error="The worker stopped responding too many times."))
    s.commit()
    return res.rowcount or 0


def claim_next(s: Session, worker_id: str, lease_seconds: int = 300) -> AnalysisJob | None:
    """Atomically claim the oldest available job. Returns None when nothing is available."""
    expire_exhausted(s)
    for _ in range(5):
        now = utcnow()
        candidate = s.scalar(
            select(AnalysisJob.id).where(_claimable(now), AnalysisJob.attempts < AnalysisJob.max_attempts)
            .order_by(AnalysisJob.created_at, AnalysisJob.id).limit(1))
        if candidate is None:
            return None
        res = s.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == candidate, _claimable(now), AnalysisJob.attempts < AnalysisJob.max_attempts)
            .values(status="running", lease_owner=worker_id, lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempts=AnalysisJob.attempts + 1, started_at=now, updated_at=now))
        s.commit()
        if res.rowcount == 1:
            return s.get(AnalysisJob, candidate, populate_existing=True)
    return None


def _owned_running(job_id: int, worker_id: str):
    return and_(AnalysisJob.id == job_id, AnalysisJob.status == "running", AnalysisJob.lease_owner == worker_id)


def heartbeat(s: Session, job_id: int, worker_id: str, lease_seconds: int = 300) -> bool:
    now = utcnow()
    res = s.execute(update(AnalysisJob).where(_owned_running(job_id, worker_id))
                    .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now))
    s.commit()
    return res.rowcount == 1


def complete(s: Session, job_id: int, worker_id: str) -> bool:
    """Mark done only if this worker still holds the job (not cancelled or taken over)."""
    now = utcnow()
    res = s.execute(update(AnalysisJob).where(_owned_running(job_id, worker_id))
                    .values(status="done", lease_owner=None, lease_expires_at=None, finished_at=now, updated_at=now))
    s.commit()
    return res.rowcount == 1


def fail(s: Session, job_id: int, worker_id: str, error: str) -> bool:
    job = s.get(AnalysisJob, job_id, populate_existing=True)
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        return False
    job.last_error = error[:2000]
    job.lease_owner = job.lease_expires_at = None
    if job.attempts >= job.max_attempts:
        job.status, job.finished_at = "failed", utcnow()
    else:
        job.status = "queued"
    s.commit()
    return True


def cancel_batch(s: Session, profile_id: int, batch_id: int) -> int:
    """Cancel this batch's jobs that have not finished. Running jobs lose their lease."""
    game_ids = select(BatchGame.game_id).where(BatchGame.batch_id == batch_id)
    batch = s.get(AnalysisBatch, batch_id)
    if batch is None or batch.profile_id != profile_id:
        return 0
    res = s.execute(
        update(AnalysisJob)
        .where(AnalysisJob.game_id.in_(game_ids), AnalysisJob.analysis_version == batch.analysis_version,
               AnalysisJob.status.in_(ACTIVE))
        .values(status="cancelled", lease_owner=None, lease_expires_at=None, finished_at=utcnow()))
    s.commit()
    return res.rowcount or 0


def batch_progress(s: Session, batch: AnalysisBatch) -> dict:
    rows = s.execute(
        select(BatchGame.game_id, BatchGame.role, AnalysisJob.status, AnalysisJob.last_error)
        .outerjoin(AnalysisJob, and_(AnalysisJob.game_id == BatchGame.game_id,
                                     AnalysisJob.analysis_version == batch.analysis_version))
        .where(BatchGame.batch_id == batch.id)).all()
    counts = Counter(r.status or "missing" for r in rows)
    return {
        "total": len(rows),
        "counts": dict(counts),
        "games": [{"game_id": r.game_id, "role": r.role, "status": r.status or "missing", "error": r.last_error}
                  for r in rows],
    }


def complete_with_results(s: Session, job_id: int, worker_id: str, write) -> bool:
    """Mark the job done and write its results in one transaction.

    The status update runs first and only succeeds while this worker still holds the lease, so a
    cancelled or taken-over job never gets results and never appears complete.
    """
    now = utcnow()
    res = s.execute(update(AnalysisJob).where(_owned_running(job_id, worker_id))
                    .values(status="done", lease_owner=None, lease_expires_at=None, finished_at=now,
                            updated_at=now, last_error=None))
    if res.rowcount != 1:
        s.rollback()
        return False
    try:
        write(s)
        s.commit()
    except Exception:
        s.rollback()
        raise
    return True


def release(s: Session, job_id: int, worker_id: str) -> bool:
    """Hand a running job back to the queue on a clean shutdown, without using up an attempt."""
    res = s.execute(update(AnalysisJob).where(_owned_running(job_id, worker_id))
                    .values(status="queued", lease_owner=None, lease_expires_at=None,
                            attempts=AnalysisJob.attempts - 1, updated_at=utcnow()))
    s.commit()
    return res.rowcount == 1
