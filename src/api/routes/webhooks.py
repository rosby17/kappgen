"""Inbound webhooks from external providers — no session auth, since the
caller is a third-party server, not a logged-in user.
"""
from fastapi import APIRouter, Request
from datetime import datetime

from src.db.session import SessionLocal
from src.db.models import Ai33TaskResult
from src.utils.logger import logger

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/ai33/speech-to-text")
async def ai33_speech_to_text_webhook(request: Request):
    """ai33.pro POSTs the final result here once a speech-to-text task
    (submitted with receive_url, see src/pipeline/ai33_provider.py::
    submit_stt_with_webhook) completes. Not authenticated — ai33.pro signs
    nothing — so this only ever RECORDS the payload; it is never trusted
    for anything until a caller that itself submitted this exact task_id
    reads it back (see await_stt_webhook_result). An unsolicited task_id is
    simply a row nothing ever reads, not a security hole.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"success": False}

    task_id = payload.get("id") or payload.get("task_id")
    if not task_id or not isinstance(task_id, str):
        return {"success": False}

    status = str(payload.get("status") or "").lower()
    db = SessionLocal()
    try:
        row = db.query(Ai33TaskResult).filter(Ai33TaskResult.task_id == task_id).first()
        if row:
            row.status = status
            row.payload = payload
            row.received_at = datetime.utcnow()
        else:
            db.add(Ai33TaskResult(task_id=task_id, status=status, payload=payload, received_at=datetime.utcnow()))
        db.commit()
    except Exception:
        logger.exception(f"Failed to record ai33.pro STT webhook for task {task_id}")
        db.rollback()
    finally:
        db.close()

    return {"success": True}
