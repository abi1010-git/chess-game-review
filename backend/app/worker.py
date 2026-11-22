"""Analysis worker: claims queued jobs, runs Stockfish, stores evidence and findings.

Run it beside the API:  python -m app.worker  [--threads 2] [--once]
Each thread owns one single-threaded engine, so results stay reproducible for a profile.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
from typing import Callable

import chess.engine

from . import config, db, jobs
from .analysis import Cancelled, analyze_game
from .engine import Engine, EngineUnavailable, StockfishEngine
from .models import Game, WorkerHeartbeat, utcnow
from .store import write_analysis

log = logging.getLogger("worker")


class EngineMismatch(RuntimeError):
    pass


def default_engine_factory(profile: dict) -> Engine:
    e = StockfishEngine(threads=profile["threads"], hash_mb=profile["hash_mb"])
    if e.name != profile["engine"] and not config.ENGINE_ALLOW_MISMATCH:
        e.close()
        raise EngineMismatch(
            f"Analysis {config.ANALYSIS_VERSION} is defined for {profile['engine']}, but the binary is {e.name}. "
            "Use the matching build, or set a new ANALYSIS_VERSION with its own profile "
            "(ENGINE_ALLOW_MISMATCH=1 overrides this).")
    return e


def _beat(worker_id: str, engine_name: str | None, job_id: int | None = None, error: str | None = None):
    with db.SessionLocal() as s:
        hb = s.get(WorkerHeartbeat, worker_id)
        if hb is None:
            hb = WorkerHeartbeat(worker_id=worker_id)
            s.add(hb)
        hb.engine_name, hb.analysis_version = engine_name, config.ANALYSIS_VERSION
        hb.current_job_id, hb.last_seen, hb.error = job_id, utcnow(), error
        s.commit()


class Stopping(Exception):
    pass


def process_one(worker_id: str, engine: Engine, profile: dict | None = None,
                lease_seconds: int | None = None, beat_every: float = 15.0,
                stop: threading.Event | None = None) -> str:
    """Claim and process one job. Returns 'idle', 'done', 'cancelled', 'released' or 'failed'."""
    profile = profile or config.ENGINE_PROFILE
    lease_seconds = lease_seconds or config.WORKER_LEASE_SECONDS
    with db.SessionLocal() as s:
        job = jobs.claim_next(s, worker_id, lease_seconds)
        if job is None:
            return "idle"
        job_id, version = job.id, job.analysis_version
        game = s.get(Game, job.game_id)
        _beat(worker_id, engine.name, job_id)
        last = time.monotonic()

        def progress(done: int, total: int):
            nonlocal last
            if stop is not None and stop.is_set():
                raise Stopping()
            if time.monotonic() - last >= beat_every:
                last = time.monotonic()
                with db.SessionLocal() as hs:
                    if not jobs.heartbeat(hs, job_id, worker_id, lease_seconds):
                        raise Cancelled()
                _beat(worker_id, engine.name, job_id)

        try:
            result = analyze_game(game.initial_fen, game.moves_uci.split(), game.player_color, engine, profile,
                                  progress)
        except Cancelled:
            log.info("job %s was cancelled or taken over; results discarded", job_id)
            return "cancelled"
        except Stopping:
            jobs.release(s, job_id, worker_id)
            return "released"
        except (chess.engine.EngineTerminatedError, chess.engine.EngineError):
            jobs.fail(s, job_id, worker_id, "The engine stopped unexpectedly.")
            raise
        except Exception as e:  # a bad game must not stop the worker
            log.exception("job %s failed", job_id)
            jobs.fail(s, job_id, worker_id, f"{type(e).__name__}: {e}")
            return "failed"

        ok = jobs.complete_with_results(s, job_id, worker_id, lambda ws: write_analysis(ws, game, version, result))
        if not ok:
            log.info("job %s was cancelled before its results were saved; discarded", job_id)
            return "cancelled"
        log.info("job %s done: game %s, %s plies, %s findings", job_id, game.id, len(result.plies),
                 len(result.findings))
        return "done"


def run_loop(worker_id: str, stop: threading.Event, engine_factory: Callable[[dict], Engine] = default_engine_factory,
             poll: float = 2.0, once: bool = False):
    profile = config.ENGINE_PROFILE
    engine = engine_factory(profile)
    try:
        while not stop.is_set():
            try:
                status = process_one(worker_id, engine, profile, stop=stop)
            except (chess.engine.EngineTerminatedError, chess.engine.EngineError):
                log.warning("engine stopped; restarting it")
                engine.close()
                engine = engine_factory(profile)
                continue
            if status == "idle":
                _beat(worker_id, engine.name)
                if once:
                    return
                stop.wait(poll)
    finally:
        engine.close()


def main():
    p = argparse.ArgumentParser(description="Chess Game Review analysis worker")
    p.add_argument("--threads", type=int, default=int(os.environ.get("WORKER_THREADS", 2)),
                   help="parallel single-threaded engines (default 2)")
    p.add_argument("--once", action="store_true", help="exit when the queue is empty")
    p.add_argument("--poll", type=float, default=2.0)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
    db.init_db()
    base = f"{socket.gethostname()}-{os.getpid()}"
    stop = threading.Event()
    try:
        default_engine_factory(config.ENGINE_PROFILE).close()  # fail fast with a clear message
    except (EngineUnavailable, EngineMismatch) as e:
        log.error("%s", e)
        _beat(f"{base}-0", None, error=str(e))
        raise SystemExit(2)
    threads = [threading.Thread(target=run_loop, name=f"w{i}", args=(f"{base}-{i}", stop),
                                kwargs={"poll": args.poll, "once": args.once}, daemon=True)
               for i in range(max(1, args.threads))]
    for t in threads:
        t.start()
    log.info("worker started: %s thread(s), analysis %s", len(threads), config.ANALYSIS_VERSION)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("stopping; unfinished jobs return to the queue when their lease expires")
        stop.set()
        for t in threads:
            t.join(timeout=10)


if __name__ == "__main__":
    main()
