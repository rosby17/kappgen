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

# "free_only" — thumbnails are generated exclusively via the free Hugging
# Face path; on failure, no paid provider is ever called (falls through to
# generate_thumbnail's own video-frame-grab fallback instead), same
# guarantee the per-scene body images already have.
# "free_then_paid" — current default behavior: free tier tried first, then
# fal.ai, then Izivoice on failure (each attempt spending real money/credits).
THUMBNAIL_PROVIDER_MODE_KEY = "thumbnail_provider_mode"
THUMBNAIL_PROVIDER_MODE_DEFAULT = "free_only"


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


def thumbnail_provider_mode() -> str:
    return get_setting(THUMBNAIL_PROVIDER_MODE_KEY, THUMBNAIL_PROVIDER_MODE_DEFAULT)


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
