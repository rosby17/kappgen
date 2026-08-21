import uuid
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Channel, Video, User
from src.models.project import VideoCreate, VideoStatus
from src.utils.ffmpeg_runner import run_ffmpeg, validate_audio_file, get_audio_duration
from src.config import STORAGE_PATH, IZIVOICE_API_KEY
from src.pipeline.transcode import ensure_sd_variant
from src.pipeline.audio_extract import ensure_extracted_audio
from src.pipeline import youtube_publisher
from src.pipeline.youtube_metadata import generate_metadata, generate_thumbnail
from src.utils.auth import get_current_user
from src.utils.billing import user_can_render
from src.utils.rate_limit import rate_limit

router = APIRouter(prefix="/api/videos", tags=["videos"])
_limit_submit = rate_limit("video_submit", max_attempts=30, window_seconds=3600)

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
    voice_id: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _rl=Depends(_limit_submit),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    validate_channel_visual_source(channel)

    can_render, reason = user_can_render(db, current_user)
    if not can_render:
        raise HTTPException(status_code=402, detail=reason)

    created_videos = []
    uploads_dir = STORAGE_PATH / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    
    if input_type == "audio":
        if not audio_files:
            raise HTTPException(status_code=400, detail="Veuillez téléverser au moins un fichier audio.")
            
        for audio_file in audio_files:
            if not audio_file.filename:
                continue

            can_render, reason = user_can_render(db, current_user)
            if not can_render:
                if not created_videos:
                    raise HTTPException(status_code=402, detail=reason)
                break

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
                voice_id=voice_id.strip() if voice_id else channel.voice_id,
            )
            db.add(video)
            created_videos.append(video)
            if current_user.free_videos_used < current_user.free_video_quota_granted:
                current_user.free_videos_used += 1

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
            voice_id=voice_id.strip() if voice_id else channel.voice_id,
        )
        db.add(video)
        if current_user.free_videos_used < current_user.free_video_quota_granted:
            current_user.free_videos_used += 1
        db.commit()
        db.refresh(video)

        return [video.to_dict()]

@router.get("")
def list_all_videos(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    videos = (
        db.query(Video)
        .join(Channel, Video.channel_id == Channel.id)
        .filter(Channel.user_id == current_user.id)
        .order_by(Video.created_at.desc())
        .all()
    )
    return [v.to_dict() for v in videos]

def _get_owned_video(db: Session, video_id: str, current_user: User) -> Video:
    video = db.query(Video).join(Channel, Video.channel_id == Channel.id).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    return video


@router.get("/{video_id}")
def get_video_status(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_owned_video(db, video_id, current_user).to_dict()

@router.get("/{video_id}/download")
def download_video(video_id: str, quality: str = "hd", db: Session = Depends(get_db)):
    # Intentionally unauthenticated: reached via a plain download link/window.open,
    # which can't carry a custom Authorization header. video_id is an opaque UUID.
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = STORAGE_PATH / video.output_path
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    if quality != "sd":
        return FileResponse(source_path, media_type="video/mp4", filename=f"kappgen-{video_id}-hd.mp4")

    # Normally already pre-generated right after the render finished (see
    # queue_runner.py) so this resolves instantly; only actually transcodes
    # here as a fallback if that background step hasn't completed yet.
    cached_path = ensure_sd_variant(source_path)
    return FileResponse(cached_path, media_type="video/mp4", filename=f"kappgen-{video_id}-sd.mp4")

@router.get("/{video_id}/audio")
def download_video_audio(video_id: str, db: Session = Depends(get_db)):
    """Extracts and returns this video's soundtrack, for the 'reuse audio' flow.
    Intentionally unauthenticated: also used directly as an <audio src>, which
    can't carry a custom Authorization header."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = STORAGE_PATH / video.output_path
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    audio_path = ensure_extracted_audio(source_path)
    return FileResponse(audio_path, media_type="audio/mp4", filename=f"kappgen-{video_id}-audio.m4a")


def _publish_video_background(video_id: str) -> None:
    """Runs the actual YouTube upload (thumbnail + upload, can take minutes
    for a long video) on its own DB session/thread — same pattern as
    generate_and_queue_auto_video_background below. The route just kicks
    this off and returns immediately instead of holding the HTTP request
    (and the creator's publish modal) open for the whole upload."""
    from src.db.session import SessionLocal
    from src.worker.queue_runner import try_publish_to_youtube

    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return
        channel = video.channel
        video_path = STORAGE_PATH / video.output_path
        try_publish_to_youtube(db, channel, video, video_path)
    except Exception as exc:
        video = db.query(Video).filter(Video.id == video_id).first()
        if video:
            video.youtube_publish_error = str(exc)[:500]
            db.commit()
    finally:
        db.close()


@router.post("/{video_id}/youtube/publish")
def publish_video_to_youtube(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Kicks off publishing in the background and returns immediately — the
    actual YouTube upload can take minutes, and the creator shouldn't have to
    sit on a blocking modal for it. Progress is surfaced the same way as
    auto/scheduled publishes already are: via video.progress_stage, which the
    video card already watches (see the /youtube|miniature/ check in App.jsx)."""
    video = _get_owned_video(db, video_id, current_user)
    if video.status != VideoStatus.DONE.value or not video.output_path:
        raise HTTPException(status_code=409, detail="La vidéo doit être prête avant sa publication.")
    if video.youtube_video_id:
        return {
            "status": "already_published",
            "youtube_video_id": video.youtube_video_id,
            "youtube_url": f"https://youtu.be/{video.youtube_video_id}",
            "video": video.to_dict(),
        }

    channel = video.channel
    if not channel or not channel.youtube_refresh_token:
        auth_url = None
        if channel and youtube_publisher.is_configured():
            auth_url = youtube_publisher.build_auth_url(channel.id)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "youtube_auth_required",
                "message": "Connecte cette chaîne à YouTube avant de publier.",
                "auth_url": auth_url,
            },
        )

    video_path = STORAGE_PATH / video.output_path
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Le fichier vidéo n'existe plus sur le serveur.")

    video.youtube_publish_error = None
    video.progress_stage = "Préparation de la publication YouTube"
    db.commit()

    threading.Thread(target=_publish_video_background, args=(video_id,), daemon=True).start()
    return {"status": "publishing", "video": video.to_dict()}

class YoutubeMetadataUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None

@router.patch("/{video_id}/youtube-metadata")
def update_youtube_metadata(video_id: str, payload: YoutubeMetadataUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lets the creator review/edit the AI-proposed YouTube title (max 100
    chars, YouTube's own limit) and description before publishing."""
    video = _get_owned_video(db, video_id, current_user)
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Le titre ne peut pas être vide.")
        video.title = title[:100]
    if payload.description is not None:
        video.youtube_description = payload.description.strip()[:5000]
    db.commit()
    db.refresh(video)
    return video.to_dict()

@router.post("/{video_id}/youtube-metadata/regenerate")
def regenerate_youtube_metadata(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Asks the AI for a fresh title/description proposal, discarding whatever
    is currently set — used by the "Régénérer le titre" action."""
    video = _get_owned_video(db, video_id, current_user)
    channel = video.channel
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    metadata = generate_metadata(video, channel)
    video.title = metadata["title"][:100]
    video.youtube_description = metadata["description"][:5000]
    if metadata.get("thumbnail_text"):
        video.thumbnail_text = metadata["thumbnail_text"][:255]
    db.commit()
    db.refresh(video)
    return video.to_dict()

@router.post("/{video_id}/youtube-thumbnail/resync")
def resync_youtube_thumbnail(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Regenerates the thumbnail and re-uploads it to the already-published
    YouTube video — for videos published before thumbnail generation existed,
    or whose thumbnail upload silently failed (e.g. the channel wasn't phone-
    verified with YouTube yet at the time)."""
    video = _get_owned_video(db, video_id, current_user)
    if not video.youtube_video_id:
        raise HTTPException(status_code=409, detail="Cette vidéo n'est pas encore publiée sur YouTube.")
    channel = video.channel
    if not channel or not channel.youtube_refresh_token:
        raise HTTPException(status_code=409, detail="Chaîne non connectée à YouTube.")
    video_path = STORAGE_PATH / video.output_path if video.output_path else None
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=404, detail="Le fichier vidéo n'existe plus sur le serveur.")

    access_token = youtube_publisher.get_valid_access_token(channel)
    if not access_token:
        raise HTTPException(status_code=502, detail="Jeton YouTube expiré ou révoqué — reconnecte la chaîne.")

    thumbnail_path = generate_thumbnail(video_path, video_path.with_name("thumbnail.jpg"), video.thumbnail_text or video.title or channel.name, channel=channel)
    try:
        youtube_publisher.set_video_thumbnail(access_token, video.youtube_video_id, thumbnail_path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Échec de l'envoi de la miniature à YouTube : {str(exc)[:300]}")
    return {"status": "ok"}

@router.post("/{video_id}/thumbnail/regenerate")
def regenerate_video_thumbnail(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Regenerates this video's NicheCut card thumbnail (output_mp4's sibling
    thumbnail.jpg) — for videos stuck with a near-black one from before the
    fallback frame-grab was fixed to pick a representative frame instead of a
    fixed timestamp. Independent of YouTube publishing (unlike the resync
    route above), since this thumbnail is shown in the app regardless."""
    video = _get_owned_video(db, video_id, current_user)
    channel = video.channel
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    video_path = STORAGE_PATH / video.output_path if video.output_path else None
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=404, detail="Le fichier vidéo n'existe plus sur le serveur.")

    generate_thumbnail(video_path, video_path.with_name("thumbnail.jpg"), video.thumbnail_text or video.title or channel.name, channel=channel)
    return {"status": "ok"}

@router.get("/channel/{channel_id}")
def list_channel_videos(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    videos = db.query(Video).filter(Video.channel_id == channel_id).order_by(Video.created_at.desc()).all()
    return [v.to_dict() for v in videos]

@router.post("/{video_id}/retry")
def retry_video(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
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
    approved_for_publish: Optional[bool] = None

@router.patch("/{video_id}")
def update_video(video_id: str, payload: VideoUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Le titre ne peut pas être vide.")
        video.title = title

    if payload.approved_for_publish is not None:
        video.approved_for_publish = payload.approved_for_publish

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
def list_video_scenes(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
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
            # API_BASE already ends in /api on the frontend; returning /api/...
            # here produced /api/api/... and broke every Studio thumbnail.
            "image_url": f"/videos/{video_id}/scenes/{s['index']}/image",
            "image_version": int(Path(s["image_path"]).stat().st_mtime) if Path(s["image_path"]).exists() else 0,
        }
        for s in scenes
    ]


@router.get("/{video_id}/scenes/{scene_index}/image")
def get_scene_image(video_id: str, scene_index: int, db: Session = Depends(get_db)):
    # Intentionally unauthenticated: this URL is used directly as an <img src>
    # in the Studio scene list, and browsers don't attach a custom
    # Authorization header to plain image/media tag requests. video_id +
    # scene_index are opaque UUID/int pairs, not enumerable in practice.
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Swaps a single scene's source image and rebuilds just that scene's Ken
    Burns clip, then queues a lightweight reassembly (no TTS/pacing/other
    images touched) so a bad AI image doesn't require regenerating the video."""
    video = _get_owned_video(db, video_id, current_user)
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
def edit_scene_subtitle(video_id: str, scene_index: int, payload: SceneSubtitleUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Corrects one scene's caption text only — no TTS/STT call, audio untouched.
    Queued the same way as an image swap; the worker calls edit_scene_subtitle_text."""
    video = _get_owned_video(db, video_id, current_user)
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
def regenerate_scene_audio_endpoint(video_id: str, scene_index: int, payload: SceneSubtitleUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Re-records one scene's narration via TTS. Re-times that scene's clip and
    every later scene's position in the final video (their own clips are kept,
    only their timeline position moves) — queued for the worker to run
    regenerate_scene_audio, since it involves a real TTS call."""
    video = _get_owned_video(db, video_id, current_user)
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
def close_video_edit(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Frees the heavy per-scene images/clips once the user leaves the editor,
    rather than waiting for the retention window — output.mp4 is untouched."""
    video = _get_owned_video(db, video_id, current_user)
    if video.edit_assets_purged_at:
        return video.to_dict()

    from src.worker.queue_runner import purge_edit_assets
    purge_edit_assets(video)
    video.edit_assets_purged_at = datetime.utcnow()
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.delete("/{video_id}")
def delete_video(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    db.delete(video)
    db.commit()
    return {"message": "Video deleted successfully"}
