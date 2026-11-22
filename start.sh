#!/bin/sh
# Runs the analysis worker beside the API, restarting it if it stops.
cd /app/backend
# Create the database once, before the API and the worker both try to.
python -c "from app import db; db.init_db()"
(
  while true; do
    python -m app.worker --threads "${WORKER_THREADS:-1}"
    echo "worker exited; restarting in 5s"
    sleep 5
  done
) &
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}"
