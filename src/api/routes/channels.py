from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from PIL import Image
import io
import random
import shutil
import time
import uuid
from src.db.session import get_db
from src.db.models import Channel, Video, User
from src.models.project import ChannelCreate, ChannelUpdate, VideoStatus
from src.config import STORAGE_PATH

router = APIRouter(prefix="/api/channels", tags=["channels"])

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ALLOWED_LIBRARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

async def save_valid_library_images(files: List[UploadFile], target_dir: Path):
    incoming_dir = target_dir.with_name(f".{target_dir.name}.incoming-{uuid.uuid4()}")
    incoming_dir.mkdir(parents=True, exist_ok=False)
    saved = 0
    rejected = 0
    try:
        for file in files:
            ext = Path(file.filename or "").suffix.lower()
            if ext not in ALLOWED_LIBRARY_EXTENSIONS:
                rejected += 1
                continue
            contents = await file.read()
            try:
                with Image.open(io.BytesIO(contents)) as image:
                    image.verify()
            except Exception:
                rejected += 1
                continue
            (incoming_dir / f"img_{saved:04d}{ext}").write_bytes(contents)
            saved += 1

        if saved == 0:
            raise HTTPException(status_code=400, detail="Aucune image valide dans les fichiers envoyés.")

        backup_dir = target_dir.with_name(f".{target_dir.name}.backup-{uuid.uuid4()}")
        if target_dir.exists():
            target_dir.rename(backup_dir)
        try:
            incoming_dir.rename(target_dir)
        except Exception:
            if backup_dir.exists() and not target_dir.exists():
                backup_dir.rename(target_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
        return saved, rejected
    finally:
        shutil.rmtree(incoming_dir, ignore_errors=True)

@router.get("/niches")
def list_niches(db: Session = Depends(get_db)):
    """
    Every distinct niche any channel has actually been saved with — the free-text
    niche field on channel creation grows this list organically instead of relying
    on a fixed pre-set catalogue, so it becomes a real shared niche database over time.
    """
    rows = db.query(Channel.niche).distinct().order_by(Channel.niche).all()
    return [r[0] for r in rows if r[0]]

@router.get("", response_model=List[Dict[str, Any]])
def list_channels(user_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Channel)
    if user_id:
        query = query.filter(Channel.user_id == user_id)
    channels = query.order_by(Channel.created_at.desc()).all()
    result = []
    for c in channels:
        data = c.to_dict()
        data["queued_count"] = db.query(Video).filter(Video.channel_id == c.id, Video.status == VideoStatus.QUEUED.value).count()
        data["rendering_count"] = db.query(Video).filter(Video.channel_id == c.id, Video.status == VideoStatus.RENDERING.value).count()
        data["done_count"] = db.query(Video).filter(Video.channel_id == c.id, Video.status == VideoStatus.DONE.value).count()
        data["failed_count"] = db.query(Video).filter(Video.channel_id == c.id, Video.status == VideoStatus.FAILED.value).count()
        result.append(data)
    return result

@router.post("", status_code=status.HTTP_201_CREATED)
def create_channel(payload: ChannelCreate, user_id: str = None, db: Session = Depends(get_db)):
    valid_user_id = None
    if user_id:
        user_exists = db.query(User).filter(User.id == user_id).first()
        if user_exists:
            valid_user_id = user_id

    channel = Channel(
        user_id=valid_user_id,
        name=payload.name,
        niche=payload.niche,
        subtitle_style=payload.subtitle_style.model_dump(),
        branding=payload.branding.model_dump(),
        music_preference=payload.music_preference.model_dump(),
        image_style=payload.image_style.model_dump(),
        effects_config=payload.effects_config.model_dump()
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.get("/{channel_id}")
def get_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    data = channel.to_dict()
    data["queued_count"] = db.query(Video).filter(Video.channel_id == channel.id, Video.status == VideoStatus.QUEUED.value).count()
    data["rendering_count"] = db.query(Video).filter(Video.channel_id == channel.id, Video.status == VideoStatus.RENDERING.value).count()
    data["done_count"] = db.query(Video).filter(Video.channel_id == channel.id, Video.status == VideoStatus.DONE.value).count()
    return data

@router.get("/{channel_id}/library-preview")
def get_channel_library_preview(channel_id: str, db: Session = Depends(get_db)):
    """Return a real random image from this channel's server-side library."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    library_dir = STORAGE_PATH / "channels" / channel.id / "library"
    images = [
        item for item in library_dir.iterdir()
        if item.is_file() and item.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS
    ] if library_dir.is_dir() else []
    if not images:
        raise HTTPException(status_code=404, detail="Aucune image disponible dans cette bibliothèque.")

    response = FileResponse(random.choice(images))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@router.put("/{channel_id}")
def update_channel(channel_id: str, payload: ChannelUpdate, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
        
    if payload.name is not None:
        channel.name = payload.name
    if payload.niche is not None:
        channel.niche = payload.niche
    if payload.subtitle_style is not None:
        channel.subtitle_style = payload.subtitle_style.model_dump()
    if payload.branding is not None:
        channel.branding = payload.branding.model_dump()
    if payload.music_preference is not None:
        channel.music_preference = payload.music_preference.model_dump()
    if payload.image_style is not None:
        channel.image_style = payload.image_style.model_dump()
    if payload.effects_config is not None:
        channel.effects_config = payload.effects_config.model_dump()

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/logo")
async def upload_channel_logo(channel_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (png, jpg, webp, gif, svg).")

    channel_dir = STORAGE_PATH / "channels" / channel.id
    channel_dir.mkdir(parents=True, exist_ok=True)

    # Remove any previous logo so switching formats doesn't leave stale files behind
    for old_logo in channel_dir.glob("logo.*"):
        old_logo.unlink(missing_ok=True)

    dest_file = channel_dir / f"logo{ext}"
    contents = await file.read()
    dest_file.write_bytes(contents)

    branding = dict(channel.branding or {})
    branding["logo_path"] = f"channels/{channel.id}/logo{ext}"
    channel.branding = branding

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/library-images/staging")
async def stage_channel_library_images(files: List[UploadFile] = File(...)):
    staging_root = STORAGE_PATH / "staging" / "channel-libraries"
    staging_root.mkdir(parents=True, exist_ok=True)

    # Opportunistic cleanup of abandoned uploads older than 24 hours.
    cutoff = time.time() - 86400
    for old_dir in staging_root.iterdir():
        if old_dir.is_dir() and old_dir.stat().st_mtime < cutoff:
            shutil.rmtree(old_dir, ignore_errors=True)

    token = str(uuid.uuid4())
    saved, rejected = await save_valid_library_images(files, staging_root / token)
    return {"staging_token": token, "library_image_count": saved, "rejected_count": rejected}

@router.post("/{channel_id}/library-images/staging")
def attach_staged_channel_library(
    channel_id: str,
    staging_token: str = Form(...),
    db: Session = Depends(get_db),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    try:
        safe_token = str(uuid.UUID(staging_token))
    except ValueError:
        raise HTTPException(status_code=400, detail="Import temporaire invalide.")

    staging_dir = STORAGE_PATH / "staging" / "channel-libraries" / safe_token
    if not staging_dir.is_dir() or not any(staging_dir.iterdir()):
        raise HTTPException(status_code=400, detail="Import temporaire introuvable ou expiré.")

    library_dir = STORAGE_PATH / "channels" / channel.id / "library"
    saved = len([item for item in staging_dir.iterdir() if item.is_file()])
    backup_dir = library_dir.with_name(f".{library_dir.name}.backup-{uuid.uuid4()}")
    library_dir.parent.mkdir(parents=True, exist_ok=True)
    if library_dir.exists():
        library_dir.rename(backup_dir)
    try:
        staging_dir.rename(library_dir)
    except Exception:
        if backup_dir.exists() and not library_dir.exists():
            backup_dir.rename(library_dir)
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)
    image_style = dict(channel.image_style or {})
    image_style["library_path"] = f"channels/{channel.id}/library"
    image_style["library_image_count"] = saved
    channel.image_style = image_style
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/library-images")
async def upload_channel_library_images(channel_id: str, files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    saved, rejected = await save_valid_library_images(
        files,
        STORAGE_PATH / "channels" / channel.id / "library",
    )

    image_style = dict(channel.image_style or {})
    image_style["library_path"] = f"channels/{channel.id}/library"
    image_style["library_image_count"] = saved
    image_style["library_rejected_count"] = rejected
    channel.image_style = image_style

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.delete("/{channel_id}")
def delete_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete(channel)
    db.commit()
    return {"message": "Channel deleted successfully"}
