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

# Backblaze B2 (S3-compatible) — replaces R2 as of Sept 2026, primary
# rendered-video + B-roll storage (not a capped fallback like R2 was):
# ~1/5 the storage cost of R2, free egress up to 3x the stored volume/day.
# All five must be set for B2 to be used at all.
B2_ENDPOINT = os.getenv("B2_ENDPOINT", "")  # e.g. s3.us-west-002.backblazeb2.com
B2_REGION = os.getenv("B2_REGION", "us-west-002")
B2_KEY_ID = os.getenv("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "")
# Public bucket URL (path-style S3 endpoint works directly for a public
# bucket, no custom domain needed) — videos are served directly from here.
B2_PUBLIC_URL_BASE = os.getenv("B2_PUBLIC_URL_BASE", "").rstrip("/")
# 0/unset = no cap (B2 is cheap enough to just always use it once configured,
# unlike R2's free-tier-first fallback behavior).
B2_FREE_TIER_CAP_BYTES = int(os.getenv("B2_FREE_TIER_CAP_BYTES", "0"))

IZIVOICE_API_KEY = os.getenv("IZIVOICE_API_KEY", "")
IZIVOICE_BASE_URL = os.getenv("IZIVOICE_BASE_URL", "https://api.izivoice.app/api")
IZIVOICE_VOICE_ID = os.getenv("IZIVOICE_VOICE_ID", "")  # optional: auto-picked from GET /voices if empty
# Direct connection to ai33.pro — the actual upstream provider Izivoice itself
# resells — so KappGen's own automated volume stops consuming Izivoice's
# separate business account. Different protocol from Izivoice's own wrapper:
# `xi-api-key` header (not `Authorization: Bearer`), FormData bodies, and
# singular `/v1/task/{id}` (not Izivoice's own `/tasks/{id}`) — see
# src/pipeline/ai33_provider.py. Admin picks which provider is actually used
# per src/utils/app_settings.py's voiceover_provider_order(); this being set
# only makes ai33pro selectable, it does not switch anything by itself.
AI33PRO_API_KEY = os.getenv("AI33PRO_API_KEY", "")
AI33PRO_BASE_URL = os.getenv("AI33PRO_BASE_URL", "https://api.ai33.pro")
# Product policy: video renders are sequential FIFO. Only an explicit admin
# override may move a video ahead of older work; a creator's plan never changes
# the order. Kept as a compatibility constant for older deployment configuration,
# but the worker clamps the video lane count to one even if a stale environment
# variable is still set higher.
MAX_CONCURRENT_RENDERS = 1
# Separate, tighter cap on simultaneous Izivoice TTS/STT calls specifically —
# Izivoice rate-limits aggressively, so this stays below MAX_CONCURRENT_RENDERS
# even when more videos are rendering in parallel (the rest of each pipeline —
# script, images, ffmpeg — isn't throttled by Izivoice at all).
MAX_CONCURRENT_IZIVOICE_CALLS = int(os.getenv("MAX_CONCURRENT_IZIVOICE_CALLS", "3"))
# Per-video clip-rendering concurrency (see orchestrator.py Step 5/7 and the
# per-scene audio trim right after it): building each scene's ffmpeg clip
# sequentially left CPU cores idle, so this was parallelized and bounded by
# os.cpu_count() -- but that reads the HOST's full core count (unaffected by
# a cgroup CPU quota), not what this container is actually capped to. On a
# worker hard-limited to 2.5 CPU (izivoice/kappgen split), that meant up to
# 4 concurrent ffmpeg clip processes fighting over 2.5 cores' worth of real
# throughput -- pinning the container at/above its own ceiling for the
# whole clip-rendering phase of every video, independent of
# MAX_CONCURRENT_RENDERS (this happens *inside* a single render). Default
# of 2 leaves headroom for the main process + concurrent TTS/HTTP calls;
# raise via env var if the container's CPU allocation grows.
MAX_CLIP_RENDER_WORKERS = int(os.getenv("MAX_CLIP_RENDER_WORKERS", "2"))
# Daily automation (run_daily_automation) used to sweep every eligible channel
# and queue+kick off each one's video back-to-back with no pause between them,
# so a sweep touching several channels could hand multiple heavy renders to
# the queue within the same minute — fine when this ran on a whole shared
# VPS, but the worker container now sits behind its own hard CPU cap
# (see the izivoice/kappgen 50/50 split), so bursts like that pin it at its
# ceiling for several minutes straight. Spacing launches out lets each
# channel's script generation + render handoff land a bit apart instead of
# piling up. Set to 0 to go back to the old back-to-back behavior.
AUTOMATION_LAUNCH_SPACING_SECONDS = int(os.getenv("AUTOMATION_LAUNCH_SPACING_SECONDS", "90"))
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
# Comma-separated first-party browser origins. Keep this explicit because the
# API uses credentialed cookies; a wildcard origin is invalid and unsafe here.
CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ORIGINS", "https://app.kappgen.com,https://kappgen.com,http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]
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
# Pexels' free video API (see src/pipeline/stock_video.py) — real stock footage
# as an automatic visual source, so a scene can be actual motion instead of a
# Ken Burns pan over a still. Free with a key (200 req/hour, 20 000/month) and
# never charges the creator any KappGen credits. Unset simply disables the
# stock-footage source; every other visual source keeps working.
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
# Google Programmable Search Engine (Custom Search JSON API), image search
# mode — extra b-roll source for the facecam pipeline (facecam_broll.py),
# alongside Pexels and the community library. Both are required together;
# either missing disables this source only, every other source keeps working.
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX", "")
# Low-cost/free text-generation providers, both OpenAI-compatible — see
# src/pipeline/ai_text.py. Neither is required; the admin picks which
# configured provider goes first via the "Ressources" tab (falls back
# through the rest automatically if the chosen one fails).
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()] or ([GEMINI_API_KEY] if GEMINI_API_KEY else [])

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

# .heic/.heif (the default photo format on iPhone since iOS 11) needs the
# pillow-heif plugin registered before Pillow can open one at all — without
# it, every photo folder imported straight off an iPhone had its HEIC files
# silently rejected. They're not a format anything downstream (ffmpeg
# included) reliably reads either way, so they're converted to JPEG on
# upload rather than just accepted as-is — see save_valid_library_images.
# Only added to the allowlist if the plugin actually loaded: accepting the
# extension without being able to decode it would just turn a clean
# rejection into a silently-broken library image.
HEIC_EXTENSIONS = {".heic", ".heif"}
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    IMAGE_UPLOAD_EXTENSIONS |= HEIC_EXTENSIONS
except ImportError:
    HEIC_EXTENSIONS = set()

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
