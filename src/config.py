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
# How many videos the worker renders at once (each with its own TTS/STT calls),
# instead of the old strictly-sequential one-at-a-time queue. Kept modest by
# default (3) because the actual video assembly (ffmpeg) is CPU-bound and this
# runs on a shared 4-vCPU VPS alongside Supabase/Coolify/other apps — going
# much higher would just make every concurrent render slower via CPU
# contention instead of finishing faster. Raise via env var on beefier hardware.
MAX_CONCURRENT_RENDERS = int(os.getenv("MAX_CONCURRENT_RENDERS", "3"))
# Separate, tighter cap on simultaneous Izivoice TTS/STT calls specifically —
# Izivoice rate-limits aggressively, so this stays below MAX_CONCURRENT_RENDERS
# even when more videos are rendering in parallel (the rest of each pipeline —
# script, images, ffmpeg — isn't throttled by Izivoice at all).
MAX_CONCURRENT_IZIVOICE_CALLS = int(os.getenv("MAX_CONCURRENT_IZIVOICE_CALLS", "3"))
# Used to encrypt connected customers' Izivoice API keys at rest. In production
# set a long, stable random value; changing it invalidates stored connections.
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
# Signs session tokens (src/utils/auth.py). Every route that trusts "who the
# caller is" depends on this being a real secret in production — falling back
# to a fixed dev value only so local/test runs don't crash without a .env.
SECRET_KEY = os.getenv("SECRET_KEY", "") or "dev-insecure-secret-change-me"

# Maketou + Tara Money (dklo.co) — same merchant credentials already used by
# the sibling izivoice project (reused deliberately, per the user).
MAKETOU_API_KEY = os.getenv("MAKETOU_API_KEY", "")
MAKETOU_PRODUCT_ID = os.getenv("MAKETOU_PRODUCT_ID", "")
TARA_API_KEY = os.getenv("TARA_API_KEY", "")
TARA_BUSINESS_ID = os.getenv("TARA_BUSINESS_ID", "")
# Not reused from izivoice — this only has to match what NicheCut itself
# sends as the webHookUrl query param at checkout time.
TARA_WEBHOOK_SECRET = os.getenv("TARA_WEBHOOK_SECRET", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
# Separate from the login flow above (which only verifies an id_token client-side).
# YouTube publishing needs a full server-side OAuth2 authorization-code exchange
# (to get a refresh_token with the youtube.upload scope), which requires a client
# secret and a registered redirect URI on the same or a dedicated OAuth client.
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
YOUTUBE_OAUTH_REDIRECT_URI = os.getenv("YOUTUBE_OAUTH_REDIRECT_URI", "")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://kappgen.com")
# This backend's own public URL — needed to build webhook callback URLs (Tara
# Money) that point back at the server, not the frontend.
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "https://api.kappgen.com")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "NicheCut")

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
