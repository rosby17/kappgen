"""Admin-managed pool of shared Izivoice API keys — same rotation pattern as
Hugging Face's account pool (src/pipeline/images.py's _hf_accounts_from_db /
_mark_hf_account), generalized here so voiceover.py, images.py, and music.py
can all fail over from one exhausted/invalid Izivoice key to the next without
a redeploy. Only applies to the SHARED/default key used when a creator hasn't
connected their own personal Izivoice key (src/utils/credentials.py) — a
creator's own key is never rotated away from.
"""
from datetime import datetime
from typing import Any, List, Optional


def izivoice_accounts_from_db() -> List[dict]:
    """Ordered by last_used_at ascending (nulls first) so load spreads evenly.
    Falls back to the single static IZIVOICE_API_KEY (wrapped as one entry,
    id=None) if the pool table is empty, so an existing single-env-var
    deployment keeps working before any account is added via the admin UI."""
    from src.db.session import SessionLocal
    from src.db.models import IzivoiceAccount
    db = SessionLocal()
    try:
        rows = (
            db.query(IzivoiceAccount)
            .filter(IzivoiceAccount.is_enabled == True)  # noqa: E712
            .order_by(IzivoiceAccount.last_used_at.asc().nullsfirst())
            .all()
        )
        if rows:
            return [{"id": r.id, "token": r.token} for r in rows]
    finally:
        db.close()
    from src.config import IZIVOICE_API_KEY
    return [{"id": None, "token": IZIVOICE_API_KEY}] if IZIVOICE_API_KEY else []


def mark_izivoice_account(account_id: Optional[str], status: str, error: Optional[str] = None) -> None:
    """Best-effort status update after a real attempt — never allowed to fail
    the actual generation it's tracking."""
    if not account_id:
        return
    try:
        from src.db.session import SessionLocal
        from src.db.models import IzivoiceAccount
        db = SessionLocal()
        try:
            account = db.query(IzivoiceAccount).filter(IzivoiceAccount.id == account_id).first()
            if account:
                account.status = status
                account.last_used_at = datetime.utcnow()
                if status != "active":
                    account.last_error = error
                db.commit()
        finally:
            db.close()
    except Exception:
        pass


def resolve_izivoice_candidates(api_key: Optional[str] = None) -> List[dict]:
    """A creator's own connected key (api_key, from src/utils/credentials.py)
    always wins and is never rotated away from — only the shared/default key
    draws from the admin-managed pool."""
    if api_key:
        return [{"id": None, "token": api_key}]
    return izivoice_accounts_from_db()
