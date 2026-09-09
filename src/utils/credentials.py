import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from src.config import CREDENTIAL_ENCRYPTION_KEY, IZIVOICE_API_KEY


def _fernet() -> Fernet:
    seed = CREDENTIAL_ENCRYPTION_KEY or IZIVOICE_API_KEY
    if not seed:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be configured before storing provider credentials.")
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_credential(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_credential(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("The stored provider credential can no longer be decrypted.") from exc


def izivoice_key_for_user(user) -> str:
    """Customer BYOK first, NicheCut's shared key as the seamless fallback.

    The stored credential can become undecryptable if the encryption seed
    changes under it (e.g. CREDENTIAL_ENCRYPTION_KEY isn't set, so IZIVOICE_API_KEY
    doubles as the seed, and that key later gets rotated) — every voice
    endpoint for that user would otherwise 500 forever. Fall back to the
    shared key instead of raising, same as if BYOK were never set."""
    try:
        return decrypt_credential(getattr(user, "izivoice_api_key_encrypted", None)) or IZIVOICE_API_KEY
    except RuntimeError:
        return IZIVOICE_API_KEY


def key_prefix(raw_key: str) -> str:
    """Short, non-secret preview shown in the UI once a key is saved (e.g.
    'sk-ant-...I9kQ') — enough to recognize which key is connected without
    ever displaying (or re-fetching) the real value."""
    raw_key = (raw_key or "").strip()
    if len(raw_key) <= 12:
        return raw_key[:4] + "…" if raw_key else ""
    return f"{raw_key[:8]}…{raw_key[-4:]}"


def get_user_provider_key(user, provider_id: str) -> Optional[str]:
    """The user's own connected+enabled key for one text-generation provider
    (see src/pipeline/ai_providers.py), decrypted — or None if they haven't
    connected one, disabled it, or it can no longer be decrypted (encryption
    seed rotated: same fail-open-to-platform behavior as izivoice_key_for_user,
    never a hard error)."""
    entries = getattr(user, "external_ai_keys", None) or {}
    entry = entries.get(provider_id)
    if not entry or not entry.get("enabled", True) or not entry.get("encrypted"):
        return None
    try:
        return decrypt_credential(entry["encrypted"])
    except RuntimeError:
        return None
