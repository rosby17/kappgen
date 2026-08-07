from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
import sys

# Add base directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import STORAGE_PATH, ASSETS_PATH
from src.db.session import init_db
from src.api.routes import channels, videos, auth

app = FastAPI(
    title="NicheCut SaaS API",
    description="Automated long-form video pipeline for YouTube niche channels.",
    version="1.0.0"
)

# CORS middleware for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

# Mount frontend static build directory if present
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="frontend_assets")

    @app.get("/")
    def serve_frontend_index():
        return FileResponse(FRONTEND_DIST / "index.html")

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/api/health")
def health_check():
    return {"status": "ok", "app": "NicheCut Video Pipeline MVP"}

@app.get("/api/db-status")
def get_db_status():
    from src.config import DATABASE_URL, SUPABASE_URL
    is_postgres = "postgresql" in DATABASE_URL or "postgres" in DATABASE_URL
    return {
        "status": "connected",
        "engine": "postgresql" if is_postgres else "sqlite",
        "service_name": "Supabase PostgreSQL (Self-Hosted VPS rooseveltvps)" if is_postgres else "Local SQLite",
        "supabase_url": SUPABASE_URL or "https://bd.izivoice.app",
        "database_host": "31.97.118.192:5432" if is_postgres else "local file data/app.db",
        "tables": ["users", "channels", "videos"]
    }
