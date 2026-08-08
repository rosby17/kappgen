from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from src.db.session import get_db
from src.db.models import Channel, Video, User
from src.models.project import ChannelCreate, ChannelUpdate, VideoStatus
from src.config import STORAGE_PATH

router = APIRouter(prefix="/api/channels", tags=["channels"])

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ALLOWED_LIBRARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

@router.get("", response_model=List[Dict[str, Any]])
def list_channels(db: Session = Depends(get_db)):
    channels = db.query(Channel).order_by(Channel.created_at.desc()).all()
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

@router.post("/{channel_id}/library-images")
async def upload_channel_library_images(channel_id: str, files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    library_dir = STORAGE_PATH / "channels" / channel.id / "library"
    library_dir.mkdir(parents=True, exist_ok=True)

    valid_files = []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_LIBRARY_EXTENSIONS:
            continue
        contents = await file.read()
        if contents:
            valid_files.append((ext, contents))

    if not valid_files:
        raise HTTPException(status_code=400, detail="Aucune image valide dans les fichiers envoyés.")

    # Only replace the existing library after the new payload has been fully
    # received and validated. A bad upload can no longer erase good assets.
    for old_file in library_dir.iterdir():
        if old_file.is_file():
            old_file.unlink(missing_ok=True)

    for index, (ext, contents) in enumerate(valid_files):
        dest_file = library_dir / f"img_{index:04d}{ext}"
        dest_file.write_bytes(contents)

    saved = len(valid_files)

    image_style = dict(channel.image_style or {})
    image_style["source"] = "library"
    image_style["library_path"] = f"channels/{channel.id}/library"
    image_style["library_image_count"] = saved
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
