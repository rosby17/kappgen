from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from pathlib import Path
import sys

# Add base directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import STORAGE_PATH
from src.db.session import init_db
from src.utils.error_tracking import init_error_tracking
from src.api.routes import channels, videos, auth, folders, api_keys, billing, admin
from src.utils.auth import get_current_admin

init_error_tracking("api")

app = FastAPI(
    title="KappGen SaaS API",
    description="Automated long-form video pipeline for YouTube niche channels.",
    version="1.0.0"
)

# CORS: only KappGen's own origins (+ localhost for dev) may send credentialed
# requests. The previous `allow_origin_regex=r"https?://.*"` accepted every
# origin on the internet, which combined with allow_credentials=True defeated
# CORS entirely — confirmed live (an arbitrary Origin header got echoed back
# with access-control-allow-credentials: true).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?kappgen\.com|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Plain ASGI middleware, not @app.middleware("http") (Starlette's
# BaseHTTPMiddleware) — that variant buffers/rewraps the whole response body,
# which silently strips Range/206 support from the /storage StaticFiles mount
# and forced every video preview to download sequentially instead of seeking.
# Confirmed live: a Range request against output.mp4 came back 200 with no
# Accept-Ranges header. This version only touches the response-start message,
# so body streaming (and Range handling) passes through untouched.
class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeadersMiddleware)

# Mount static storage directory for output video streaming and downloads
app.mount("/storage", StaticFiles(directory=str(STORAGE_PATH)), name="storage")

# Register API routes
app.include_router(auth.router)
app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(folders.router)
app.include_router(api_keys.router)
app.include_router(billing.router)
app.include_router(admin.router)

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/api/health")
def health_check():
    return {"status": "ok", "app": "KappGen Video Pipeline MVP"}

@app.get("/api/db-status")
def get_db_status(admin=Depends(get_current_admin)):
    # Was publicly reachable — leaked the DB engine, host and table names to
    # anyone, unauthenticated. Confirmed live before this fix.
    from src.config import DATABASE_URL
    from urllib.parse import urlparse

    is_postgres = "postgresql" in DATABASE_URL or "postgres" in DATABASE_URL
    if is_postgres:
        parsed = urlparse(DATABASE_URL)
        db_name = parsed.path.lstrip('/') or "nichecut"
        host = f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "VPS")
        service = f"PostgreSQL Dedicated ({db_name})"
    else:
        host = "data/app.db"
        service = "Local SQLite"

    return {
        "status": "connected",
        "engine": "postgresql" if is_postgres else "sqlite",
        "service_name": service,
        "database_host": host,
        "tables": ["users", "channels", "videos"]
    }
