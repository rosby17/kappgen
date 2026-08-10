from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from src.db.session import get_db
from src.db.models import Folder, Video

router = APIRouter(prefix="/api/folders", tags=["folders"])


class FolderCreate(BaseModel):
    name: str
    user_id: Optional[str] = None


class FolderRename(BaseModel):
    name: str


@router.get("")
def list_folders(user_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Folder)
    if user_id:
        query = query.filter(Folder.user_id == user_id)
    folders = query.order_by(Folder.created_at.asc()).all()
    return [f.to_dict() for f in folders]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_folder(payload: FolderCreate, db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom du dossier ne peut pas être vide.")
    folder = Folder(name=name, user_id=payload.user_id)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder.to_dict()


@router.patch("/{folder_id}")
def rename_folder(folder_id: str, payload: FolderRename, db: Session = Depends(get_db)):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom du dossier ne peut pas être vide.")
    folder.name = name
    db.commit()
    db.refresh(folder)
    return folder.to_dict()


@router.delete("/{folder_id}")
def delete_folder(folder_id: str, db: Session = Depends(get_db)):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    db.query(Video).filter(Video.folder_id == folder_id).update({Video.folder_id: None})
    db.delete(folder)
    db.commit()
    return {"message": "Dossier supprimé (les vidéos ont été replacées hors dossier)."}
