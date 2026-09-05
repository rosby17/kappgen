"""Thin read/write helpers around AppSetting (see src/db/models.py) — global
admin-controlled flags with no redeploy needed. Each caller opens its own
short-lived session so this can be called from anywhere (a request handler
with a `db` already in scope, or deep pipeline code that doesn't have one)
without threading a session through every call site.
"""
import json
from typing import List, Optional
from src.db.session import SessionLocal
from src.db.models import AppSetting

# Emergency kill switch: one flag, checked from every provider-order resolver
# in the codebase (ai_providers.py for text/vision, voiceover.py, music.py,
# images.py's generate_thumbnail_image) — when on, each of those drops every
# provider not explicitly marked free (see each call site's own "free" set)
# from its candidate list, so a personal account running out of credits
# (Izivoice, Claude/Anthropic, ai33.pro, fal.ai, OpenAI...) can't keep
# getting billed by KappGen's automated renders while the operator tops it
# back up. Categories with no free alternative (voice, music) simply stop
# generating rather than degrade to something free-but-wrong — silently
# spending money nobody currently has is worse than a paused feature.
PAID_APIS_DISABLED_KEY = "paid_apis_disabled"


def paid_apis_disabled() -> bool:
    return get_setting(PAID_APIS_DISABLED_KEY, "false") == "true"


def set_paid_apis_disabled(disabled: bool) -> None:
    set_setting(PAID_APIS_DISABLED_KEY, "true" if disabled else "false")


# Thumbnails: ai33.pro direct (bypasses Izivoice's own account/quota
# entirely — same reasoning as voiceover_provider_order below), Izivoice,
# and fal.ai's own GPT Image 2 all wrap the same underlying model, so this
# is purely about which account's quota gets spent and which one to fail
# over to (see generate_thumbnail_image, images.py) — it used to be
# hardcoded to Izivoice alone regardless of this setting, which meant a
# total thumbnail outage with no way to fail over the moment Izivoice's
# account hit its own quota/auth issues. Scene images keep their own
# independent source/provider policy.
THUMBNAIL_PROVIDER_ORDER_KEY = "thumbnail_provider_order"
THUMBNAIL_PROVIDERS_ALL = ["izivoice", "fal", "ai33pro", "huggingface"]
THUMBNAIL_PROVIDER_ORDER_DEFAULT = ["izivoice"]
# The only genuinely free option here — same FLUX.1-schnell free tier the
# scene-image generator already defaults to. Forced into the order (even if
# the admin never explicitly enabled it) when paid_apis_disabled() is on, so
# the kill switch degrades to "still generates something" rather than
# "thumbnails stop working entirely" for a channel that had never touched
# this setting.
THUMBNAIL_FREE_PROVIDERS = {"huggingface"}


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        return row.value if row else default
    finally:
        db.close()


def set_setting(key: str, value: str) -> None:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    finally:
        db.close()


def thumbnail_provider_order() -> List[str]:
    raw = get_setting(THUMBNAIL_PROVIDER_ORDER_KEY, None)
    if raw is None:
        order = list(THUMBNAIL_PROVIDER_ORDER_DEFAULT)
    else:
        try:
            parsed = json.loads(raw)
            order = [p for p in parsed if p in THUMBNAIL_PROVIDERS_ALL] if isinstance(parsed, list) else list(THUMBNAIL_PROVIDER_ORDER_DEFAULT)
        except (ValueError, TypeError):
            order = list(THUMBNAIL_PROVIDER_ORDER_DEFAULT)
    if paid_apis_disabled():
        free_only = [p for p in order if p in THUMBNAIL_FREE_PROVIDERS]
        return free_only or ["huggingface"]
    return order


def set_thumbnail_provider_order(order: List[str]) -> None:
    set_setting(THUMBNAIL_PROVIDER_ORDER_KEY, json.dumps(order))


# Admin-defined priority order for text-generation providers (see
# src/pipeline/ai_text.py) — "anthropic" | "deepseek" | "groq" | "openai" |
# "fal", in the order they should be tried. Any configured provider left out
# of this list is still appended after it (in the module's default order),
# so nothing is ever unreachable — this only lets the admin push preferred
# providers to the front, e.g. move off Claude the instant its balance runs
# low, without a redeploy. Set from the "Ressources" tab.
AI_TEXT_PROVIDER_ORDER_KEY = "ai_text_provider_order"


# Groq and Gemini are the only text/vision providers with a real free tier
# this codebase relies on (see ai_text.py's generate_text docstring: "Groq's
# free tier", "Gemini before any paid provider") — Anthropic, DeepSeek, fal.ai
# and OpenAI are all pay-per-token. The actual enforcement point is
# ai_providers.py's ids_for() (the single choke point every text AND vision
# call in the app goes through) rather than here, since ordered_ids() always
# appends every capable-but-unranked provider behind the ranked ones — this
# order list alone can only reorder, never actually remove a provider from
# the chain.
AI_TEXT_FREE_PROVIDERS = {"groq", "gemini"}


def ai_text_provider_order() -> List[str]:
    raw = get_setting(AI_TEXT_PROVIDER_ORDER_KEY, None)
    if not raw:
        return []
    try:
        order = json.loads(raw)
        return [p for p in order if isinstance(p, str)] if isinstance(order, list) else []
    except (ValueError, TypeError):
        return []


def set_ai_text_provider_order(order: List[str]) -> None:
    set_setting(AI_TEXT_PROVIDER_ORDER_KEY, json.dumps(order))


# Admin-defined priority order for voiceover/TTS providers — only "izivoice"
# exists today (a single shared key, not a pool — see src/pipeline/voiceover.py),
# but this is the same order-picker structure as thumbnails/AI-text above so
# a second provider (e.g. ElevenLabs) can be added later without changing the
# admin UI's shape, just this list.
VOICEOVER_PROVIDER_ORDER_KEY = "voiceover_provider_order"
# "ai33pro" is the direct upstream provider Izivoice itself resells (see
# src/pipeline/ai33_provider.py) — added so KappGen's own automated volume
# can stop consuming Izivoice's separate business account. Default order is
# unchanged (izivoice first) so this is opt-in only, from the admin UI.
VOICEOVER_PROVIDERS_ALL = ["izivoice", "ai33pro"]
VOICEOVER_PROVIDER_ORDER_DEFAULT = ["izivoice"]


def voiceover_provider_order() -> List[str]:
    if paid_apis_disabled():
        # Both izivoice and ai33pro are paid — no free voice provider exists
        # in this codebase, so the kill switch means voice generation simply
        # stops rather than degrading to something free-but-wrong.
        return []
    raw = get_setting(VOICEOVER_PROVIDER_ORDER_KEY, None)
    if raw is None:
        return list(VOICEOVER_PROVIDER_ORDER_DEFAULT)
    try:
        order = json.loads(raw)
        return [p for p in order if p in VOICEOVER_PROVIDERS_ALL] if isinstance(order, list) else list(VOICEOVER_PROVIDER_ORDER_DEFAULT)
    except (ValueError, TypeError):
        return list(VOICEOVER_PROVIDER_ORDER_DEFAULT)


def set_voiceover_provider_order(order: List[str]) -> None:
    set_setting(VOICEOVER_PROVIDER_ORDER_KEY, json.dumps(order))


# Same order-picker structure again, for background music generation
# (src/pipeline/music.py) — Izivoice's own /music route is itself a thin
# passthrough to this same ai33.pro endpoint, so routing there directly
# bypasses Izivoice's account/quota the same way voice and thumbnails do.
MUSIC_PROVIDER_ORDER_KEY = "music_provider_order"
MUSIC_PROVIDERS_ALL = ["izivoice", "ai33pro"]
MUSIC_PROVIDER_ORDER_DEFAULT = ["izivoice"]


def music_provider_order() -> List[str]:
    if paid_apis_disabled():
        # Both izivoice and ai33pro are paid — no free music provider exists,
        # so the kill switch means music generation simply stops (the
        # existing synthetic-drone fallback still kicks in downstream, same
        # as any other total-failure case — see _generate_synthetic_fallback_track).
        return []
    raw = get_setting(MUSIC_PROVIDER_ORDER_KEY, None)
    if raw is None:
        return list(MUSIC_PROVIDER_ORDER_DEFAULT)
    try:
        order = json.loads(raw)
        return [p for p in order if p in MUSIC_PROVIDERS_ALL] if isinstance(order, list) else list(MUSIC_PROVIDER_ORDER_DEFAULT)
    except (ValueError, TypeError):
        return list(MUSIC_PROVIDER_ORDER_DEFAULT)


def set_music_provider_order(order: List[str]) -> None:
    set_setting(MUSIC_PROVIDER_ORDER_KEY, json.dumps(order))


# Admin-adjustable number of videos the worker renders at once. Read live by
# each render lane on every poll (see src/worker/queue_runner.py) rather than
# once at process boot, so turning this up or down from the admin UI takes
# effect within a few seconds — no redeploy or restart needed. Ceiling of 4
# matches the worker container's Docker CPU cap (2.5 cores at time of
# writing): beyond that, lanes start fighting each other for CPU instead of
# actually finishing videos faster, so the admin UI itself refuses more.
MAX_CONCURRENT_RENDERS_KEY = "max_concurrent_renders"
MAX_CONCURRENT_RENDERS_DEFAULT = 2
MAX_CONCURRENT_RENDERS_CEILING = 4


def max_concurrent_renders() -> int:
    raw = get_setting(MAX_CONCURRENT_RENDERS_KEY, None)
    if raw is None:
        return MAX_CONCURRENT_RENDERS_DEFAULT
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return MAX_CONCURRENT_RENDERS_DEFAULT
    return max(1, min(MAX_CONCURRENT_RENDERS_CEILING, value))


def set_max_concurrent_renders(value: int) -> None:
    value = max(1, min(MAX_CONCURRENT_RENDERS_CEILING, int(value)))
    set_setting(MAX_CONCURRENT_RENDERS_KEY, str(value))
