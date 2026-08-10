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
from src.pipeline.audio_extract import ensure_extracted_audio

router = APIRouter(prefix="/api/videos", tags=["videos"])

LIBRARY_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

# At the capped ~4.2Mbps render bitrate a 60min video lands around ~2GB —
# generous for long-form content while stopping runaway renders (and the
# very long CPU-bound renders that caused them) before they even start.
MAX_VIDEO_DURATION_SECONDS = 60 * 60

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
    transcribe_audio: bool = Form(True),
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

            if estimated_duration and estimated_duration > MAX_VIDEO_DURATION_SECONDS:
                dest_file.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"« {audio_file.filename} » dure {estimated_duration/60:.0f} min — la durée maximale est de {MAX_VIDEO_DURATION_SECONDS//60} min.",
                )

            video = Video(
                channel_id=channel.id,
                script_text=auto_title,
                input_type="audio",
                audio_input_path=str(dest_file),
                status=VideoStatus.QUEUED.value,
                estimated_duration_seconds=estimated_duration,
                transcribe_audio=transcribe_audio,
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

        if estimated_duration > MAX_VIDEO_DURATION_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Ce script produirait une vidéo d'environ {estimated_duration/60:.0f} min — la durée maximale est de {MAX_VIDEO_DURATION_SECONDS//60} min. Raccourcissez le texte ou divisez-le en plusieurs vidéos.",
            )

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

@router.get("/{video_id}/audio")
def download_video_audio(video_id: str, db: Session = Depends(get_db)):
    """Extracts and returns this video's soundtrack, for the 'reuse audio' flow."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = STORAGE_PATH / video.output_path
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    audio_path = ensure_extracted_audio(source_path)
    return FileResponse(audio_path, media_type="audio/mp4", filename=f"nichecut-{video_id}-audio.m4a")

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

def _load_scenes_manifest(video: Video) -> List[Dict[str, Any]]:
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    scenes_path = video_dir / "source" / "scenes.json"
    if not scenes_path.exists():
        raise HTTPException(
            status_code=409,
            detail="Cette vidéo n’est plus éditable (fichiers sources supprimés ou vidéo antérieure à cette fonctionnalité).",
        )
    import json
    return json.loads(scenes_path.read_text(encoding="utf-8"))


@router.get("/{video_id}/scenes")
def list_video_scenes(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status != VideoStatus.DONE.value:
        raise HTTPException(status_code=409, detail="La vidéo n’est pas encore prête.")
    scenes = _load_scenes_manifest(video)
    return [
        {
            "index": s["index"],
            "start": s["start"],
            "end": s["end"],
            "duration": s["duration"],
            "text": s.get("text", ""),
            "editable_text": s.get("word_start_idx") is not None,
            "image_url": f"/api/videos/{video_id}/scenes/{s['index']}/image",
        }
        for s in scenes
    ]


@router.get("/{video_id}/scenes/{scene_index}/image")
def get_scene_image(video_id: str, scene_index: int, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    scenes = _load_scenes_manifest(video)
    scene = next((s for s in scenes if s["index"] == scene_index), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    image_path = Path(scene["image_path"])
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Scene image file not found on disk")
    return FileResponse(image_path)


@router.post("/{video_id}/scenes/{scene_index}/image")
async def replace_scene_image(
    video_id: str,
    scene_index: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Swaps a single scene's source image and rebuilds just that scene's Ken
    Burns clip, then queues a lightweight reassembly (no TTS/pacing/other
    images touched) so a bad AI image doesn't require regenerating the video."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
        raise HTTPException(status_code=409, detail="La vidéo est en cours de rendu ; réessayez une fois terminée.")

    if image.content_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise HTTPException(status_code=400, detail="Format d’image non supporté (PNG, JPEG ou WEBP attendu).")

    import json
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    scenes_path = video_dir / "source" / "scenes.json"
    scenes = _load_scenes_manifest(video)
    scene = next((s for s in scenes if s["index"] == scene_index), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    contents = await image.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Le fichier image est vide.")

    image_path = Path(scene["image_path"])
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(contents)

    channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    from src.pipeline.clip_builder import build_image_clip
    effects = channel.effects_config or {}
    build_image_clip(
        image_path=image_path,
        output_clip_path=Path(scene["clip_path"]),
        duration=scene["duration"],
        zoom_min_pct=effects.get("zoom_min_pct", 1.0),
        zoom_max_pct=effects.get("zoom_max_pct", 1.12),
    )

    scenes_path.write_text(json.dumps(scenes, indent=2), encoding="utf-8")

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = True
    video.error_message = None
    video.progress_stage = "En attente du réassemblage"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)
    return video.to_dict()


class SceneSubtitleUpdate(BaseModel):
    text: str


@router.patch("/{video_id}/scenes/{scene_index}/subtitle")
def edit_scene_subtitle(video_id: str, scene_index: int, payload: SceneSubtitleUpdate, db: Session = Depends(get_db)):
    """Corrects one scene's caption text only — no TTS/STT call, audio untouched.
    Queued the same way as an image swap; the worker calls edit_scene_subtitle_text."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
        raise HTTPException(status_code=409, detail="La vidéo est en cours de rendu ; réessayez une fois terminée.")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Le texte ne peut pas être vide.")

    scenes = _load_scenes_manifest(video)
    scene = next((s for s in scenes if s["index"] == scene_index), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    if scene.get("word_start_idx") is None:
        raise HTTPException(status_code=409, detail="Cette scène n’a pas de sous-titres modifiables (vidéo antérieure à cette fonctionnalité).")

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = True
    video.pending_edit = {"type": "subtitle_text", "scene_index": scene_index, "text": text}
    video.error_message = None
    video.progress_stage = "En attente de la correction des sous-titres"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.post("/{video_id}/scenes/{scene_index}/regenerate-audio")
def regenerate_scene_audio_endpoint(video_id: str, scene_index: int, payload: SceneSubtitleUpdate, db: Session = Depends(get_db)):
    """Re-records one scene's narration via TTS. Re-times that scene's clip and
    every later scene's position in the final video (their own clips are kept,
    only their timeline position moves) — queued for the worker to run
    regenerate_scene_audio, since it involves a real TTS call."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
        raise HTTPException(status_code=409, detail="La vidéo est en cours de rendu ; réessayez une fois terminée.")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Le texte ne peut pas être vide.")

    scenes = _load_scenes_manifest(video)
    scene = next((s for s in scenes if s["index"] == scene_index), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    if scene.get("word_start_idx") is None:
        raise HTTPException(status_code=409, detail="Cette scène n’a pas de plage audio modifiable (vidéo antérieure à cette fonctionnalité).")

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = True
    video.pending_edit = {"type": "audio", "scene_index": scene_index, "text": text}
    video.error_message = None
    video.progress_stage = "En attente de la régénération de la voix"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.post("/{video_id}/close-edit")
def close_video_edit(video_id: str, db: Session = Depends(get_db)):
    """Frees the heavy per-scene images/clips once the user leaves the editor,
    rather than waiting for the retention window — output.mp4 is untouched."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.edit_assets_purged_at:
        return video.to_dict()

    from src.worker.queue_runner import purge_edit_assets
    from datetime import datetime
    purge_edit_assets(video)
    video.edit_assets_purged_at = datetime.utcnow()
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
