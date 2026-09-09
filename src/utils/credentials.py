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
