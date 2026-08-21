from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Folder, Video, User
from src.utils.auth import get_current_user

router = APIRouter(prefix="/api/folders", tags=["folders"])


class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None


class FolderRename(BaseModel):
    name: str


class FolderMove(BaseModel):
    parent_id: Optional[str] = None


def _is_descendant(db: Session, folder_id: str, candidate_ancestor_id: str) -> bool:
    """True if candidate_ancestor_id is folder_id itself or one of its descendants —
    used to reject a move that would create a cycle in the folder tree."""
    current_id = candidate_ancestor_id
    seen = set()
    while current_id:
        if current_id == folder_id:
            return True
        if current_id in seen:
            break  # already-corrupt data; bail rather than loop forever
        seen.add(current_id)
        parent = db.query(Folder.parent_id).filter(Folder.id == current_id).first()
        current_id = parent[0] if parent else None
    return False


@router.get("")
def list_folders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    folders = db.query(Folder).filter(Folder.user_id == current_user.id).order_by(Folder.created_at.asc()).all()
    return [f.to_dict() for f in folders]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_folder(payload: FolderCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom du dossier ne peut pas être vide.")
    parent_id = payload.parent_id
    if parent_id:
        parent = db.query(Folder).filter(Folder.id == parent_id).first()
        if not parent or parent.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Dossier parent introuvable.")
    folder = Folder(name=name, user_id=current_user.id, parent_id=parent_id)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder.to_dict()


@router.patch("/{folder_id}/move")
def move_folder(folder_id: str, payload: FolderMove, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    if folder.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if payload.parent_id:
        if payload.parent_id == folder_id or _is_descendant(db, folder_id, payload.parent_id):
            raise HTTPException(status_code=400, detail="Impossible de déplacer un dossier dans lui-même ou l'un de ses sous-dossiers.")
        parent = db.query(Folder).filter(Folder.id == payload.parent_id).first()
        if not parent or parent.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Dossier parent introuvable.")
    folder.parent_id = payload.parent_id
    db.commit()
    db.refresh(folder)
    return folder.to_dict()


@router.patch("/{folder_id}")
def rename_folder(folder_id: str, payload: FolderRename, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    if folder.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom du dossier ne peut pas être vide.")
    folder.name = name
    db.commit()
    db.refresh(folder)
    return folder.to_dict()


@router.delete("/{folder_id}")
def delete_folder(folder_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    if folder.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    db.query(Video).filter(Video.folder_id == folder_id).update({Video.folder_id: None})
    # Subfolders move up to this folder's own parent instead of being deleted
    # too — deleting "2024" shouldn't silently wipe out "2024/Janvier".
    db.query(Folder).filter(Folder.parent_id == folder_id).update({Folder.parent_id: folder.parent_id})
    db.delete(folder)
    db.commit()
    return {"message": "Dossier supprimé (le contenu a été replacé hors dossier)."}
