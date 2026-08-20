from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import sys

# Add base directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import STORAGE_PATH
from src.db.session import init_db
from src.api.routes import channels, videos, auth, folders, api_keys, billing, admin

app = FastAPI(
    title="NicheCut SaaS API",
    description="Automated long-form video pipeline for YouTube niche channels.",
    version="1.0.0"
)

# CORS middleware for frontend development & production
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
app.include_router(admin.router)

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/api/health")
def health_check():
    return {"status": "ok", "app": "NicheCut Video Pipeline MVP"}

@app.get("/api/db-status")
def get_db_status():
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
