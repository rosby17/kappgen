#!/bin/sh
set -e

# ROLE selects what this container runs, so the same image can be deployed
# as two independent Coolify apps (API and worker) sharing the same DB and
# storage volume — redeploying the API (frontend/API-only changes, the vast
# majority) never restarts the worker, so in-progress renders are never
# interrupted by a routine deploy. Defaults to "all" (old combined
# behavior) so this stays a no-op for any deployment not yet split.
ROLE="${ROLE:-all}"
# Was hardcoded to 4 (see the reasoning below) — right after this container's
# own hard CPU cap went in (izivoice/kappgen split, kappgen-backend at 1.5
# CPU), a cold start of 4 uvicorn workers plus a normal burst of dashboard
# traffic pinned it at 131% of that cap for long enough to trip the CPU
# auto-stop watchdog. >1 is what actually fixes the stalling problem below;
# default trimmed to 2 to fit the real budget, still overridable.
API_UVICORN_WORKERS="${API_UVICORN_WORKERS:-2}"

case "$ROLE" in
  api)
    # Multiple worker processes instead of a single one: route handlers make
    # plenty of synchronous/blocking calls (outbound HTTP to
    # Anthropic/OpenAI/fal.ai/Izivoice, sync SQLAlchemy queries) inside
    # `async def` endpoints — on one process, any one of those in flight
    # froze the single event loop, stalling every OTHER unrelated request
    # (even a trivial one like submitting a new video) for as long as that
    # call took. Multiple processes mean a slow request in one no longer
    # blocks requests being served by the others. Note: the simple
    # in-memory rate limiter (src/utils/rate_limit.py) is now per-process,
    # not global — its limits are correspondingly looser, an acceptable
    # trade-off for fixing this.
    exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --workers "$API_UVICORN_WORKERS"
    ;;
  worker)
    exec python -m src.worker.queue_runner
    ;;
  all)
    # Background queue worker (polls the DB for queued videos and renders them).
    python -m src.worker.queue_runner &
    # Foreground API server (PID 1). See the `api` case above for why this
    # is multi-process and why the count is capped by API_UVICORN_WORKERS.
    exec uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --workers "$API_UVICORN_WORKERS"
    ;;
  *)
    echo "Unknown ROLE '$ROLE' (expected api, worker, or all)" >&2
    exit 1
    ;;
esac
