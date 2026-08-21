import hashlib
import os
import secrets
import httpx
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.config import GOOGLE_CLIENT_ID, FRONTEND_BASE_URL
from src.db.session import get_db
from src.db.models import PasswordReset, User
from src.models.project import UserCreate, UserLogin, ForgotPasswordPayload, ChangePasswordPayload, ResetPasswordPayload
from src.utils.logger import logger
from src.utils.auth import create_session_token, get_current_user
from src.utils.email import SUPPORTED_LOCALES, detect_locale, send_brevo_email, email_shell, EMAIL_ACCENT

router = APIRouter(prefix="/api/auth", tags=["auth"])
RESET_CODE_LIFETIME_MINUTES = 10
RESET_MAX_REQUESTS_PER_HOUR = 5
RESET_MAX_ATTEMPTS = 5
RESET_GENERIC_MESSAGE = "Si un compte correspond à cette adresse, un code de vérification vient d'être envoyé."
# The first 20 accounts ever created get a bigger free trial (10 videos)
# instead of the standard 3 — confirmed by the user, a one-time early-user
# bonus, not a recurring promotion.
EARLY_USER_COUNT = 20
EARLY_USER_FREE_QUOTA = 10
STANDARD_FREE_QUOTA = 3


def _free_quota_for_new_user(db: Session) -> int:
    existing_count = db.query(User).count()
    return EARLY_USER_FREE_QUOTA if existing_count < EARLY_USER_COUNT else STANDARD_FREE_QUOTA


def _auth_response(user: User) -> dict:
    return {"user": user.to_dict(), "token": create_session_token(user.id)}


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

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(payload: UserCreate, request: Request, db: Session = Depends(get_db)):
    email_clean = payload.email.strip().lower()
    if not email_clean:
        raise HTTPException(status_code=400, detail="L'adresse email est requise.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Le mot de passe doit contenir au moins 8 caractères.")

    existing = db.query(User).filter(User.email == email_clean).first()
    if existing:
        raise HTTPException(status_code=400, detail="Un compte existe déjà avec cette adresse email.")

    default_name = email_clean.split('@')[0].capitalize()
    user = User(
        email=email_clean,
        name=default_name,
        hashed_password=hash_password(payload.password),
        free_video_quota_granted=_free_quota_for_new_user(db),
        locale=detect_locale(request.headers.get("accept-language")),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        send_welcome_email(user.email, user.name, user.locale)
    except Exception as exc:
        logger.error(f"Welcome email failed for user {user.id}: {exc}")

    return _auth_response(user)

@router.post("/login")
def login_user(payload: UserLogin, db: Session = Depends(get_db)):
    email_clean = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")
    return _auth_response(user)

@router.post("/change-password")
def change_password(payload: ChangePasswordPayload, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")

    if not verify_password(payload.old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="L'ancien mot de passe est incorrect.")
        
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe est trop court.")
        
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
def forgot_password(payload: ForgotPasswordPayload, db: Session = Depends(get_db)):
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
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe doit contenir au moins 8 caractères.")
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
def google_auth(payload: GoogleAuthPayload, db: Session = Depends(get_db)):
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
            free_video_quota_granted=_free_quota_for_new_user(db),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif picture_url and user.picture_url != picture_url:
        user.picture_url = picture_url
        db.commit()
        db.refresh(user)
    return _auth_response(user)


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
