"""Session tokens and auth dependencies.

Before this module existed, every route trusted a client-supplied user_id
with zero verification — anyone could edit a query param and act as any
user, or claim to be an admin. Login/register now issue a signed JWT; every
route that needs to know "who is calling" depends on get_current_user (or
get_current_admin) instead of trusting a param the client controls.
"""
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from src.config import SECRET_KEY
from src.db.session import get_db
from src.db.models import User

JWT_ALGORITHM = "HS256"
SESSION_TOKEN_LIFETIME_DAYS = 30


def create_session_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=SESSION_TOKEN_LIFETIME_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expirée, reconnectez-vous.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalide.")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalide.")
    return user_id


def get_current_user(authorization: str = Header(None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise.")
    user_id = _decode_token(authorization[len("Bearer "):].strip())
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Compte introuvable.")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès réservé aux administrateurs.")
    return user
