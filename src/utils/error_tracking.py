"""Wires up error reporting to the self-hosted GlitchTip instance (Sentry-
protocol-compatible), shared by both the API process (app.py) and the
worker process (queue_runner.py) — a worker crash mid-render is just as
important to catch as an API 500, and previously the only way to find
either was grepping raw docker logs by hand.

No-ops entirely if SENTRY_DSN isn't configured, so this is safe to call
unconditionally at startup in any environment (local dev included)."""
from src.config import SENTRY_DSN
from src.utils.logger import logger


def init_error_tracking(role: str) -> None:
    if not SENTRY_DSN:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # Traces/profiling aren't the point here (this is error tracking,
        # not APM) — kept at 0 so this never adds per-request overhead or
        # GlitchTip storage cost beyond actual errors.
        traces_sample_rate=0.0,
        environment=role,  # "api" | "worker" — same DSN/project, tagged apart in GlitchTip
    )
    logger.info(f"Error tracking initialized (GlitchTip, role={role}).")
