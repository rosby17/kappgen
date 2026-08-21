#!/bin/sh
set -e

# ROLE selects what this container runs, so the same image can be deployed
# as two independent Coolify apps (API and worker) sharing the same DB and
# storage volume — redeploying the API (frontend/API-only changes, the vast
# majority) never restarts the worker, so in-progress renders are never
# interrupted by a routine deploy. Defaults to "all" (old combined
# behavior) so this stays a no-op for any deployment not yet split.
ROLE="${ROLE:-all}"

case "$ROLE" in
  api)
    exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec python -m src.worker.queue_runner
    ;;
  all)
    # Background queue worker (polls the DB for queued videos and renders them).
    python -m src.worker.queue_runner &
    # Foreground API server (PID 1).
    exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000
    ;;
  *)
    echo "Unknown ROLE '$ROLE' (expected api, worker, or all)" >&2
    exit 1
    ;;
esac
