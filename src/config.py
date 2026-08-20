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
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
# Separate from the login flow above (which only verifies an id_token client-side).
# YouTube publishing needs a full server-side OAuth2 authorization-code exchange
# (to get a refresh_token with the youtube.upload scope), which requires a client
# secret and a registered redirect URI on the same or a dedicated OAuth client.
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
YOUTUBE_OAUTH_REDIRECT_URI = os.getenv("YOUTUBE_OAUTH_REDIRECT_URI", "")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://nichecut.tools-cl.com")

# Vision analysis (reference image -> style prompt) for AI image generation.
# Provider-swappable: set VISION_PROVIDER to "anthropic" (default, Claude) or
# "openai" once an OpenAI key is available — see src/pipeline/vision.py.
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "anthropic")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# NOTE: NicheCut's database must always be its own, dedicated instance — never
# shared with another project (see incident: an earlier setup pointed this at
# Izivoice's production Supabase Postgres and polluted its public schema).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///./data/app.db"

# Kept for backward compatibility with code that still imports these; NicheCut
# does not use Supabase's Auth/Storage/Realtime layers, only a plain Postgres DB.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

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
