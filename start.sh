#!/bin/sh
# Runs the analysis worker beside the API, restarting it if it stops.
cd /app/backend
(
  sleep 4  # let the API create the database first
  while true; do
    python -m app.worker --threads "${WORKER_THREADS:-2}"
    echo "worker exited; restarting in 5s"
    sleep 5
  done
) &
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}"
