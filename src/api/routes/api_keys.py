import hashlib
import secrets
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import ApiKey, User

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class ApiKeyCreate(BaseModel):
    user_id: str
    name: str = "Clé API"


@router.get("")
def list_api_keys(user_id: str, db: Session = Depends(get_db)):
    keys = db.query(ApiKey).filter(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc()).all()
    return [k.to_dict() for k in keys]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_api_key(payload: ApiKeyCreate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")

    raw_key = f"nck_{secrets.token_urlsafe(32)}"
    api_key = ApiKey(
        user_id=user.id,
        name=(payload.name or "Clé API").strip() or "Clé API",
        key_prefix=raw_key[:12],
        hashed_key=_hash_key(raw_key),
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    result = api_key.to_dict()
    # The raw key is only ever returned once, at creation time — the backend
    # only stores its hash, matching how GitHub/Stripe-style API keys work.
    result["key"] = raw_key
    return result


@router.delete("/{key_id}")
def revoke_api_key(key_id: str, db: Session = Depends(get_db)):
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="Clé API introuvable.")
    db.delete(api_key)
    db.commit()
    return {"message": "Clé API révoquée."}
