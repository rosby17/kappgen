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

# Thumbnails default to Izivoice's GPT Image 2 (best style/character-reference
# behavior admins are used to), with fal.ai's own GPT Image 2 as an actual
# fallback now (see generate_thumbnail_image, images.py) rather than being
# hardcoded to Izivoice alone regardless of this setting — Izivoice's account
# hitting its own quota/auth issues used to mean a total thumbnail outage
# with no way to fail over, even though the admin settings UI already
# offered fal as a choice here. Scene images keep their own independent
# source/provider policy.
THUMBNAIL_PROVIDER_ORDER_KEY = "thumbnail_provider_order"
THUMBNAIL_PROVIDERS_ALL = ["izivoice", "fal"]
THUMBNAIL_PROVIDER_ORDER_DEFAULT = ["izivoice"]


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
        return list(THUMBNAIL_PROVIDER_ORDER_DEFAULT)
    try:
        order = json.loads(raw)
        return [p for p in order if p in THUMBNAIL_PROVIDERS_ALL] if isinstance(order, list) else list(THUMBNAIL_PROVIDER_ORDER_DEFAULT)
    except (ValueError, TypeError):
        return list(THUMBNAIL_PROVIDER_ORDER_DEFAULT)


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
