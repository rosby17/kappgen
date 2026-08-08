import hashlib
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.config import GOOGLE_CLIENT_ID
from src.db.session import get_db
from src.db.models import User
from src.models.project import UserCreate, UserLogin, ChangePasswordPayload, ResetPasswordPayload

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
def register_user(payload: UserCreate, db: Session = Depends(get_db)):
    email_clean = payload.email.strip().lower()
    if not email_clean:
        raise HTTPException(status_code=400, detail="L'adresse email est requise.")
        
    existing = db.query(User).filter(User.email == email_clean).first()
    if existing:
        raise HTTPException(status_code=400, detail="Un compte existe déjà avec cette adresse email.")
        
    default_name = email_clean.split('@')[0].capitalize()
    user = User(
        email=email_clean,
        name=default_name,
        hashed_password=hash_password(payload.password)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.to_dict()

@router.post("/login")
def login_user(payload: UserLogin, db: Session = Depends(get_db)):
    email_clean = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")
    return user.to_dict()

@router.post("/change-password")
def change_password(payload: ChangePasswordPayload, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")
        
    if not verify_password(payload.old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="L'ancien mot de passe est incorrect.")
        
    if len(payload.new_password) < 4:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe est trop court.")
        
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"message": "Mot de passe modifié avec succès."}

@router.post("/reset-password")
def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    email_clean = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user:
        raise HTTPException(status_code=404, detail="Aucun compte trouvé avec cet e-mail.")
        
    user.hashed_password = hash_password(payload.new_password)
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
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif picture_url and user.picture_url != picture_url:
        user.picture_url = picture_url
        db.commit()
        db.refresh(user)
    return user.to_dict()


@router.get("/me/{user_id}")
def get_user_profile(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")
    return user.to_dict()


class ProfileUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None


@router.patch("/me/{user_id}")
def update_user_profile(user_id: str, payload: ProfileUpdate, db: Session = Depends(get_db)):
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
    db.commit()
    db.refresh(user)
    return user.to_dict()
