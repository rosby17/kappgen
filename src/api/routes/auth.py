import hashlib
import os
import secrets
import httpx
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.config import GOOGLE_CLIENT_ID, FRONTEND_BASE_URL
from src.db.session import get_db
from src.db.models import PasswordReset, User
from src.models.project import UserCreate, UserLogin, ForgotPasswordPayload, ChangePasswordPayload, ResetPasswordPayload
from src.utils.logger import logger
from src.utils.auth import create_session_token, get_current_user, set_session_cookie, clear_session_cookie
from src.utils.email import SUPPORTED_LOCALES, detect_locale, send_brevo_email, email_shell, EMAIL_ACCENT
from src.utils.rate_limit import rate_limit
from src.utils.billing import grant_welcome_credits

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/session")
def get_session(current_user: User = Depends(get_current_user)):
    # Purpose-built for the marketing landing page (kappgen.com): the session
    # cookie is scoped to .kappgen.com, so this lets it check "is this visitor
    # already logged in?" (to show "Accéder à KappGen" instead of the
    # login/signup buttons) without needing the user's id up front — 401 via
    # get_current_user when there's no valid cookie.
    return {"id": current_user.id, "name": current_user.name}

_limit_login = rate_limit("login", max_attempts=10, window_seconds=300)
_limit_register = rate_limit("register", max_attempts=5, window_seconds=3600)
_limit_forgot = rate_limit("forgot", max_attempts=5, window_seconds=3600)
_limit_verify_resend = rate_limit("verify_resend", max_attempts=5, window_seconds=3600)
EMAIL_VERIFY_TOKEN_BYTES = 32
RESET_CODE_LIFETIME_MINUTES = 10
RESET_MAX_REQUESTS_PER_HOUR = 5
RESET_MAX_ATTEMPTS = 5
RESET_GENERIC_MESSAGE = "Si un compte correspond à cette adresse, un code de vérification vient d'être envoyé."
# New accounts no longer get a flat "N free videos" quota (free_video_quota_granted
# stays 0) — they get a spendable credit pot instead (grant_welcome_credits,
# see registration below), so free usage tracks real cost per video rather
# than counting a cheap short video the same as an hour-long AI-generated
# one. Existing accounts created before this change keep whatever quota they
# already had; this only affects new registrations going forward.


def _auth_response(user: User, response: Response) -> dict:
    token = create_session_token(user.id)
    set_session_cookie(response, token)
    return {"user": user.to_dict(), "token": token}


class GoogleAuthPayload(BaseModel):
    credential: str

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return f"{salt.hex()}:{key.hex()}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        parts = hashed_password.split(':')
        if len(parts) != 2:
            return False
        salt_hex, key_hex = parts[0], parts[1]
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt, 100000)
        return key.hex() == key_hex
    except Exception:
        return False

def _weak_password_reason(password: str) -> Optional[str]:
    """8+ chars was the only rule — "aaaaaaaa" passed, confirmed live. Length
    still matters most (NIST 800-63B), so no forced complexity theater —
    just reject the trivially-guessable shapes a length check alone lets through."""
    if len(password) < 8:
        return "Le mot de passe doit contenir au moins 8 caractères."
    if len(set(password.lower())) <= 2:
        return "Ce mot de passe est trop simple (trop peu de caractères différents)."
    if password.isdigit() or password.isalpha():
        return "Le mot de passe doit contenir à la fois des lettres et des chiffres."
    return None

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(payload: UserCreate, request: Request, response: Response, db: Session = Depends(get_db), _rl=Depends(_limit_register)):
    email_clean = payload.email.strip().lower()
    if not email_clean:
        raise HTTPException(status_code=400, detail="L'adresse email est requise.")
    weak_reason = _weak_password_reason(payload.password)
    if weak_reason:
        raise HTTPException(status_code=400, detail=weak_reason)

    existing = db.query(User).filter(User.email == email_clean).first()
    if existing:
        raise HTTPException(status_code=400, detail="Un compte existe déjà avec cette adresse email.")

    default_name = email_clean.split('@')[0].capitalize()
    user = User(
        email=email_clean,
        name=default_name,
        hashed_password=hash_password(payload.password),
        free_video_quota_granted=0,
        locale=detect_locale(request.headers.get("accept-language")),
        email_verify_token=secrets.token_urlsafe(EMAIL_VERIFY_TOKEN_BYTES),
        email_verify_sent_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    grant_welcome_credits(db, user)

    try:
        send_welcome_email(user.email, user.name, user.locale)
    except Exception as exc:
        logger.error(f"Welcome email failed for user {user.id}: {exc}")

    try:
        send_verification_email(user.email, user.email_verify_token, user.locale)
    except Exception as exc:
        logger.error(f"Verification email failed for user {user.id}: {exc}")

    return _auth_response(user, response)

@router.post("/login")
def login_user(payload: UserLogin, response: Response, db: Session = Depends(get_db), _rl=Depends(_limit_login)):
    email_clean = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")
    return _auth_response(user, response)

@router.post("/logout")
def logout_user(response: Response):
    clear_session_cookie(response)
    return {"message": "Déconnecté."}

@router.post("/change-password")
def change_password(payload: ChangePasswordPayload, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")

    if not verify_password(payload.old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="L'ancien mot de passe est incorrect.")
        
    weak_reason = _weak_password_reason(payload.new_password)
    if weak_reason:
        raise HTTPException(status_code=400, detail=weak_reason)
        
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"message": "Mot de passe modifié avec succès."}

def send_password_reset_email(email: str, code: str, locale: str = "fr") -> None:
    copy = {
        "fr": {
            "eyebrow": "Récupération de compte",
            "title": "Réinitialise ton mot de passe",
            "intro": f"Saisis ce code dans KappGen. Il expire dans {RESET_CODE_LIFETIME_MINUTES} minutes et ne peut être utilisé qu'une fois.",
            "footer": "Si tu n'as pas demandé ce code, ignore simplement cet email — ton mot de passe ne sera pas modifié.",
            "subject": f"{code} — Code de récupération KappGen",
            "preheader": f"Ton code de récupération KappGen : {code}",
            "text": f"Ton code de récupération KappGen est {code}. Il expire dans {RESET_CODE_LIFETIME_MINUTES} minutes.",
        },
        "en": {
            "eyebrow": "Account recovery",
            "title": "Reset your password",
            "intro": f"Enter this code in KappGen. It expires in {RESET_CODE_LIFETIME_MINUTES} minutes and can only be used once.",
            "footer": "If you didn't request this code, just ignore this email — your password won't be changed.",
            "subject": f"{code} — KappGen recovery code",
            "preheader": f"Your KappGen recovery code: {code}",
            "text": f"Your KappGen recovery code is {code}. It expires in {RESET_CODE_LIFETIME_MINUTES} minutes.",
        },
    }[locale if locale in SUPPORTED_LOCALES else "fr"]

    body = f"""
      <p style="color:{EMAIL_ACCENT};font-size:12px;font-weight:700;letter-spacing:1.4px;margin:0 0 16px;text-transform:uppercase">{copy['eyebrow']}</p>
      <h1 style="color:#eaf6ff;font-size:24px;font-weight:700;margin:0 0 12px;line-height:1.3">{copy['title']}</h1>
      <p style="color:#9badc0;font-size:15px;line-height:1.7;margin:0 0 24px">{copy['intro']}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center" style="background:#0d141f;border:1px solid #26364a;border-radius:12px;padding:20px">
            <span style="color:{EMAIL_ACCENT};font-size:32px;font-weight:800;letter-spacing:8px;font-family:'Courier New',monospace">{code}</span>
          </td>
        </tr>
      </table>
      <p style="color:#5b6779;font-size:13px;line-height:1.6;margin:24px 0 0">{copy['footer']}</p>
    """
    send_brevo_email(
        email,
        copy["subject"],
        email_shell(copy["preheader"], body),
        copy["text"],
    )


def send_verification_email(email: str, token: str, locale: str = "fr") -> None:
    verify_url = f"{FRONTEND_BASE_URL}/verify-email?token={token}"
    copy = {
        "fr": {
            "eyebrow": "Vérification d'email",
            "title": "Confirme ton adresse email",
            "intro": "Clique sur le bouton ci-dessous pour confirmer ton adresse email et activer toutes les fonctionnalités de ton compte KappGen.",
            "cta": "Confirmer mon email",
            "subject": "Confirme ton adresse email — KappGen",
            "preheader": "Confirme ton adresse email pour activer ton compte KappGen",
            "text": f"Confirme ton adresse email en ouvrant ce lien : {verify_url}",
        },
        "en": {
            "eyebrow": "Email verification",
            "title": "Confirm your email address",
            "intro": "Click the button below to confirm your email address and unlock all features on your KappGen account.",
            "cta": "Confirm my email",
            "subject": "Confirm your email address — KappGen",
            "preheader": "Confirm your email address to activate your KappGen account",
            "text": f"Confirm your email address by opening this link: {verify_url}",
        },
    }[locale if locale in SUPPORTED_LOCALES else "fr"]

    body = f"""
      <p style="color:{EMAIL_ACCENT};font-size:12px;font-weight:700;letter-spacing:1.4px;margin:0 0 16px;text-transform:uppercase">{copy['eyebrow']}</p>
      <h1 style="color:#eaf6ff;font-size:24px;font-weight:700;margin:0 0 12px;line-height:1.3">{copy['title']}</h1>
      <p style="color:#9badc0;font-size:15px;line-height:1.7;margin:0 0 28px">{copy['intro']}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{EMAIL_ACCENT};border-radius:10px">
                  <a href="{verify_url}" style="display:inline-block;padding:12px 24px;color:#07101a;font-size:15px;font-weight:700;text-decoration:none">{copy['cta']}</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    """
    send_brevo_email(
        email,
        copy["subject"],
        email_shell(copy["preheader"], body),
        copy["text"],
    )


def send_welcome_email(email: str, name: str, locale: str = "fr") -> None:
    copy = {
        "fr": {
            "eyebrow": "Bienvenue",
            "title": f"Salut {name}, ton compte est prêt 🎬",
            "intro": "Tu peux dès maintenant déléguer tes chaînes à KappGen pour générer tes premières vidéos courtes.",
            "cta": "Déléguer mes chaînes à KappGen",
            "subject": "Bienvenue sur KappGen 🎬",
            "preheader": f"Bienvenue sur KappGen, {name} !",
            "text": f"Bienvenue sur KappGen, {name} ! Ton compte est prêt : {FRONTEND_BASE_URL}",
        },
        "en": {
            "eyebrow": "Welcome",
            "title": f"Hey {name}, your account is ready 🎬",
            "intro": "You can now delegate your channels to KappGen to generate your first short-form videos.",
            "cta": "Delegate my channels to KappGen",
            "subject": "Welcome to KappGen 🎬",
            "preheader": f"Welcome to KappGen, {name}!",
            "text": f"Welcome to KappGen, {name}! Your account is ready: {FRONTEND_BASE_URL}",
        },
    }[locale if locale in SUPPORTED_LOCALES else "fr"]

    body = f"""
      <p style="color:{EMAIL_ACCENT};font-size:12px;font-weight:700;letter-spacing:1.4px;margin:0 0 16px;text-transform:uppercase">{copy['eyebrow']}</p>
      <h1 style="color:#eaf6ff;font-size:24px;font-weight:700;margin:0 0 12px;line-height:1.3">{copy['title']}</h1>
      <p style="color:#9badc0;font-size:15px;line-height:1.7;margin:0 0 28px">{copy['intro']}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{EMAIL_ACCENT};border-radius:10px">
                  <a href="{FRONTEND_BASE_URL}/channels/new" style="display:inline-block;padding:12px 24px;color:#07101a;font-size:15px;font-weight:700;text-decoration:none">{copy['cta']}</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    """
    send_brevo_email(
        email,
        copy["subject"],
        email_shell(copy["preheader"], body),
        copy["text"],
    )


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordPayload, db: Session = Depends(get_db), _rl=Depends(_limit_forgot)):
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise HTTPException(status_code=503, detail="La récupération par email est temporairement indisponible.")

    email_clean = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user or user.auth_provider == "google":
        return {"message": RESET_GENERIC_MESSAGE}

    now = datetime.utcnow()
    recent_count = db.query(PasswordReset).filter(
        PasswordReset.user_id == user.id,
        PasswordReset.created_at >= now - timedelta(hours=1),
    ).count()
    if recent_count >= RESET_MAX_REQUESTS_PER_HOUR:
        return {"message": RESET_GENERIC_MESSAGE}

    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        send_password_reset_email(email_clean, code, user.locale)
    except Exception as exc:
        logger.error(f"Password reset email failed for user {user.id}: {exc}")
        raise HTTPException(status_code=503, detail="Impossible d'envoyer l'email pour le moment. Réessaie dans quelques minutes.")

    db.query(PasswordReset).filter(
        PasswordReset.user_id == user.id,
        PasswordReset.used_at.is_(None),
    ).update({"used_at": now}, synchronize_session=False)
    db.add(PasswordReset(
        user_id=user.id,
        code_hash=hash_password(code),
        expires_at=now + timedelta(minutes=RESET_CODE_LIFETIME_MINUTES),
    ))
    db.commit()
    return {"message": RESET_GENERIC_MESSAGE}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    email_clean = payload.email.strip().lower()
    weak_reason = _weak_password_reason(payload.new_password)
    if weak_reason:
        raise HTTPException(status_code=400, detail=weak_reason)
    if len(payload.code.strip()) != 6 or not payload.code.strip().isdigit():
        raise HTTPException(status_code=400, detail="Le code est invalide ou expiré.")

    user = db.query(User).filter(User.email == email_clean).first()
    if not user or user.auth_provider == "google":
        raise HTTPException(status_code=400, detail="Le code est invalide ou expiré.")

    reset = db.query(PasswordReset).filter(
        PasswordReset.user_id == user.id,
        PasswordReset.used_at.is_(None),
    ).order_by(PasswordReset.created_at.desc()).first()
    now = datetime.utcnow()
    if not reset or reset.expires_at < now or reset.attempts >= RESET_MAX_ATTEMPTS:
        raise HTTPException(status_code=400, detail="Le code est invalide ou expiré.")

    reset.attempts += 1
    if not verify_password(payload.code.strip(), reset.code_hash):
        db.commit()
        raise HTTPException(status_code=400, detail="Le code est invalide ou expiré.")

    user.hashed_password = hash_password(payload.new_password)
    reset.used_at = now
    db.commit()
    return {"message": "Mot de passe réinitialisé avec succès."}

@router.post("/google")
def google_auth(payload: GoogleAuthPayload, response: Response, db: Session = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="La connexion Google n'est pas configurée sur le serveur.")

    resp = httpx.get(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"id_token": payload.credential},
        timeout=15.0
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Jeton Google invalide ou expiré.")

    token_info = resp.json()
    if token_info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Jeton Google non destiné à cette application.")

    email_clean = (token_info.get("email") or "").strip().lower()
    if not email_clean:
        raise HTTPException(status_code=400, detail="Aucun email associé à ce compte Google.")

    picture_url = token_info.get("picture")
    user = db.query(User).filter(User.email == email_clean).first()
    if not user:
        user = User(
            email=email_clean,
            name=token_info.get("name") or email_clean.split('@')[0].capitalize(),
            hashed_password=hash_password(os.urandom(32).hex()),
            picture_url=picture_url,
            auth_provider="google",
            free_video_quota_granted=0,
            # Google already verified this address before issuing the id_token.
            email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        grant_welcome_credits(db, user)
    elif picture_url and user.picture_url != picture_url:
        user.picture_url = picture_url
        db.commit()
        db.refresh(user)
    return _auth_response(user, response)


@router.post("/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    token_clean = (token or "").strip()
    if not token_clean:
        raise HTTPException(status_code=400, detail="Jeton de vérification invalide.")
    user = db.query(User).filter(User.email_verify_token == token_clean).first()
    if not user:
        raise HTTPException(status_code=400, detail="Jeton de vérification invalide ou déjà utilisé.")
    user.email_verified = True
    user.email_verify_token = None
    db.commit()
    return {"message": "Adresse email confirmée."}


@router.post("/resend-verification")
def resend_verification(current_user: User = Depends(get_current_user), db: Session = Depends(get_db), _rl=Depends(_limit_verify_resend)):
    user = db.query(User).filter(User.id == current_user.id).first()
    if user.email_verified:
        return {"message": "Cette adresse email est déjà confirmée."}
    user.email_verify_token = secrets.token_urlsafe(EMAIL_VERIFY_TOKEN_BYTES)
    user.email_verify_sent_at = datetime.utcnow()
    db.commit()
    try:
        send_verification_email(user.email, user.email_verify_token, user.locale)
    except Exception as exc:
        logger.error(f"Verification email resend failed for user {user.id}: {exc}")
        raise HTTPException(status_code=503, detail="Impossible d'envoyer l'email pour le moment. Réessaie dans quelques minutes.")
    return {"message": "Email de vérification renvoyé."}


@router.get("/me/{user_id}")
def get_user_profile(user_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")
    return user.to_dict()


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    locale: Optional[str] = None


@router.patch("/me/{user_id}")
def update_user_profile(user_id: str, payload: ProfileUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Le nom ne peut pas être vide.")
        user.name = name
    if payload.phone is not None:
        user.phone = payload.phone.strip() or None
    if payload.locale is not None:
        if payload.locale not in SUPPORTED_LOCALES:
            raise HTTPException(status_code=400, detail="Langue non supportée.")
        user.locale = payload.locale
    db.commit()
    db.refresh(user)
    return user.to_dict()
