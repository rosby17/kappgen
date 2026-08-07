import hashlib
import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import User
from src.models.project import UserCreate, UserLogin, ChangePasswordPayload, ResetPasswordPayload

router = APIRouter(prefix="/api/auth", tags=["auth"])

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

@router.get("/me/{user_id}")
def get_user_profile(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé.")
    return user.to_dict()
