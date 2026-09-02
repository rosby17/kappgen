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
from src.db.models import Channel, Video, User, CommunityLibraryFolder, CommunityLibraryImagePlacement
from src.models.project import VideoCreate, VideoStatus
from src.utils.ffmpeg_runner import run_ffmpeg, validate_audio_file, get_audio_duration
from src.config import STORAGE_PATH, IMAGE_UPLOAD_EXTENSIONS
from src.pipeline.transcode import ensure_sd_variant
from src.pipeline.audio_extract import ensure_extracted_audio
from src.pipeline import youtube_publisher
from src.pipeline.youtube_compliance import evaluate_youtube_compliance, evaluate_script_compliance, build_compliance_dossier
from src.pipeline.youtube_metadata import generate_metadata, generate_thumbnail
from src.utils.auth import get_current_user
from src.utils.billing import user_can_render, estimate_video_cost_credits
from src.utils.rate_limit import rate_limit

router = APIRouter(prefix="/api/videos", tags=["videos"])
_limit_submit = rate_limit("video_submit", max_attempts=30, window_seconds=3600)

LIBRARY_IMAGE_EXTENSIONS = IMAGE_UPLOAD_EXTENSIONS

# At the capped ~4.2Mbps render bitrate a 60min video lands around ~2GB —
# generous for long-form content while stopping runaway renders (and the
# very long CPU-bound renders that caused them) before they even start.
# Technical guardrail only. Commercial duration limits come from the user's
# plan; unrestricted/admin-credit access must not silently fall back to the
# former one-hour cap.
MAX_VIDEO_DURATION_SECONDS = 12 * 60 * 60

def validate_channel_visual_source(channel: Channel, db: Session) -> None:
    """Fail before TTS/queueing only when the channel's enabled visual
    sources (see resolve_enabled_image_sources — a creator can now combine
    any of AI generation, their own library, and the niche's community
    library, tried in that priority order at render time) would have
    nothing real to draw from at all.

    AI generation, once enabled and allowed by the account's tier, is
    trusted without a pre-check here: it runs through free-tier Hugging
    Face and already falls through gracefully per-image to whichever other
    sources are enabled (and finally to generic synthetic art) on its own
    failure — see fetch_or_generate_images — so there's nothing further to
    validate upfront. Without AI enabled, at least one of library/community
    must actually have real images now, or queuing is blocked with a clear
    message instead of silently rendering placeholder-only art."""
    from src.pipeline.images import resolve_enabled_image_sources
    image_style = channel.image_style or {}
    enabled = resolve_enabled_image_sources(image_style)

    if "ai_generated" in enabled:
        # Scene images (unlike thumbnails, which can use a paid
        # reference-image-conditioned provider) are generated exclusively
        # through Hugging Face's free-tier FLUX.1-schnell, with a non-billed
        # local-library fallback if that fails (see images.py's
        # fetch_or_generate_images / _generate_with_huggingface_flux —
        # `log_usage(..., 0.0, ...)`, Izivoice's paid image path is never
        # called for this). user_ai_images_enabled is a deliberate tier gate
        # (product decision), not a cost-recovery one — the only real check
        # AI generation needs before queuing.
        from src.utils.billing import user_ai_images_enabled
        owner = channel.user
        if owner and not user_ai_images_enabled(db, owner):
            raise HTTPException(
                status_code=403,
                detail="Les fonctionnalités IA ne sont pas incluses dans ton abonnement actuel. Passe à un palier supérieur pour les débloquer, ou choisis une bibliothèque d’images à la place.",
            )
        return

    has_real_source = False
    if "library" in enabled:
        library_path = str(image_style.get("library_path") or "")
        expected_prefix = f"channels/{channel.id}/library"
        library_dir = (STORAGE_PATH / library_path).resolve() if library_path else None
        storage_root = STORAGE_PATH.resolve()
        safe = library_dir is not None and (library_dir == storage_root or storage_root in library_dir.parents)
        has_real_source = has_real_source or (safe and library_path == expected_prefix and library_dir.is_dir() and any(
            item.is_file() and item.suffix.lower() in LIBRARY_IMAGE_EXTENSIONS
            for item in library_dir.iterdir()
        ))
    if "community" in enabled:
        has_approved_folder = db.query(CommunityLibraryFolder).filter(
            CommunityLibraryFolder.status == "approved",
            CommunityLibraryFolder.niche.ilike(channel.niche or ""),
        ).first() is not None
        if not has_approved_folder:
            has_approved_folder = db.query(CommunityLibraryImagePlacement).join(
                CommunityLibraryFolder,
                CommunityLibraryFolder.channel_id == CommunityLibraryImagePlacement.channel_id,
            ).filter(
                CommunityLibraryFolder.status == "approved",
                CommunityLibraryImagePlacement.niche.ilike(channel.niche or ""),
            ).first() is not None
        has_real_source = has_real_source or has_approved_folder
    if not has_real_source:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Aucune des sources visuelles activées pour « {channel.name} » n’a d’images disponibles pour l’instant. "
                "Importe une bibliothèque d’images, active la génération IA, ou attends qu’une bibliothèque collaborative existe pour cette niche."
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
    audio_rights_confirmed: bool = Form(False),
    audio_source_type: Optional[str] = Form(None),
    force_script_render: bool = Form(False),
    voice_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _rl=Depends(_limit_submit),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if not channel.is_active:
        raise HTTPException(status_code=409, detail="Cette chaîne est désactivée. Réactive-la pour générer de nouvelles vidéos.")
    if channel.content_type == "music":
        raise HTTPException(
            status_code=409,
            detail="Les chaînes de vidéo musicale n'ont pas de formulaire par vidéo — utilise \"Générer une vidéo\" à la place.",
        )
    # A creator-supplied title takes priority over both the filename-derived
    # one (audio) and the AI-generated one (queue_runner only fills video.title
    # when it's still empty) — set immediately at creation so a queued video
    # never sits showing "(sans titre)" while it waits its turn, and the AI
    # metadata step doesn't need to invent one.
    explicit_title = title.strip()[:100] if title and title.strip() else None
    # Nothing downstream ever rejected a near-empty script for a text-input
    # video — it would render "successfully" on almost nothing (a couple
    # seconds of near-silent audio), with the post-render AI metadata step
    # then improvising a title describing the missing content, since that's
    # all it had to go on (this is exactly what produced several
    # "Script manquant..." videos in production). 40 chars is generous
    # enough for no real narration to ever be rejected by mistake.
    if input_type == "text" and len((script_text or "").strip()) < 40:
        raise HTTPException(status_code=400, detail="Le script est trop court (ou vide) pour générer une vidéo.")
    validate_channel_visual_source(channel, db)

    if input_type == "audio" and transcribe_audio:
        from src.utils.billing import user_ai_transcription_enabled
        if not user_ai_transcription_enabled(db, current_user):
            raise HTTPException(
                status_code=403,
                detail="La transcription automatique (IA) n'est pas incluse dans ton abonnement actuel. Désactive-la pour utiliser le titre du fichier comme sous-titre, ou passe à un palier supérieur.",
            )
    if input_type == "audio" and not audio_rights_confirmed:
        raise HTTPException(
            status_code=400,
            detail="Confirmez que vous possédez les droits nécessaires sur l’audio et la voix avant de continuer.",
        )
    allowed_audio_sources = {"personal", "licensed", "cloned", "third_party", "music"}
    if input_type == "audio" and audio_source_type not in allowed_audio_sources:
        raise HTTPException(status_code=400, detail="Indiquez la provenance de l’audio importé.")

    can_render, reason = user_can_render(db, current_user)
    if not can_render:
        raise HTTPException(status_code=402, detail=reason)

    from src.utils.billing import user_max_video_duration_seconds
    tier_max_duration = user_max_video_duration_seconds(db, current_user)
    effective_max_duration = MAX_VIDEO_DURATION_SECONDS if tier_max_duration is None else min(MAX_VIDEO_DURATION_SECONDS, tier_max_duration)

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
            
            # Only meaningful for a single-file upload — with several files at
            # once every one would otherwise share the same explicit title,
            # so it's reserved for the filename-derived fallback in that case.
            auto_title = explicit_title if (explicit_title and len(audio_files) == 1) else clean_filename_title(audio_file.filename)

            try:
                estimated_duration = get_audio_duration(dest_file)
            except Exception:
                estimated_duration = None

            if estimated_duration and estimated_duration > effective_max_duration:
                dest_file.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"« {audio_file.filename} » dure {estimated_duration/60:.0f} min — la durée maximale de ton abonnement est de {effective_max_duration//60} min.",
                )

            estimated_cost = estimate_video_cost_credits(
                estimated_duration_seconds=estimated_duration or 0,
                transcribe_audio=transcribe_audio,
                image_style=channel.image_style,
                music_preference=channel.music_preference,
            )
            can_render, reason = user_can_render(db, current_user, estimated_cost)
            if not can_render:
                dest_file.unlink(missing_ok=True)
                if not created_videos:
                    raise HTTPException(status_code=402, detail=reason)
                break

            video = Video(
                channel_id=channel.id,
                title=auto_title,
                script_text=auto_title,
                input_type="audio",
                creation_source="audio",
                audio_input_path=str(dest_file),
                status=VideoStatus.QUEUED.value,
                estimated_duration_seconds=estimated_duration,
                transcribe_audio=transcribe_audio,
                audio_rights_confirmed=True,
                audio_source_type=audio_source_type,
                voice_id=voice_id.strip() if voice_id else channel.voice_id,
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

        # Full script preflight happens before credits are estimated or the
        # video enters the render queue. Red stops here; orange is preserved
        # on the row so it will require human approval before publication.
        history = (
            db.query(Video)
            .filter(Video.channel_id == channel.id)
            .order_by(Video.created_at.desc())
            .limit(30)
            .all()
        )
        script_report = evaluate_script_compliance(script_text, explicit_title or "", channel, history)
        if not script_report["can_render"] and not force_script_render:
            raise HTTPException(status_code=409, detail={
                "code": "script_compliance_blocked",
                "message": script_report["blockers"][0] if script_report["blockers"] else "Ce scénario doit être corrigé avant le rendu.",
                "report": script_report,
            })
            
        # Rough speech-rate estimate (~150 wpm) so the queue can prioritize
        # shorter jobs; corrected to the real duration once TTS runs.
        word_count = len(script_text.split())
        estimated_duration = max(3.0, word_count / 2.5)

        if estimated_duration > effective_max_duration:
            raise HTTPException(
                status_code=400,
                detail=f"Ce script produirait une vidéo d'environ {estimated_duration/60:.0f} min — la durée maximale de ton abonnement est de {effective_max_duration//60} min. Raccourcissez le texte ou divisez-le en plusieurs vidéos, ou passe à un palier supérieur.",
            )

        estimated_cost = estimate_video_cost_credits(
            script_char_count=len(script_text),
            estimated_duration_seconds=estimated_duration,
            transcribe_audio=transcribe_audio,
            image_style=channel.image_style,
            music_preference=channel.music_preference,
        )
        can_render, reason = user_can_render(db, current_user, estimated_cost)
        if not can_render:
            raise HTTPException(status_code=402, detail=reason)

        video = Video(
            channel_id=channel.id,
            title=explicit_title,
            script_text=script_text.strip(),
            input_type="text",
            creation_source="script",
            audio_input_path=None,
            status=VideoStatus.QUEUED.value,
            estimated_duration_seconds=estimated_duration,
            transcribe_audio=transcribe_audio,
            voice_id=voice_id.strip() if voice_id else channel.voice_id,
            approved_for_publish=False,
            script_compliance_overridden=bool(force_script_render and not script_report["can_render"]),
            script_compliance_overridden_at=datetime.utcnow() if force_script_render and not script_report["can_render"] else None,
            script_compliance_overridden_by=current_user.id if force_script_render and not script_report["can_render"] else None,
            youtube_compliance_report=script_report,
            youtube_compliance_history=[{
                "at": datetime.utcnow().isoformat(),
                "event": "script_preflight_completed",
                "details": {"score": script_report["score"], "status": script_report["status"]},
            }] + ([{
                "at": datetime.utcnow().isoformat(),
                "event": "script_render_forced",
                "details": {"user_id": current_user.id, "score": script_report["score"]},
            }] if force_script_render and not script_report["can_render"] else []),
        )
        db.add(video)
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


def _video_cost_transactions(db: Session, video: Video, user_id: str):
    """Debits made with a video_id (script generation, the base render fee)
    are matched directly; older/deep-pipeline debits (transcription, AI
    images/thumbnail, Izivoice voice/music) don't carry one, so they're
    matched by falling inside [started_at, finished_at] for this user
    instead — good enough since a creator rarely has two videos rendering in
    the exact same window. Shared by the creator-facing cost-recap endpoint
    and the admin video list's per-video cost column."""
    from src.db.models import CreditTransaction
    window_end = video.finished_at or datetime.utcnow()
    query = db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user_id,
        CreditTransaction.transaction_type == "debit",
    )
    if video.started_at:
        query = query.filter(
            (CreditTransaction.video_id == video.id) |
            ((CreditTransaction.video_id.is_(None)) & (CreditTransaction.created_at >= video.started_at) & (CreditTransaction.created_at <= window_end))
        )
    else:
        query = query.filter(CreditTransaction.video_id == video.id)
    return query.order_by(CreditTransaction.created_at.asc()).all()


@router.get("/{video_id}/cost-recap")
def get_video_cost_recap(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Itemized "what did this video cost" breakdown, shown right after a render finishes."""
    video = _get_owned_video(db, video_id, current_user)
    transactions = _video_cost_transactions(db, video, current_user.id)
    items = [{"description": t.description, "credits": -t.amount, "created_at": t.created_at.isoformat() if t.created_at else None} for t in transactions]
    return {"video_id": video.id, "total_credits": sum(item["credits"] for item in items), "items": items}

def _download_filename(video: Video, suffix: str) -> str:
    """Uses the video's real title (sanitized to a safe filename) instead of
    its opaque id, so a manually re-uploaded/re-posted video keeps its title
    visible in the downloaded file instead of forcing the creator to retype
    it from memory."""
    title = (video.title or "").strip()
    if not title:
        return f"kappgen-{video.id}-{suffix}.mp4"
    safe = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    safe = re.sub(r"\s+", " ", safe)[:150].strip()
    return f"{safe or video.id}-{suffix}.mp4"


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

    if not video.downloaded_at:
        video.downloaded_at = datetime.utcnow()
        db.commit()

    if quality != "sd":
        return FileResponse(source_path, media_type="video/mp4", filename=_download_filename(video, "hd"))

    # Normally already pre-generated right after the render finished (see
    # queue_runner.py) so this resolves instantly; only actually transcodes
    # here as a fallback if that background step hasn't completed yet.
    cached_path = ensure_sd_variant(source_path)
    return FileResponse(cached_path, media_type="video/mp4", filename=_download_filename(video, "sd"))


@router.get("/{video_id}/thumbnail/download")
def download_video_thumbnail(video_id: str, db: Session = Depends(get_db)):
    """Serves this video's thumbnail.jpg as a real download (Content-Disposition:
    attachment) — separate from the plain <img src> path the app itself uses to
    display it, since that one gets served inline and a browser can't reliably
    "Save As" a cross-origin <img> in one click. Meant for a creator who wants
    the exact same thumbnail KappGen generated to manually post alongside a
    video they publish outside the app. Intentionally unauthenticated, same
    reasoning as /download above (opaque video_id, no custom header needed)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")

    # output_path is a full R2 URL for videos stored there, not a local
    # STORAGE_PATH-relative path — thumbnail.jpg always stays local
    # regardless (see _finalize_output_storage), sitting next to wherever
    # this video's other local render artifacts live.
    if video.storage_backend == "r2":
        thumbnail_path = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "thumbnail.jpg"
    else:
        thumbnail_path = (STORAGE_PATH / video.output_path).with_name("thumbnail.jpg")
    if not thumbnail_path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    return FileResponse(thumbnail_path, media_type="image/jpeg", filename=_download_filename(video, "thumbnail").replace(".mp4", ".jpg"))


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


def _refresh_compliance_report(db: Session, video: Video) -> dict:
    previous = (
        db.query(Video)
        .filter(Video.channel_id == video.channel_id, Video.id != video.id)
        .order_by(Video.created_at.desc())
        .limit(30)
        .all()
    )
    report = evaluate_youtube_compliance(video, video.channel, previous)
    previous_report = video.youtube_compliance_report or {}
    video.youtube_compliance_report = report
    if previous_report.get("score") != report.get("score") or previous_report.get("status") != report.get("status"):
        _append_compliance_event(video, "check_completed", {"score": report["score"], "status": report["status"]})
    db.commit()
    return report


def _append_compliance_event(video: Video, event: str, details: Optional[dict] = None) -> None:
    history = list(video.youtube_compliance_history or [])
    history.append({"at": datetime.utcnow().isoformat(), "event": event, "details": details or {}})
    video.youtube_compliance_history = history[-50:]


@router.get("/{video_id}/youtube/compliance")
def get_youtube_compliance(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    return _refresh_compliance_report(db, video)


@router.get("/{video_id}/youtube/compliance/dossier")
def get_youtube_compliance_dossier(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    _refresh_compliance_report(db, video)
    db.refresh(video)
    return build_compliance_dossier(video, video.channel)


class CompliancePublishRequest(BaseModel):
    confirm_human_review: bool = False
    force_publish: bool = False


class ComplianceOverrideRequest(BaseModel):
    confirm_risk: bool = False


@router.post("/{video_id}/script-compliance/override")
def override_script_compliance(video_id: str, payload: ComplianceOverrideRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    report = video.youtube_compliance_report or {}
    if not payload.confirm_risk:
        raise HTTPException(status_code=400, detail="Vous devez confirmer le risque avant de forcer le montage.")
    if report.get("phase") not in {"script_preflight", "audio_preflight"} or report.get("can_render", True):
        raise HTTPException(status_code=409, detail="Cette vidéo n’est pas bloquée par le contrôle avant montage.")
    video.script_compliance_overridden = True
    video.script_compliance_overridden_at = datetime.utcnow()
    video.script_compliance_overridden_by = current_user.id
    video.publication_compliance_overridden = False
    video.status = VideoStatus.QUEUED.value
    video.error_message = None
    video.progress_stage = "Montage forcé par le créateur"
    video.progress_percent = 0
    _append_compliance_event(video, "script_render_forced", {"user_id": current_user.id, "score": report.get("score")})
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.post("/{video_id}/youtube/publish")
def publish_video_to_youtube(video_id: str, payload: Optional[CompliancePublishRequest] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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

    report = _refresh_compliance_report(db, video)
    force_publish = bool(payload and payload.force_publish)
    if not report["can_human_publish"] and not force_publish:
        _append_compliance_event(video, "publish_blocked", {"score": report["score"], "status": report["status"]})
        db.commit()
        raise HTTPException(status_code=409, detail={"code": "youtube_compliance_blocked", "message": "Le contrôle YouTube bloque cette publication.", "report": report})
    if (report["requires_human_review"] or force_publish) and not (payload and payload.confirm_human_review):
        _append_compliance_event(video, "human_review_required", {"score": report["score"]})
        db.commit()
        raise HTTPException(status_code=409, detail={"code": "youtube_compliance_review_required", "message": "Une validation humaine est requise.", "report": report})
    if force_publish:
        video.publication_compliance_overridden = True
        video.publication_compliance_overridden_at = datetime.utcnow()
        video.publication_compliance_overridden_by = current_user.id
        video.approved_for_publish = True
        video.youtube_compliance_reviewed_at = datetime.utcnow()
        video.youtube_compliance_reviewed_by = current_user.id
        _append_compliance_event(video, "publication_forced", {"user_id": current_user.id, "score": report["score"], "status": report["status"]})
    elif report["requires_human_review"]:
        video.approved_for_publish = True
        video.youtube_compliance_reviewed_at = datetime.utcnow()
        video.youtube_compliance_reviewed_by = current_user.id
        _append_compliance_event(video, "human_review_confirmed", {"user_id": current_user.id, "score": report["score"]})
    else:
        _append_compliance_event(video, "publish_authorized", {"score": report["score"], "mode": "manual"})

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
    # A force decision only covers the exact title/description the creator
    # reviewed. Editing either invalidates that decision and requires a fresh
    # acknowledgement against the newly calculated report.
    if payload.title is not None or payload.description is not None:
        video.publication_compliance_overridden = False
        video.publication_compliance_overridden_at = None
        video.publication_compliance_overridden_by = None
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
    channel = video.channel

    estimated_cost = estimate_video_cost_credits(
        script_char_count=len((video.script_text or "").strip()) if video.input_type == "text" else 0,
        estimated_duration_seconds=video.estimated_duration_seconds or video.duration_seconds or 0,
        transcribe_audio=bool(video.transcribe_audio),
        image_style=channel.image_style if channel else None,
        music_preference=channel.music_preference if channel else None,
    )
    can_render, reason = user_can_render(db, current_user, estimated_cost)
    if not can_render:
        raise HTTPException(status_code=402, detail=reason)

    # A failed automation attempt that never got a script at all (created by
    # _record_automation_failure, script_text left "") can't just be
    # re-queued for rendering as-is — that renders "successfully" on empty
    # content instead of actually retrying what failed, producing a
    # near-silent few-second video with a self-describing placeholder title
    # ("Script manquant...", from the post-render metadata step improvising
    # off an empty script) instead of a real one. Regenerate the script
    # first for this case; a plain manual/audio video always has real
    # content already and just needs re-queuing.
    if not (video.script_text or "").strip() and channel and channel.automation_mode == "auto" and channel.content_type != "music":
        from threading import Thread
        from src.worker.queue_runner import retry_auto_video_script_background
        # RENDERING, not QUEUED: the render lane's picker claims any 'queued'
        # row within ~2s of it appearing, but the script isn't written yet —
        # retry_auto_video_script_background() below runs in the background
        # and only flips this to QUEUED once it actually has one. Marking it
        # QUEUED here let the render lane grab it mid-regeneration and crash
        # on the "script is empty" guard in process_single_queued_video()
        # (seen in production logs, e.g. video 06856f5a — picked up and
        # failed twice, seconds after each retry, while the AI call was
        # still in flight or had already failed silently in the background).
        video.status = VideoStatus.RENDERING.value
        video.error_message = None
        video.progress_stage = "Régénération du script…"
        video.progress_percent = 0
        db.commit()
        Thread(target=retry_auto_video_script_background, args=(video.id,), daemon=True).start()
        db.refresh(video)
        return video.to_dict()

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
    extended_retention: Optional[bool] = None

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

    if payload.extended_retention is not None:
        # No credit/subscription gate yet — billed later (see Video.extended_retention).
        video.extended_retention = payload.extended_retention
        if payload.extended_retention:
            video.purged_at = None

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


# Cap on video.edit_history — a bounded undo stack, not a full audit log.
_EDIT_HISTORY_MAX = 20


def _push_edit_history(video: Video, entry: Dict[str, Any]) -> None:
    history = list(video.edit_history or [])
    history.append(entry)
    video.edit_history = history[-_EDIT_HISTORY_MAX:]


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

    # Back up the image being replaced before overwriting it, so /undo can
    # restore it — only when one already exists (nothing to revert to on a
    # scene's first-ever image).
    if image_path.exists():
        import time
        history_dir = video_dir / "source" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        backup_path = history_dir / f"scene_{scene_index}_{int(time.time() * 1000)}{image_path.suffix}"
        backup_path.write_bytes(image_path.read_bytes())
        _push_edit_history(video, {"type": "image", "scene_index": scene_index, "backup_path": str(backup_path)})

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

    previous_text = scene.get("text", "")
    if previous_text != text:
        _push_edit_history(video, {"type": "subtitle_text", "scene_index": scene_index, "previous_text": previous_text})

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
    can_render, reason = user_can_render(db, current_user, len(text))
    if not can_render:
        raise HTTPException(status_code=402, detail=reason)

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


class LogoPositionUpdate(BaseModel):
    logo_enabled: Optional[bool] = None
    logo_corner: Optional[str] = None
    logo_shape: Optional[str] = None
    logo_size_percent: Optional[float] = None
    logo_x_percent: Optional[float] = None
    logo_y_percent: Optional[float] = None


@router.patch("/{video_id}/logo")
def update_video_logo_position(video_id: str, payload: LogoPositionUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Repositions the channel logo from the Studio editor and reassembles this
    video with it. There's no per-video logo override in the data model — the
    logo is channel branding — so this writes straight to channel.branding,
    same as the wizard's logo controls, then reassembles just this video.
    Meaning: the new position previewed here also applies to every future
    video from this channel (the Studio UI says so), not only this one."""
    video = _get_owned_video(db, video_id, current_user)
    if video.status not in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
        raise HTTPException(status_code=409, detail="La vidéo est en cours de rendu ; réessayez une fois terminée.")

    channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    branding = dict(channel.branding or {})
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Aucune modification fournie.")

    previous = {k: branding.get(k) for k in updates}
    _push_edit_history(video, {"type": "logo", "previous": previous})

    branding.update(updates)
    channel.branding = branding

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = True
    video.pending_edit = None
    video.error_message = None
    video.progress_stage = "En attente du repositionnement du logo"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.post("/{video_id}/undo")
def undo_last_edit(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Pops the last reversible Studio edit (image swap, subtitle-text edit, or
    logo reposition — see Video.edit_history) and reapplies its previous
    state, then reassembles. Voice/audio regenerations aren't in the stack —
    they re-time the whole video and aren't cheaply reversible."""
    video = _get_owned_video(db, video_id, current_user)
    if video.status not in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
        raise HTTPException(status_code=409, detail="La vidéo est en cours de rendu ; réessayez une fois terminée.")

    history = list(video.edit_history or [])
    if not history:
        raise HTTPException(status_code=409, detail="Rien à annuler.")
    entry = history.pop()
    video.edit_history = history

    entry_type = entry.get("type")
    if entry_type == "image":
        import json
        scene_index = entry["scene_index"]
        backup_path = Path(entry["backup_path"])
        if not backup_path.exists():
            raise HTTPException(status_code=409, detail="L’image précédente n’est plus disponible (nettoyée entre-temps).")

        video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
        scenes_path = video_dir / "source" / "scenes.json"
        scenes = _load_scenes_manifest(video)
        scene = next((s for s in scenes if s["index"] == scene_index), None)
        if not scene:
            raise HTTPException(status_code=404, detail="Scene not found")

        image_path = Path(scene["image_path"])
        image_path.write_bytes(backup_path.read_bytes())
        backup_path.unlink(missing_ok=True)

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
        video.pending_edit = None
        video.progress_stage = "En attente de l’annulation (image)"

    elif entry_type == "subtitle_text":
        video.pending_edit = {"type": "subtitle_text", "scene_index": entry["scene_index"], "text": entry["previous_text"]}
        video.progress_stage = "En attente de l’annulation (sous-titre)"

    elif entry_type == "logo":
        channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        branding = dict(channel.branding or {})
        branding.update(entry.get("previous") or {})
        channel.branding = branding
        video.pending_edit = None
        video.progress_stage = "En attente de l’annulation (logo)"

    else:
        raise HTTPException(status_code=409, detail=f"Type de modification inconnu : {entry_type}")

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = True
    video.error_message = None
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
