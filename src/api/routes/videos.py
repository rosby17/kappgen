import uuid
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Channel, Video
from src.models.project import VideoCreate, VideoStatus
from src.utils.ffmpeg_runner import run_ffmpeg, validate_audio_file, get_audio_duration
from src.config import STORAGE_PATH, IZIVOICE_API_KEY
from src.pipeline.transcode import ensure_sd_variant

router = APIRouter(prefix="/api/videos", tags=["videos"])

LIBRARY_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

def validate_channel_visual_source(channel: Channel) -> None:
    """Fail before TTS/queueing when the selected visual source cannot work."""
    image_style = channel.image_style or {}
    source = image_style.get("source", "library")
    if source in {"ai_generated", "hybrid"} and not IZIVOICE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="La génération d’images IA n’est pas configurée sur le serveur.",
        )
    if source in {"library", "hybrid"}:
        library_path = str(image_style.get("library_path") or "")
        expected_prefix = f"channels/{channel.id}/library"
        library_dir = (STORAGE_PATH / library_path).resolve() if library_path else None
        storage_root = STORAGE_PATH.resolve()
        safe = library_dir is not None and (library_dir == storage_root or storage_root in library_dir.parents)
        has_images = safe and library_path == expected_prefix and library_dir.is_dir() and any(
            item.is_file() and item.suffix.lower() in LIBRARY_IMAGE_EXTENSIONS
            for item in library_dir.iterdir()
        )
        if not has_images:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"La bibliothèque d’images de la chaîne « {channel.name} » n’est pas enregistrée sur le serveur. "
                    "Modifiez cette chaîne et réimportez son dossier d’images avant de lancer la vidéo."
                ),
            )

def clean_filename_title(filename: str) -> str:
    """Extracts clean video title from filename."""
    stem = Path(filename).stem
    clean = re.sub(r'[-_]+', ' ', stem).strip()
    return clean if clean else "Audio préenregistré"

@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_video_subject(
    channel_id: str = Form(...),
    input_type: str = Form("text"),                    # "text" | "audio"
    script_text: Optional[str] = Form(""),
    audio_files: Optional[List[UploadFile]] = File(None),
    db: Session = Depends(get_db)
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    validate_channel_visual_source(channel)
        
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
            if not contents:
                raise HTTPException(status_code=400, detail=f"Le fichier {audio_file.filename} est vide.")
            dest_file.write_bytes(contents)
            try:
                validate_audio_file(dest_file)
            except ValueError as exc:
                dest_file.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"{audio_file.filename}: {exc}")
            
            auto_title = clean_filename_title(audio_file.filename)

            try:
                estimated_duration = get_audio_duration(dest_file)
            except Exception:
                estimated_duration = None

            video = Video(
                channel_id=channel.id,
                script_text=auto_title,
                input_type="audio",
                audio_input_path=str(dest_file),
                status=VideoStatus.QUEUED.value,
                estimated_duration_seconds=estimated_duration,
            )
            db.add(video)
            created_videos.append(video)
            
        db.commit()
        for v in created_videos:
            db.refresh(v)
            
        return [v.to_dict() for v in created_videos]
        
    else: # input_type == "text"
        if not (script_text and script_text.strip()):
            raise HTTPException(status_code=400, detail="Veuillez saisir un texte de script pour la génération TTS.")
            
        # Rough speech-rate estimate (~150 wpm) so the queue can prioritize
        # shorter jobs; corrected to the real duration once TTS runs.
        word_count = len(script_text.split())
        estimated_duration = max(3.0, word_count / 2.5)

        video = Video(
            channel_id=channel.id,
            script_text=script_text.strip(),
            input_type="text",
            audio_input_path=None,
            status=VideoStatus.QUEUED.value,
            estimated_duration_seconds=estimated_duration,
        )
        db.add(video)
        db.commit()
        db.refresh(video)
        
        return [video.to_dict()]

@router.get("")
def list_all_videos(user_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Video)
    if user_id:
        query = query.join(Channel, Video.channel_id == Channel.id).filter(Channel.user_id == user_id)
    videos = query.order_by(Video.created_at.desc()).all()
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

    if quality != "sd":
        return FileResponse(source_path, media_type="video/mp4", filename=f"nichecut-{video_id}-hd.mp4")

    # Normally already pre-generated right after the render finished (see
    # queue_runner.py) so this resolves instantly; only actually transcodes
    # here as a fallback if that background step hasn't completed yet.
    cached_path = ensure_sd_variant(source_path)
    return FileResponse(cached_path, media_type="video/mp4", filename=f"nichecut-{video_id}-sd.mp4")

@router.get("/channel/{channel_id}")
def list_channel_videos(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    videos = db.query(Video).filter(Video.channel_id == channel_id).order_by(Video.created_at.desc()).all()
    return [v.to_dict() for v in videos]

@router.post("/{video_id}/retry")
def retry_video(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    video.status = VideoStatus.QUEUED.value
    video.error_message = None
    video.progress_stage = "En attente du moteur de rendu"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)
    
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
