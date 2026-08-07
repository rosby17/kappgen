import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# API Keys
IZIVOICE_API_KEY = os.getenv("IZIVOICE_API_KEY", "")
IZIVOICE_BASE_URL = os.getenv("IZIVOICE_BASE_URL", "https://api.izivoice.app/api")
IZIVOICE_VOICE_ID = os.getenv("IZIVOICE_VOICE_ID", "")  # optional: auto-picked from GET /voices if empty
AI_IMAGE_PROVIDER_API_KEY = os.getenv("AI_IMAGE_PROVIDER_API_KEY", "")
AI_IMAGE_PROVIDER_ENDPOINT = os.getenv("AI_IMAGE_PROVIDER_ENDPOINT", "https://api.ai33.pro")
AI_IMAGE_MODEL_ID = os.getenv("AI_IMAGE_MODEL_ID", "gemini-2.5-flash-image")

# Supabase Credentials
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
_supa_db = os.getenv("SUPABASE_DATABASE_URL", "").strip()
_main_db = os.getenv("DATABASE_URL", "").strip()

SUPABASE_DATABASE_URL = _supa_db if _supa_db else (_main_db if _main_db else "sqlite:///./data/app.db")
DATABASE_URL = SUPABASE_DATABASE_URL

# Storage & DB Config
STORAGE_PATH = Path(os.getenv("STORAGE_PATH", BASE_DIR / "storage")).resolve()
ASSETS_PATH = Path(os.getenv("ASSETS_PATH", BASE_DIR / "assets")).resolve()
DATA_PATH = BASE_DIR / "data"

# Create required directories if they don't exist
STORAGE_PATH.mkdir(parents=True, exist_ok=True)
ASSETS_PATH.mkdir(parents=True, exist_ok=True)
(ASSETS_PATH / "fonts").mkdir(parents=True, exist_ok=True)
(ASSETS_PATH / "overlays").mkdir(parents=True, exist_ok=True)
(ASSETS_PATH / "music").mkdir(parents=True, exist_ok=True)
(ASSETS_PATH / "images").mkdir(parents=True, exist_ok=True)
DATA_PATH.mkdir(parents=True, exist_ok=True)
