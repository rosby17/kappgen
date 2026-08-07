#!/bin/sh
set -e

# Background queue worker (polls the DB for queued videos and renders them).
python -m src.worker.queue_runner &

# Foreground API server (PID 1).
exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000
