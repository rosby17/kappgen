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
    # 4 worker processes instead of the previous single-process default:
    # route handlers make plenty of synchronous/blocking calls (outbound
    # HTTP to Anthropic/OpenAI/fal.ai/Izivoice, sync SQLAlchemy queries)
    # inside `async def` endpoints — on one process, any one of those in
    # flight froze the single event loop, stalling every OTHER unrelated
    # request (even a trivial one like submitting a new video) for as long
    # as that call took. Multiple processes mean a slow request in one no
    # longer blocks requests being served by the others. Note: the simple
    # in-memory rate limiter (src/utils/rate_limit.py) is now per-process,
    # not global — its limits are correspondingly looser, an acceptable
    # trade-off for fixing this.
    exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --workers 4
    ;;
  worker)
    exec python -m src.worker.queue_runner
    ;;
  all)
    # Background queue worker (polls the DB for queued videos and renders them).
    python -m src.worker.queue_runner &
    # Foreground API server (PID 1).
    # 4 worker processes instead of the previous single-process default:
    # route handlers make plenty of synchronous/blocking calls (outbound
    # HTTP to Anthropic/OpenAI/fal.ai/Izivoice, sync SQLAlchemy queries)
    # inside `async def` endpoints — on one process, any one of those in
    # flight froze the single event loop, stalling every OTHER unrelated
    # request (even a trivial one like submitting a new video) for as long
    # as that call took. Multiple processes mean a slow request in one no
    # longer blocks requests being served by the others. Note: the simple
    # in-memory rate limiter (src/utils/rate_limit.py) is now per-process,
    # not global — its limits are correspondingly looser, an acceptable
    # trade-off for fixing this.
    exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --workers 4
    ;;
  *)
    echo "Unknown ROLE '$ROLE' (expected api, worker, or all)" >&2
    exit 1
    ;;
esac
