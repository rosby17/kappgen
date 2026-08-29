"""Thin read/write helpers around AppSetting (see src/db/models.py) — global
admin-controlled flags with no redeploy needed. Each caller opens its own
short-lived session so this can be called from anywhere (a request handler
with a `db` already in scope, or deep pipeline code that doesn't have one)
without threading a session through every call site.
"""
from typing import Optional
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
