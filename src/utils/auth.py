"""Session tokens and auth dependencies.

Before this module existed, every route trusted a client-supplied user_id
with zero verification — anyone could edit a query param and act as any
user, or claim to be an admin. Login/register now issue a signed JWT; every
route that needs to know "who is calling" depends on get_current_user (or
get_current_admin) instead of trusting a param the client controls.
"""
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Cookie, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session
from src.config import SECRET_KEY, FRONTEND_BASE_URL
from src.db.session import get_db
from src.db.models import User

JWT_ALGORITHM = "HS256"
SESSION_TOKEN_LIFETIME_DAYS = 30
SESSION_COOKIE_NAME = "kappgen_session"
# kappgen.com/*.kappgen.com only see a scoped cookie in prod; localhost dev
# gets a host-only cookie (Domain can't be "localhost" for most browsers).
_IS_PROD = FRONTEND_BASE_URL.startswith("https://")
_COOKIE_DOMAIN = ".kappgen.com" if _IS_PROD else None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TOKEN_LIFETIME_DAYS * 24 * 3600,
        httponly=True,
        secure=_IS_PROD,
        samesite="lax",
        domain=_COOKIE_DOMAIN,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, domain=_COOKIE_DOMAIN, path="/")


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


def get_current_user(
    authorization: str = Header(None),
    session_cookie: str = Cookie(None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    # Authorization header stays supported (existing sessions, non-browser
    # API callers); the frontend itself now relies on the httpOnly cookie so
    # the session token is never readable from JS (mitigates token theft via
    # a hypothetical XSS bug — previously stored in localStorage).
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):].strip()
    elif session_cookie:
        token = session_cookie
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise.")
    user_id = _decode_token(token)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Compte introuvable.")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès réservé aux administrateurs.")
    return user
