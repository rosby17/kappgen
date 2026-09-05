from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_redoc_html
from fastapi.responses import HTMLResponse
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from pathlib import Path
import sys

# Add base directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import STORAGE_PATH, CORS_ORIGINS
from src.db.session import init_db
from src.utils.error_tracking import init_error_tracking
from src.api.routes import channels, videos, auth, folders, api_keys, billing, admin, facecam, webhooks
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
    # Contact channel currently handled through the owner's WhatsApp.
    contact={"name": "Support KappGen · WhatsApp", "url": "https://wa.me/237655306425"},
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
    allow_origins=CORS_ORIGINS,
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
    # Scalar provides the modern, searchable reference experience used by
    # contemporary API products while reading the same OpenAPI contract.
    return HTMLResponse(f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KappGen API · Documentation</title><link rel="icon" href="{KAPPGEN_FAVICON}">
<style>html,body{{margin:0;background:#080d15;color:#e8f3ff}}#app{{min-height:100vh}}</style></head>
<body><div id="app"></div>
<script type="text/javascript">var configuration={{
  spec: {{url: '{app.openapi_url}'}},
  theme: 'purple',
  darkMode: true,
  hideModels: false,
  hideDownloadButton: false,
  hideClientButton: true,
  showSidebar: true,
  hideTestRequestButton: false,
  metaData: {{title: 'KappGen API', description: 'API de création, montage et publication vidéo'}},
  customCss: `:root {{ --scalar-color-1: #e8f3ff; --scalar-color-accent: #00c2ff; --scalar-background-1: #080d15; --scalar-background-2: #0e1724; --scalar-background-3: #141f2f; --scalar-border-color: #263750; }} .sidebar {{ border-right: 1px solid #263750; }} .t-doc__header {{ background: linear-gradient(135deg,#0d1b2b,#0a111d); }}`
}};</script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
<script>Scalar.createApiReference('#app', configuration)</script>
</body></html>''')

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
app.include_router(facecam.router)
app.include_router(folders.router, include_in_schema=False)
app.include_router(api_keys.router, include_in_schema=False)
app.include_router(billing.router)
app.include_router(webhooks.router, include_in_schema=False)
# Admin operations remain available to the back-office, but are intentionally
# excluded from the public OpenAPI contract and interactive documentation.
app.include_router(admin.router, include_in_schema=False)

# Public API contract: the application has many internal endpoints used by the
# web console (voice catalogues, previews, editing helpers, diagnostics, etc.).
# They remain available to the first-party app but are deliberately omitted
# from the public documentation so the API exposes only stable integration
# primitives, not our internal architecture.
_PUBLIC_API_OPERATIONS = {
    ("GET", "/api/channels"), ("POST", "/api/channels"),
    ("GET", "/api/channels/{channel_id}"), ("PUT", "/api/channels/{channel_id}"),
    ("POST", "/api/channels/{channel_id}/generate-now"),
    ("GET", "/api/channels/{channel_id}/library-preview"),
    ("POST", "/api/channels/{channel_id}/library-images"),
    ("GET", "/api/channels/library/overview"),
    ("GET", "/api/channels/{channel_id}/library/images"),
    ("POST", "/api/channels/{channel_id}/broll"),
    ("GET", "/api/channels/{channel_id}/broll"),
    ("GET", "/api/channels/{channel_id}/youtube/auth-url"),
    ("POST", "/api/channels/{channel_id}/youtube/disconnect"),
    ("POST", "/api/videos"), ("GET", "/api/videos"),
    ("GET", "/api/videos/{video_id}"), ("GET", "/api/videos/{video_id}/download"),
    ("POST", "/api/videos/{video_id}/retry"),
    ("GET", "/api/billing/plans"), ("GET", "/api/billing/subscription"),
    ("GET", "/api/billing/credits"),
    ("GET", "/api/billing/api-credits"), ("GET", "/api/billing/api-credits/transactions"),
}

def _public_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, summary=app.summary,
                         description=app.description, routes=app.routes,
                         tags=app.openapi_tags, contact=app.contact,
                         license_info=app.license_info, servers=app.servers)
    schema["paths"] = {
        path: {method: operation for method, operation in methods.items()
               if (method.upper(), path) in _PUBLIC_API_OPERATIONS}
        for path, methods in schema["paths"].items()
        if any((method.upper(), path) in _PUBLIC_API_OPERATIONS for method in methods)
    }
    app.openapi_schema = schema
    return schema

app.openapi = _public_openapi

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
