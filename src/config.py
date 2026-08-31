import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Self-hosted GlitchTip (Sentry-protocol-compatible) error tracking — empty
# means error reporting is simply off (see src/utils/error_tracking.py),
# never a hard requirement to run the app locally/in dev.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")

# API Keys
# Cloudflare R2 (S3-compatible) — hybrid rendered-video storage: as long as
# R2 usage tracked in our own DB stays under R2_FREE_TIER_CAP_BYTES, finished
# renders upload there instead of staying on the VPS's own (small, shared)
# disk. Once usage would cross the cap, new renders fall back to local disk
# automatically — no code change needed to stay on R2's free tier today and
# raise the cap (or remove it) later after upgrading to a paid R2 plan.
# All four must be set for R2 to be used at all; leaving any unset keeps
# every video on local disk exactly like before this feature existed.
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
# Public bucket URL (r2.dev subdomain, or a custom domain mapped to the
# bucket) — videos are served directly from here, not proxied through our
# own API. Required alongside the 4 vars above for R2 to actually be used.
R2_PUBLIC_URL_BASE = os.getenv("R2_PUBLIC_URL_BASE", "").rstrip("/")
# Cloudflare R2's free tier is 10GB storage — kept at 9.5GB to leave margin
# for in-flight uploads counted before the DB commit lands. Override via env
# once on a paid plan (or set very high to effectively remove the cap).
R2_FREE_TIER_CAP_BYTES = int(os.getenv("R2_FREE_TIER_CAP_BYTES", str(9_500_000_000)))

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
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://app.kappgen.com")
# This backend's own public URL — needed to build webhook callback URLs (Tara
# Money) that point back at the server, not the frontend.
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "https://api.kappgen.com")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "NicheCut")

# Claude-powered pipeline steps (vision analysis, music prompts, ...) try
# providers in order: Anthropic direct -> fal.ai (Claude via OpenRouter,
# billed against fal.ai credits) -> OpenAI. Whichever call in the chain
# succeeds first is used; each key is optional, but at least one must be set.
# See src/pipeline/vision.py for the fallback implementation.
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "anthropic")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FAL_API_KEY = os.getenv("FAL_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Hugging Face Inference Providers (routed to nscale's FLUX.1-schnell) — free
# tier, tried FIRST for image generation before any paid provider (fal.ai,
# Izivoice) since it costs nothing up to each account's small monthly free
# credit. No image-conditioning support, so callers with reference images
# skip straight to the paid providers that do support it.
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY", "")
# Optional: several free-tier accounts' tokens, comma-separated — rotated
# through on quota-exhaustion (429) before falling back to a paid provider,
# so the combined free allowance is the sum of every account's own quota
# instead of just one. Falls back to the single HUGGINGFACE_API_KEY above if unset.
HUGGINGFACE_API_KEYS = [
    k.strip() for k in os.getenv("HUGGINGFACE_API_KEYS", "").split(",") if k.strip()
] or ([HUGGINGFACE_API_KEY] if HUGGINGFACE_API_KEY else [])
# Extra text-generation-only fallback (see src/pipeline/ai_text.py) — not
# used by vision.py, which stays on the three providers above.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# NOTE: NicheCut's database must always be its own, dedicated instance — never
# shared with another project (see incident: an earlier setup pointed this at
# Izivoice's production Supabase Postgres and polluted its public schema).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///./data/app.db"

# Kept for backward compatibility with code that still imports these; NicheCut
# does not use Supabase's Auth/Storage/Realtime layers, only a plain Postgres DB.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Every raster image extension a creator's uploaded library / overlay is
# accepted under — one shared set instead of four separately hand-maintained
# ones (channels.py, videos.py, admin.py, image_pool.py/images.py) that had
# quietly drifted out of sync with each other. .jfif specifically (JPEG File
# Interchange Format — real JPEG bytes, just a different extension, common
# from images saved off the web/Bing/WhatsApp) was missing from all of them,
# rejecting an otherwise perfectly valid image outright before it ever
# reached PIL's own format sniffing. PIL/Pillow can already decode every
# format listed here — this only ever widens the extension allowlist, never
# the actual format support (bad bytes still get rejected the same way).
IMAGE_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".jpe", ".png", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff"}

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
