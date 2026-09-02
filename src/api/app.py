from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
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
    title="KappGen API",
    summary="Create, render and publish branded YouTube videos.",
    description="""## KappGen developer API

The KappGen API powers channel configuration, media libraries, video
generation and optional YouTube publication. All resources belong to the
authenticated account. Use an API key from **KappGen → Paramètres → API**
for server-to-server integrations; never expose a key in browser code.

### Lifecycle
Create a channel, upload its visual assets, submit a script or audio file,
then poll the video resource until it reaches `done` or `failed`. YouTube
publication is always opt-in and remains subject to the channel owner's
settings and review controls.

The interactive examples below use the current `/api` endpoints. Files and
generated media are returned as short-lived URLs where applicable.
""",
    version="1.1.0",
    openapi_tags=[
        {"name": "auth", "description": "Account session and authentication."},
        {"name": "channels", "description": "YouTube channels and visual configuration."},
        {"name": "videos", "description": "Submit, monitor, edit and publish videos."},
        {"name": "folders", "description": "Organize generated videos."},
        {"name": "api-keys", "description": "Create and revoke integration keys."},
        {"name": "billing", "description": "Plans, credits and purchase history."},
    ],
    contact={"name": "KappGen support", "email": "contact@kappgen.com", "url": "https://kappgen.com"},
    license_info={"name": "KappGen API Terms", "url": "https://kappgen.com/terms"},
    servers=[{"url": "https://api.kappgen.com", "description": "Production"}],
    # Served below with the KappGen favicon instead of Swagger's default
    # green logo, so the API docs share the product identity.
    docs_url=None,
    redoc_url=None,
    swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True, "filter": True, "tryItOutEnabled": True},
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

KAPPGEN_FAVICON = "https://kappgen.com/assets/logo/favicon-32.png"

@app.get("/docs", include_in_schema=False)
def swagger_docs() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="KappGen API · Documentation",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        swagger_favicon_url=KAPPGEN_FAVICON,
        swagger_ui_parameters=app.swagger_ui_parameters,
    )

@app.get("/redoc", include_in_schema=False)
def redoc_docs() -> HTMLResponse:
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title="KappGen API · Référence",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js",
        redoc_favicon_url=KAPPGEN_FAVICON,
    )

# Mount static storage directory for output video streaming and downloads
app.mount("/storage", StaticFiles(directory=str(STORAGE_PATH)), name="storage")

# Register API routes
app.include_router(auth.router)
app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(folders.router)
app.include_router(api_keys.router)
app.include_router(billing.router)
# Admin operations remain available to the back-office, but are intentionally
# excluded from the public OpenAPI contract and interactive documentation.
app.include_router(admin.router, include_in_schema=False)

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/api/health", include_in_schema=False)
def health_check():
    return {"status": "ok", "app": "KappGen Video Pipeline MVP"}

@app.get("/api/db-status", include_in_schema=False)
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
