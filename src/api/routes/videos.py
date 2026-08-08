import uuid
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Form, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Channel, Video
from src.models.project import VideoCreate, VideoStatus
from src.worker.queue_runner import process_single_queued_video
from src.utils.ffmpeg_runner import run_ffmpeg
from src.config import STORAGE_PATH

DOWNLOAD_RESOLUTIONS = {
    "sd": "854:480",
    "4k": "3840:2160",
}

router = APIRouter(prefix="/api/videos", tags=["videos"])

def clean_filename_title(filename: str) -> str:
    """Extracts clean video title from filename."""
    stem = Path(filename).stem
    clean = re.sub(r'[-_]+', ' ', stem).strip()
    return clean if clean else "Audio préenregistré"

@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_video_subject(
    background_tasks: BackgroundTasks,
    channel_id: str = Form(...),
    input_type: str = Form("text"),                    # "text" | "audio"
    script_text: Optional[str] = Form(""),
    audio_files: Optional[List[UploadFile]] = File(None),
    db: Session = Depends(get_db)
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
        
    created_videos = []
    uploads_dir = STORAGE_PATH / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    
    if input_type == "audio":
        if not audio_files:
            raise HTTPException(status_code=400, detail="Veuillez téléverser au moins un fichier audio.")
            
        for audio_file in audio_files:
            if not audio_file.filename:
                continue
            ext = Path(audio_file.filename).suffix or ".mp3"
            dest_file = uploads_dir / f"upload_{uuid.uuid4()}{ext}"
            
            contents = await audio_file.read()
            dest_file.write_bytes(contents)
            
            auto_title = clean_filename_title(audio_file.filename)
            
            video = Video(
                channel_id=channel.id,
                script_text=auto_title,
                input_type="audio",
                audio_input_path=str(dest_file),
                status=VideoStatus.QUEUED.value
            )
            db.add(video)
            created_videos.append(video)
            
        db.commit()
        for v in created_videos:
            db.refresh(v)
            background_tasks.add_task(process_single_queued_video)
            
        return [v.to_dict() for v in created_videos]
        
    else: # input_type == "text"
        if not (script_text and script_text.strip()):
            raise HTTPException(status_code=400, detail="Veuillez saisir un texte de script pour la génération TTS.")
            
        video = Video(
            channel_id=channel.id,
            script_text=script_text.strip(),
            input_type="text",
            audio_input_path=None,
            status=VideoStatus.QUEUED.value
        )
        db.add(video)
        db.commit()
        db.refresh(video)
        
        background_tasks.add_task(process_single_queued_video)
        return [video.to_dict()]

@router.get("")
def list_all_videos(db: Session = Depends(get_db)):
    videos = db.query(Video).order_by(Video.created_at.desc()).all()
    return [v.to_dict() for v in videos]

@router.get("/{video_id}")
def get_video_status(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video.to_dict()

@router.get("/{video_id}/download")
def download_video(video_id: str, quality: str = "hd", db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = STORAGE_PATH / video.output_path
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    if quality == "hd" or quality not in DOWNLOAD_RESOLUTIONS:
        return FileResponse(source_path, media_type="video/mp4", filename=f"nichecut-{video_id}-hd.mp4")

    target_res = DOWNLOAD_RESOLUTIONS[quality]
    cached_path = source_path.with_name(f"{source_path.stem}_{quality}.mp4")
    if not cached_path.exists():
        temp_path = cached_path.with_name(f".{cached_path.stem}.part.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(source_path),
            "-vf", f"scale={target_res}:flags=lanczos",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart",
            str(temp_path)
        ]
        try:
            run_ffmpeg(cmd)
            import os
            os.replace(temp_path, cached_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    return FileResponse(cached_path, media_type="video/mp4", filename=f"nichecut-{video_id}-{quality}.mp4")

@router.get("/channel/{channel_id}")
def list_channel_videos(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    videos = db.query(Video).filter(Video.channel_id == channel_id).order_by(Video.created_at.desc()).all()
    return [v.to_dict() for v in videos]

@router.post("/{video_id}/retry")
def retry_video(video_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    video.status = VideoStatus.QUEUED.value
    video.error_message = None
    db.commit()
    db.refresh(video)
    
    background_tasks.add_task(process_single_queued_video)
    return video.to_dict()

class VideoUpdate(BaseModel):
    title: Optional[str] = None
    folder_id: Optional[str] = None
    clear_folder: bool = False

@router.patch("/{video_id}")
def update_video(video_id: str, payload: VideoUpdate, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Le titre ne peut pas être vide.")
        video.script_text = title

    if payload.clear_folder:
        video.folder_id = None
    elif payload.folder_id is not None:
        from src.db.models import Folder
        folder = db.query(Folder).filter(Folder.id == payload.folder_id).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Dossier introuvable.")
        video.folder_id = folder.id

    db.commit()
    db.refresh(video)
    return video.to_dict()

@router.delete("/{video_id}")
def delete_video(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    db.delete(video)
    db.commit()
    return {"message": "Video deleted successfully"}
