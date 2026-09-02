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

# Admin-defined priority order for thumbnail image providers — "huggingface"
# (free), "fal" (paid, best fidelity to reference images), "izivoice" (paid).
# A provider left out of the order is simply never tried — unlike the AI-text
# chain, there's no "everything still reachable" fallback here, because
# including fal/izivoice at all is itself the admin's explicit opt-in to
# spend money; leaving them out keeps the old "free_only" guarantee (no paid
# provider ever touched, same as the per-scene body images). Default is
# Hugging Face alone, preserving that free-only guarantee until an admin
# explicitly adds a paid provider to the order from the "Ressources" tab.
THUMBNAIL_PROVIDER_ORDER_KEY = "thumbnail_provider_order"
THUMBNAIL_PROVIDERS_ALL = ["huggingface", "fal", "izivoice"]
THUMBNAIL_PROVIDER_ORDER_DEFAULT = ["huggingface"]


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
VOICEOVER_PROVIDERS_ALL = ["izivoice"]
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
