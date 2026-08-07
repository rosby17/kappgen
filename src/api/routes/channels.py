from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from src.db.session import get_db
from src.db.models import Channel, Video
from src.models.project import ChannelCreate, ChannelUpdate, VideoStatus

router = APIRouter(prefix="/api/channels", tags=["channels"])

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
    channel = Channel(
        user_id=user_id,
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

@router.delete("/{channel_id}")
def delete_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete(channel)
    db.commit()
    return {"message": "Channel deleted successfully"}
