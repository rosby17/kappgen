import uuid
import re
import json
import shutil
import threading
import httpx
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Channel, Video, User
from src.models.project import VideoCreate, VideoStatus
from src.utils.ffmpeg_runner import run_ffmpeg, validate_audio_file, get_audio_duration
from src.config import STORAGE_PATH, IMAGE_UPLOAD_EXTENSIONS
from src.pipeline.transcode import ensure_sd_variant
from src.pipeline.audio_extract import ensure_extracted_audio
from src.pipeline import youtube_publisher
from src.pipeline.youtube_compliance import evaluate_youtube_compliance, evaluate_script_compliance, build_compliance_dossier
from src.pipeline.youtube_metadata import generate_metadata, generate_thumbnail, generate_contextual_thumbnail_headline
from src.utils.logger import logger
from src.utils.auth import get_current_user
from src.utils.billing import user_can_render, estimate_video_cost_credits, FOUR_K_EXPORT_CREDITS, debit_izivoice_usage_by_user_id
from src.utils.rate_limit import rate_limit

router = APIRouter(prefix="/api/videos", tags=["videos"])
_limit_submit = rate_limit("video_submit", max_attempts=30, window_seconds=3600)

LIBRARY_IMAGE_EXTENSIONS = IMAGE_UPLOAD_EXTENSIONS

def _video_is_remote(video: Video) -> bool:
    ref = str(video.output_path or "")
    return video.storage_backend in ("b2", "r2") or ref.startswith(("http://", "https://"))


def _ensure_local_thumbnail(video: Video) -> Path:
    """Ensure a thumbnail exists locally, rebuilding it from B2/R2 when the
    render itself has already been moved off the worker disk."""
    target = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "thumbnail.jpg"
    if not _video_is_remote(video):
        target = (STORAGE_PATH / video.output_path).with_name("thumbnail.jpg")
    if target.exists():
        return target
    if not _video_is_remote(video) or not video.output_path:
        raise FileNotFoundError("Thumbnail not found")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kappgen-thumbnail-source-") as tmp:
        source = Path(tmp) / "output.mp4"
        with httpx.stream("GET", str(video.output_path), timeout=600.0, follow_redirects=True) as response:
            response.raise_for_status()
            with source.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    handle.write(chunk)
        generate_thumbnail(source, target, video.thumbnail_text or video.title or "Nouvelle vidéo", channel=video.channel)
    if not target.exists():
        raise FileNotFoundError("Thumbnail not found")
    return target

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
    trusted without a pre-check here. The community source is trusted too:
    it means both curated creator media *and* KappGen's Pexels stock search,
    which discovers free visuals per scene at render time. A niche therefore
    does not need a pre-existing community upload to launch."""
    from src.pipeline.images import resolve_enabled_image_sources
    image_style = channel.image_style or {}
    media_mode = image_style.get("media_mode", "images")
    enabled = resolve_enabled_image_sources(image_style)

    # Mixed/video montage can source its motion footage from Pexels even when
    # the creator has not uploaded B-roll or explicitly enabled the public
    # image library. The montage mode itself is the creator's request for
    # stock video; do not reject a valid configuration before the worker can
    # perform that search.
    if media_mode in ("mixed", "videos"):
        from src.config import PEXELS_API_KEY
        if PEXELS_API_KEY:
            return

    if "ai_generated" in enabled and media_mode != "videos":
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

    # "Bibliothèque communautaire" is not only an existing database folder.
    # During assembly it also activates Pexels image/video search from each
    # scene's content (or a safe visual fallback if a stock result is absent).
    # Rejecting the launch merely because no contributor had uploaded to this
    # exact niche contradicted the UI's promise of free ready-to-use media.
    if "community" in enabled:
        return

    has_real_source = False
    if media_mode != "videos" and "library" in enabled:
        library_path = str(image_style.get("library_path") or "")
        expected_prefix = f"channels/{channel.id}/library"
        library_dir = (STORAGE_PATH / library_path).resolve() if library_path else None
        storage_root = STORAGE_PATH.resolve()
        safe = library_dir is not None and (library_dir == storage_root or storage_root in library_dir.parents)
        has_real_source = has_real_source or (safe and library_path == expected_prefix and library_dir.is_dir() and any(
            item.is_file() and item.suffix.lower() in LIBRARY_IMAGE_EXTENSIONS
            for item in library_dir.iterdir()
        ))
    if media_mode == "videos":
        broll_path = str(image_style.get("broll_path") or "")
        expected_prefix = f"channels/{channel.id}/broll"
        broll_dir = (STORAGE_PATH / broll_path).resolve() if broll_path else None
        root = STORAGE_PATH.resolve()
        safe = broll_dir is not None and root in broll_dir.parents
        has_real_source = safe and broll_path == expected_prefix and broll_dir.is_dir() and any(
            item.is_file() and item.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"} for item in broll_dir.iterdir()
        )
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

        # Without an explicit title, this row used to sit in "Mes Vidéos"
        # showing its own raw script_text as a fallback (queue_runner only
        # overwrites video.title once the worker actually picks the job up,
        # and even then only if it's still falsy — so a video that waits any
        # length of time in the queue had nothing better to show). The full
        # script is already known here, so there's no reason to wait: generate
        # the real title (and description/thumbnail_text, cached for reuse by
        # generate_metadata's reuse_existing path later — no second LLM call)
        # right now, before the row is even created.
        quick_title, quick_description, quick_thumbnail_text = explicit_title, None, None
        if not explicit_title:
            try:
                stub = SimpleNamespace(script_text=script_text.strip(), duration_seconds=None, id=None)
                meta = generate_metadata(stub, channel)
                quick_title = meta["title"]
                quick_description = meta["description"]
                quick_thumbnail_text = meta["thumbnail_text"]
            except Exception as exc:
                logger.warning(f"Could not generate an early title for a new text video on channel {channel.id}: {exc}")

        video = Video(
            channel_id=channel.id,
            title=quick_title,
            youtube_description=quick_description,
            thumbnail_text=quick_thumbnail_text,
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


ALLOWED_FACECAM_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


def _concat_facecam_rushes(parts: List[Path], dest: Path) -> None:
    """Joins multiple raw rush files into one source video before the edit
    pipeline runs. Creators rarely hand over one continuous take — a
    recording session is usually split across several files (recorder
    stopped/restarted, multiple angles exported separately, etc.), given
    to us in the order they were actually recorded (the creator controls
    that order client-side before submitting)."""
    if len(parts) == 1:
        shutil.move(str(parts[0]), str(dest))
        return
    concat_list = dest.with_suffix(".concat.txt")
    concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in parts))
    try:
        run_ffmpeg(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(dest)],
            timeout=300,
        )
    except Exception:
        # Stream-copy concat only works when every rush shares the same
        # codec/resolution/fps. Rushes from different recording sessions or
        # apps often don't — re-encode instead, which tolerates mismatches.
        inputs: List[str] = []
        filter_inputs = []
        for i, p in enumerate(parts):
            inputs += ["-i", str(p)]
            filter_inputs.append(f"[{i}:v:0][{i}:a:0]")
        filter_complex = "".join(filter_inputs) + f"concat=n={len(parts)}:v=1:a=1[outv][outa]"
        run_ffmpeg(
            ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex,
             "-map", "[outv]", "-map", "[outa]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac",
             str(dest)],
            timeout=900,
        )
    finally:
        concat_list.unlink(missing_ok=True)
        for p in parts:
            p.unlink(missing_ok=True)


@router.post("/facecam/upload")
async def submit_facecam_video(
    channel_id: str = Form(...),
    title: Optional[str] = Form(None),
    raw_files: Optional[List[UploadFile]] = File(None),
    cloud_link: Optional[str] = Form(None),
    editing_settings: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _rl=Depends(_limit_submit),
):
    """Entry point for the Facecam product: upload one or more raw
    talking-head rush files (direct upload, or a Drive/Dropbox share link
    downloaded server-side) and queue it for facecam_editor.py's auto-edit
    pipeline (silence/mistake cuts, verification, b-roll, motion-graphic
    cards). Multiple rush files are concatenated, in the order given, into
    a single source before editing starts.
    """
    raw_files = [f for f in (raw_files or []) if f and f.filename]
    from src.pipeline.facecam_project import FacecamSettings
    try:
        parsed_settings = FacecamSettings.model_validate_json(editing_settings).model_dump() if editing_settings else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Réglages de montage invalides.")
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if not channel.is_active:
        raise HTTPException(status_code=409, detail="Cette chaîne est désactivée. Réactive-la pour générer de nouvelles vidéos.")
    if channel.content_type != "facecam":
        raise HTTPException(status_code=422, detail="Choisis une chaîne Facecam pour ce montage.")
    if not raw_files and not cloud_link:
        raise HTTPException(status_code=400, detail="Fournis un ou plusieurs fichiers vidéo, ou un lien cloud (Drive/Dropbox).")

    can_render, reason = user_can_render(db, current_user)
    if not can_render:
        raise HTTPException(status_code=402, detail=reason)

    uploads_dir = STORAGE_PATH / "uploads" / "facecam"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    if raw_files:
        part_paths: List[Path] = []
        try:
            for rf in raw_files:
                ext = Path(rf.filename).suffix.lower() or ".mp4"
                if ext not in ALLOWED_FACECAM_VIDEO_EXTENSIONS:
                    raise HTTPException(status_code=400, detail=f"Format non supporté ({ext}). Utilise MP4, MOV, MKV ou WEBM.")
                part_path = uploads_dir / f"part_{uuid.uuid4()}{ext}"
                contents = await rf.read()
                if not contents:
                    raise HTTPException(status_code=400, detail=f"Le fichier « {rf.filename} » est vide.")
                part_path.write_bytes(contents)
                part_paths.append(part_path)
            first_ext = part_paths[0].suffix
            dest_file = uploads_dir / f"upload_{uuid.uuid4()}{first_ext if len(part_paths) == 1 else '.mp4'}"
            _concat_facecam_rushes(part_paths, dest_file)
        except HTTPException:
            for p in part_paths:
                p.unlink(missing_ok=True)
            raise
        except Exception as exc:
            for p in part_paths:
                p.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Impossible d'assembler les fichiers envoyés : {exc}")
    else:
        # v1: a pasted share link, not a full per-provider OAuth folder sync
        # (a much bigger integration, deferred until the need is confirmed).
        # Drive/Dropbox share links both redirect to the real file when a
        # direct-download query param is added, which httpx follows.
        link = cloud_link.strip()
        direct_link = link
        if "drive.google.com" in link:
            match = re.search(r"/d/([\w-]+)", link)
            if match:
                direct_link = f"https://drive.google.com/uc?export=download&id={match.group(1)}"
        elif "dropbox.com" in link:
            direct_link = link.replace("?dl=0", "?dl=1")
        dest_file = uploads_dir / f"upload_{uuid.uuid4()}.mp4"
        try:
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                with client.stream("GET", direct_link) as resp:
                    resp.raise_for_status()
                    with open(dest_file, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
        except Exception as exc:
            dest_file.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Téléchargement du lien cloud impossible : {exc}")
        if dest_file.stat().st_size == 0:
            dest_file.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Le lien cloud n'a renvoyé aucun contenu (vérifie qu'il est bien public/partagé).")

    try:
        estimated_duration = get_audio_duration(dest_file)
    except Exception:
        estimated_duration = None

    if estimated_duration and estimated_duration > MAX_VIDEO_DURATION_SECONDS:
        dest_file.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Cette vidéo dure {estimated_duration/60:.0f} min — la durée maximale est de {MAX_VIDEO_DURATION_SECONDS//60} min.",
        )

    # dest_file.name is a random upload_<uuid>.ext on disk, not what the
    # client actually named their file — fall back to the first rush's
    # filename (or the cloud link's basename) so an untitled upload still
    # gets a real name.
    fallback_name = raw_files[0].filename if raw_files else dest_file.name
    video = Video(
        channel_id=channel.id,
        title=(title.strip()[:100] if title and title.strip() else None) or clean_filename_title(fallback_name),
        input_type="facecam",
        facecam_settings=parsed_settings,
        creation_source="facecam",
        raw_asset_path=str(dest_file.relative_to(STORAGE_PATH)),
        status=VideoStatus.QUEUED.value,
        estimated_duration_seconds=estimated_duration,
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.get("")
def list_all_videos(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    videos = (
        db.query(Video)
        .join(Channel, Video.channel_id == Channel.id)
        .filter(Channel.user_id == current_user.id)
        .order_by(Video.created_at.desc())
        .all()
    )
    _backfill_trust_scores(db, videos)
    return [v.to_dict() for v in videos]

def _get_owned_video(db: Session, video_id: str, current_user: User) -> Video:
    video = db.query(Video).join(Channel, Video.channel_id == Channel.id).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    # Admin video operations (the admin dashboard's kebab menu) intentionally
    # span every creator's channel; regular users remain restricted to their
    # own channels.
    if video.channel.user_id != current_user.id and not getattr(current_user, "is_admin", False):
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


# Raw CreditTransaction.description strings (e.g. "voiceover_tts (46428 cr.
# Izivoice x1.0)") are written for admin bookkeeping — provider name,
# multiplier, real cost. A creator doesn't need to know it went through
# Izivoice at x1.0, just what it paid for; matched by prefix and swapped for
# a plain label here, admin's own view (which reuses the same
# _video_cost_transactions helper) is untouched.
# A creator was able to regenerate the same video's AI thumbnail unlimited
# times (one account ran up 7 regenerations, 14 000 cr., on a single video)
# before this cap existed — see regenerate_video_thumbnail below.
MAX_THUMBNAIL_REGENERATIONS = 5

_CLIENT_COST_LABELS = [
    (re.compile(r"^ai_thumbnail_generation\b"), "Miniature générée par IA"),
    (re.compile(r"^voiceover_tts\b"), "Voix off (synthèse vocale)"),
    (re.compile(r"^transcription_stt\b"), "Transcription (sous-titres)"),
    (re.compile(r"^ai_music_generation\b"), "Musique générée par IA"),
    (re.compile(r"^music_video_generation\b"), "Génération de la vidéo musicale"),
    (re.compile(r"^stock_media\b"), "Image & vidéos d'illustration"),
    (re.compile(r"^Génération auto de script\b"), "Script généré automatiquement"),
    (re.compile(r"^Frais forfaitaire vidéo\b"), "Frais de montage vidéo"),
    (re.compile(r"^Conservation vidéo\b"), "Conservation prolongée"),
]


def _client_cost_label(description: str) -> str:
    for pattern, label in _CLIENT_COST_LABELS:
        if pattern.match(description or ""):
            return label
    return description or "Autre"


@router.get("/{video_id}/cost-recap")
def get_video_cost_recap(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Itemized "what did this video cost" breakdown, shown right after a render finishes.

    Grouped by label rather than one row per CreditTransaction: a long video
    can bill 50+ individual 100-credit Pexels assets, each its own debit row
    for the admin ledger's sake — surfaced to the creator as-is, that was 50+
    near-identical lines for what is conceptually one cost ("stock footage").
    Order-preserving (first-seen position) so the recap still roughly reads
    top-to-bottom in the order costs were actually incurred during the render."""
    video = _get_owned_video(db, video_id, current_user)
    transactions = _video_cost_transactions(db, video, current_user.id)
    grouped: Dict[str, Dict[str, Any]] = {}
    for t in transactions:
        label = _client_cost_label(t.description)
        entry = grouped.setdefault(label, {"description": label, "credits": 0, "count": 0})
        entry["credits"] += -t.amount
        entry["count"] += 1
    # Every possible cost category is always shown, at 0 credits when this
    # particular video never used it, instead of only listing whichever ones
    # happened to fire — a music/audio video showing just one bare "Frais de
    # rendu" line looked like a different, opaque pricing model instead of
    # the same one as a narration video that simply skipped voix off,
    # transcription, thumbnail, etc. Any label a description didn't match
    # (an unrecognized/legacy debit description) still appears too, appended
    # after the fixed categories in first-seen order.
    all_labels = [label for _, label in _CLIENT_COST_LABELS]
    for label in grouped:
        if label not in all_labels:
            all_labels.append(label)
    items = [
        {
            **(entry := grouped.get(label, {"description": label, "credits": 0, "count": 0})),
            "description": f'{entry["description"]} × {entry["count"]}' if entry["count"] > 1 else entry["description"],
        }
        for label in all_labels
    ]
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

    if quality not in {"hd", "sd", "4k"}:
        raise HTTPException(status_code=400, detail="Qualité invalide. Choisis HD, SD ou 4K.")

    # B2/R2 outputs are intentionally public and no longer exist on the
    # worker's local disk after finalization. Redirect instead of prefixing
    # STORAGE_PATH to an HTTPS URL (which caused "file not found on disk").
    is_remote = video.storage_backend in ("b2", "r2") or str(video.output_path).startswith(("http://", "https://"))
    if is_remote and quality != "4k":
        if not video.downloaded_at:
            video.downloaded_at = datetime.utcnow()
            db.commit()
        filename = _download_filename(video, quality)
        def remote_stream():
            with httpx.stream("GET", video.output_path, timeout=300.0, follow_redirects=True) as response:
                response.raise_for_status()
                yield from response.iter_bytes()
        return StreamingResponse(
            remote_stream(),
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    source_path = STORAGE_PATH / video.output_path if not is_remote else None
    if quality == "4k":
        if not video.channel or not video.channel.user_id:
            raise HTTPException(status_code=409, detail="Impossible de facturer cet export 4K.")
        if not debit_izivoice_usage_by_user_id(video.channel.user_id, FOUR_K_EXPORT_CREDITS, "video_4k_export", video_id=video.id):
            raise HTTPException(status_code=402, detail=f"Crédits insuffisants pour l’export 4K ({FOUR_K_EXPORT_CREDITS:,} crédits).")
        target = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "output-4k.mp4"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="kappgen-4k-") as tmp:
                if is_remote:
                    source_path = Path(tmp) / "source.mp4"
                    with httpx.stream("GET", video.output_path, timeout=300.0, follow_redirects=True) as response:
                        response.raise_for_status()
                        with source_path.open("wb") as handle:
                            for chunk in response.iter_bytes():
                                handle.write(chunk)
                if not source_path or not source_path.exists():
                    raise HTTPException(status_code=404, detail="Vidéo source introuvable pour l’export 4K.")
                run_ffmpeg(["ffmpeg", "-y", "-i", str(source_path), "-vf", "scale=3840:2160:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", "-movflags", "+faststart", str(target)])
        return FileResponse(target, media_type="video/mp4", filename=_download_filename(video, "4k"))

    source_path = source_path
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

    # output_path is a full B2 (or legacy R2) URL for videos stored there,
    # not a local STORAGE_PATH-relative path — thumbnail.jpg always stays
    # local regardless (see _finalize_output_storage), sitting next to
    # wherever this video's other local render artifacts live.
    try:
        thumbnail_path = _ensure_local_thumbnail(video)
    except Exception as exc:
        logger.warning("Could not recover thumbnail for video %s: %s", video_id, exc)
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    return FileResponse(thumbnail_path, media_type="image/jpeg", filename=_download_filename(video, "thumbnail").replace(".mp4", ".jpg"))


@router.get("/{video_id}/thumbnail")
def serve_video_thumbnail(video_id: str, db: Session = Depends(get_db)):
    """Serve an inline card thumbnail, repairing legacy renders that never
    produced one by extracting a representative frame from their MP4."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")
    video_path = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "output.mp4"
    try:
        thumbnail_path = _ensure_local_thumbnail(video)
    except Exception as exc:
        logger.warning("Could not recover thumbnail for video %s: %s", video_id, exc)
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(thumbnail_path, media_type="image/jpeg")


@router.get("/{video_id}/audio")
def download_video_audio(video_id: str, db: Session = Depends(get_db)):
    """Extracts and returns this video's soundtrack, for the 'reuse audio' flow.
    Intentionally unauthenticated: also used directly as an <audio src>, which
    can't carry a custom Authorization header."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.output_path:
        raise HTTPException(status_code=404, detail="Video not found")

    output_ref = str(video.output_path)
    if not _video_is_remote(video):
        source_path = STORAGE_PATH / output_ref
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="Video file not found on disk")
        audio_path = ensure_extracted_audio(source_path)
        return FileResponse(audio_path, media_type="audio/mp4", filename=f"kappgen-{video_id}-audio.m4a")

    # The render lives in B2/R2; download and extract only while the response
    # is being streamed, keeping storage details invisible to the client.
    def remote_audio_stream():
        with tempfile.TemporaryDirectory(prefix="kappgen-audio-source-") as tmp:
            source_path = Path(tmp) / "output.mp4"
            with httpx.stream("GET", output_ref, timeout=600.0, follow_redirects=True) as response:
                response.raise_for_status()
                with source_path.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        handle.write(chunk)
            audio_path = ensure_extracted_audio(source_path)
            with audio_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
    return StreamingResponse(remote_audio_stream(), media_type="audio/mp4", headers={"Content-Disposition": f'attachment; filename="kappgen-{video_id}-audio.m4a"'})


def _publish_video_background(video_id: str) -> None:
    """Runs the actual YouTube upload (thumbnail + upload, can take minutes
    for a long video) on its own DB session/thread — same pattern as
    generate_and_queue_auto_video_background below. The route just kicks
    this off and returns immediately instead of holding the HTTP request
    (and the creator's publish modal) open for the whole upload."""
    from src.db.session import SessionLocal
    from src.worker.queue_runner import try_publish_to_youtube

    db = SessionLocal()
    temp_dir = None
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return
        channel = video.channel
        output_ref = str(video.output_path or "")
        is_remote = video.storage_backend in ("b2", "r2") or output_ref.startswith(("http://", "https://"))
        if is_remote:
            # Finished renders are normally moved to B2/R2 to free the worker
            # disk. YouTube still needs a local seekable file, so materialize
            # the object only for the duration of this background upload.
            temp_dir = tempfile.TemporaryDirectory(prefix="kappgen-youtube-")
            video_path = Path(temp_dir.name) / "output.mp4"
            with httpx.stream("GET", output_ref, timeout=600.0, follow_redirects=True) as response:
                response.raise_for_status()
                with video_path.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        handle.write(chunk)
        else:
            video_path = STORAGE_PATH / output_ref
            if not video_path.exists():
                raise FileNotFoundError("Le fichier vidéo n'existe plus sur le serveur.")
        try_publish_to_youtube(db, channel, video, video_path)
    except Exception as exc:
        video = db.query(Video).filter(Video.id == video_id).first()
        if video:
            video.youtube_publish_error = str(exc)[:500]
            db.commit()
    finally:
        if temp_dir:
            temp_dir.cleanup()
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


_TRUST_SCORE_BACKFILL_MAX_PER_REQUEST = 3


def _backfill_trust_scores(db: Session, videos: List[Video]) -> None:
    """Give finished legacy videos the same automatic Trust Score as newly
    rendered videos. The video list is the first place creators return to,
    so it should never make them click merely to initialise an analysis.

    Capped to a handful per request — this reads and SHA-256-hashes every
    source visual asset on disk per video (see _visual_asset_hashes), which
    is expensive. Doing this for every legacy video with no v2 report on
    every single GET /videos call (unbounded, synchronous, inside a request
    holding a DB connection) exhausted the connection pool and hung the API
    the first time this shipped, since EVERY existing "done" video across
    every user lacked a v2 report at once. It still fully backfills over
    time — just a few videos per page load instead of everything at once."""
    missing = [video for video in videos if video.status == VideoStatus.DONE.value and (not video.youtube_compliance_report or (video.youtube_compliance_report or {}).get("version", 1) < 2)]
    if not missing:
        return
    for video in missing[:_TRUST_SCORE_BACKFILL_MAX_PER_REQUEST]:
        try:
            previous = (
                db.query(Video)
                .filter(Video.channel_id == video.channel_id, Video.id != video.id)
                .order_by(Video.created_at.desc())
                .limit(30)
                .all()
            )
            report = evaluate_youtube_compliance(video, video.channel, previous)
            video.youtube_compliance_report = report
            _append_compliance_event(video, "trust_score_backfilled", {"score": report["score"], "status": report["status"]})
        except Exception as e:
            logger.warning(f"Trust Score backfill failed for video {video.id}: {e}")
    db.commit()


@router.get("/{video_id}/youtube/compliance")
def get_youtube_compliance(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    return _refresh_compliance_report(db, video)


@router.get("/{video_id}/youtube/status")
def get_youtube_status(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cheap, read-only check the video card uses before deciding whether
    "Voir sur YouTube" should just open the link or route through the
    publish-review modal to offer a republish (see POST .../youtube/publish,
    which does the authoritative check + republish itself — this endpoint
    only decides which UI to show, never mutates anything)."""
    video = _get_owned_video(db, video_id, current_user)
    if not video.youtube_video_id:
        return {"published": False, "exists": False}
    channel = video.channel
    access_token = channel and youtube_publisher.get_valid_access_token(channel)
    exists = youtube_publisher.video_exists(access_token, video.youtube_video_id) if access_token else True
    return {"published": True, "exists": exists, "youtube_video_id": video.youtube_video_id}


@router.get("/{video_id}/youtube/compliance/dossier")
def get_youtube_compliance_dossier(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    _refresh_compliance_report(db, video)
    db.refresh(video)
    return build_compliance_dossier(video, video.channel)


class CompliancePublishRequest(BaseModel):
    confirm_human_review: bool = False
    force_publish: bool = False
    republish: bool = False


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

    channel = video.channel
    if video.youtube_video_id and not (payload and payload.republish):
        # Before refusing as "already published", confirm it's actually still
        # live — a creator who deleted the video on YouTube (or had it taken
        # down) has no other way to get it back online otherwise. Reuses the
        # exact same publish flow below with the video's existing title/
        # description/thumbnail, since try_publish_to_youtube already prefers
        # those over regenerating when they're already set.
        access_token = channel and youtube_publisher.get_valid_access_token(channel)
        still_live = youtube_publisher.video_exists(access_token, video.youtube_video_id) if access_token else True
        if still_live:
            return {
                "status": "already_published",
                "youtube_video_id": video.youtube_video_id,
                "youtube_url": f"https://youtu.be/{video.youtube_video_id}",
                "video": video.to_dict(),
            }
        _append_compliance_event(video, "youtube_video_missing_republishing", {"previous_youtube_video_id": video.youtube_video_id})
        video.youtube_video_id = None
        video.youtube_published_at = None
        db.commit()
    elif video.youtube_video_id and payload and payload.republish:
        _append_compliance_event(video, "youtube_republish_requested", {"previous_youtube_video_id": video.youtube_video_id})
        video.youtube_video_id = None
        video.youtube_published_at = None
        db.commit()

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

    output_ref = str(video.output_path or "")
    is_remote = video.storage_backend in ("b2", "r2") or output_ref.startswith(("http://", "https://"))
    video_path = STORAGE_PATH / output_ref if not is_remote else None
    if video_path is not None and not video_path.exists():
        raise HTTPException(status_code=404, detail="Le fichier vidéo n'existe plus sur le serveur.")

    report = _refresh_compliance_report(db, video)
    force_publish = bool(payload and payload.force_publish)
    if not report["can_human_publish"] and not force_publish:
        _append_compliance_event(video, "publish_blocked", {"score": report["score"], "status": report["status"]})
        db.commit()
        raise HTTPException(status_code=409, detail={"code": "youtube_compliance_blocked", "message": "Le contrôle YouTube bloque cette publication.", "report": report})
    # Orange (requires_human_review) no longer needs a separate confirmation
    # tick before publishing — the score and its breakdown are already shown
    # right there in the same modal, and re-confirming a video already
    # reviewed once was pure friction. Only a genuinely blocked (red) video
    # still needs an explicit force_publish choice, right above.
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
    access_token = youtube_publisher.get_valid_access_token(channel)
    if not access_token:
        raise HTTPException(status_code=502, detail="Jeton YouTube expiré ou révoqué — reconnecte la chaîne.")

    # Push whatever thumbnail is currently active (whatever the creator sees
    # on their card — possibly restored from history, or manually
    # regenerated) instead of generating yet another new one, which used to
    # silently diverge from what was actually visible in the app. Only
    # generate one from scratch for a legacy video that never got one.
    try:
        thumbnail_path = _ensure_local_thumbnail(video)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Impossible de récupérer la vidéo depuis le stockage : {str(exc)[:200]}")
    try:
        youtube_publisher.set_video_thumbnail(access_token, video.youtube_video_id, thumbnail_path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Échec de l'envoi de la miniature à YouTube : {str(exc)[:300]}")
    return {"status": "ok"}

def _regenerate_thumbnail_background(video_id: str) -> None:
    """Runs the actual (AI-backed) regeneration off the request thread — see
    regenerate_video_thumbnail below for why."""
    from src.db.session import SessionLocal
    db = SessionLocal()
    succeeded = False
    temp_dir = None
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return
        channel = video.channel
        output_ref = str(video.output_path or "")
        is_remote = video.storage_backend in ("b2", "r2") or output_ref.startswith(("http://", "https://"))
        if is_remote:
            temp_dir = tempfile.TemporaryDirectory(prefix="kappgen-thumbnail-")
            video_path = Path(temp_dir.name) / "output.mp4"
            with httpx.stream("GET", output_ref, timeout=600.0, follow_redirects=True) as response:
                response.raise_for_status()
                with video_path.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        handle.write(chunk)
            # Thumbnails and their history remain local even when the MP4 is
            # stored remotely, so the app can serve them without downloading
            # the full video on every card refresh.
            current = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "thumbnail.jpg"
            current.parent.mkdir(parents=True, exist_ok=True)
        else:
            video_path = STORAGE_PATH / output_ref if output_ref else None
            if not channel or not video_path or not video_path.exists():
                return
            current = video_path.with_name("thumbnail.jpg")
        if not channel or not video_path or not video_path.exists():
            return
        if current.exists():
            history_dir = current.parent / "thumbnail_history"
            history_dir.mkdir(parents=True, exist_ok=True)
            archive = history_dir / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
            shutil.copy2(current, archive)
        # A manual thumbnail regeneration is explicitly a fresh creative
        # request. Re-read the actual script here instead of recycling the
        # previous caption (which may have been an old, title-derived draft).
        video.thumbnail_text = generate_contextual_thumbnail_headline(
            script=video.script_text or "",
            title=video.title or "",
            niche=(channel.niche or ""),
            draft=video.thumbnail_text or "",
        )
        db.commit()
        # strict=True whenever the channel actually has a reference style —
        # same "no generic placeholder" rule as the automatic pipeline (see
        # queue_runner.py): a manual regeneration request should fail
        # clearly, not silently swap one mediocre image for another.
        thumbnail_style = channel.thumbnail_style or {}
        strict = bool(thumbnail_style.get("reference_image_paths") or thumbnail_style.get("reference_image_path"))
        generate_thumbnail(video_path, current, video.thumbnail_text or video.title or channel.name, channel=channel, strict=strict)
        succeeded = True
    except Exception as e:
        logger.error(f"Thumbnail regeneration failed for video {video_id}: {e}")
    finally:
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                video.thumbnail_regenerating = False
                if succeeded:
                    # Bumps the frontend's cache-busting key so a refresh right
                    # after regenerating never reuses the old, now-stale image
                    # URL (see the field's own comment in db/models.py).
                    video.thumbnail_updated_at = datetime.utcnow()
                    video.thumbnail_error = None
                else:
                    video.thumbnail_error = (
                        "La miniature n'a pas pu être régénérée dans le style de la chaîne. Réessaie dans quelques minutes."
                    )
                db.commit()
        except Exception:
            pass
        db.close()
        if temp_dir:
            temp_dir.cleanup()


@router.post("/{video_id}/thumbnail/regenerate")
def regenerate_video_thumbnail(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Kicks off regeneration of this video's NicheCut card thumbnail
    (output_mp4's sibling thumbnail.jpg) — for videos stuck with a near-black
    one from before the fallback frame-grab was fixed to pick a representative
    frame instead of a fixed timestamp. Independent of YouTube publishing
    (unlike the resync route above), since this thumbnail is shown in the app
    regardless.

    Only starts the job and returns immediately — the AI background-image
    call this can trigger routinely takes well over a minute, longer than
    Cloudflare's edge proxy will hold an HTTP request open, which used to
    surface in the app as a bare "Failed to fetch" once the connection got
    cut mid-request even though the backend was still working. The frontend
    polls /{video_id}/thumbnail/regenerate/status instead of awaiting this
    response."""
    video = _get_owned_video(db, video_id, current_user)
    channel = video.channel
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    thumbnail_style = channel.thumbnail_style or {}
    configured_references = thumbnail_style.get("reference_image_paths") or ([] if not thumbnail_style.get("reference_image_path") else [thumbnail_style["reference_image_path"]])
    if not configured_references:
        raise HTTPException(
            status_code=409,
            detail="Aucune référence de style de miniature n'est configurée pour cette chaîne. Réimporte les références avant de régénérer.",
        )
    missing_references = [
        path for path in configured_references
        if not isinstance(path, str) or not (STORAGE_PATH / path).is_file()
    ]
    if missing_references:
        raise HTTPException(
            status_code=409,
            detail="Les fichiers de référence de miniature sont introuvables sur le serveur. Réimporte-les avant de régénérer.",
        )
    output_ref = str(video.output_path or "")
    is_remote = video.storage_backend in ("b2", "r2") or output_ref.startswith(("http://", "https://"))
    video_path = STORAGE_PATH / output_ref if output_ref and not is_remote else None
    if video_path is not None and not video_path.exists():
        raise HTTPException(status_code=404, detail="Le fichier vidéo n'existe plus sur le serveur.")
    # Each regeneration archives the thumbnail it's about to replace into
    # thumbnail_history/ (see _regenerate_thumbnail_background below) BEFORE
    # generating the new one — so the archive count IS the number of past
    # regenerations, independent of whether a given attempt was free (Hugging
    # Face) or paid (2000 cr., see THUMBNAIL_CREDITS below): a creator was
    # able to regenerate the same video's thumbnail 7 times (14 000 cr.)
    # before this cap existed.
    # Remote videos have no local output.mp4 path. Their thumbnails are kept
    # in the channel's local storage directory, just like the background
    # regeneration task below, so do not dereference video_path here.
    thumbnail_dir = (
        STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
        if is_remote
        else video_path.parent
    )
    history_dir = thumbnail_dir / "thumbnail_history"
    past_regenerations = len(list(history_dir.glob("*.jpg"))) if history_dir.exists() else 0
    if past_regenerations >= MAX_THUMBNAIL_REGENERATIONS:
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {MAX_THUMBNAIL_REGENERATIONS} régénérations de miniature atteinte pour cette vidéo.",
        )
    # A stuck flag from a server restart mid-regeneration (the background
    # thread is a daemon with no persistence of its own) must never block
    # every future attempt forever — past a generous timeout, treat it as
    # orphaned and let this request take over rather than 409ing endlessly.
    stale_cutoff = datetime.utcnow() - timedelta(minutes=5)
    if video.thumbnail_regenerating and (
        video.thumbnail_regenerating_started_at is None
        or video.thumbnail_regenerating_started_at > stale_cutoff
    ):
        raise HTTPException(status_code=409, detail="Une régénération est déjà en cours pour cette vidéo.")

    video.thumbnail_regenerating = True
    video.thumbnail_regenerating_started_at = datetime.utcnow()
    db.commit()
    threading.Thread(target=_regenerate_thumbnail_background, args=(video_id,), daemon=True).start()
    return {"status": "started"}


@router.get("/{video_id}/thumbnail/regenerate/status")
def get_thumbnail_regenerate_status(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    return {"regenerating": bool(video.thumbnail_regenerating)}


@router.get("/{video_id}/thumbnail/history")
def list_video_thumbnail_history(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    output_ref = str(video.output_path or "")
    is_remote = video.storage_backend in ("b2", "r2") or output_ref.startswith(("http://", "https://"))
    video_path = STORAGE_PATH / output_ref if output_ref and not is_remote else STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "output.mp4"
    history_dir = video_path.parent / "thumbnail_history"
    items = []
    if history_dir and history_dir.is_dir():
        items = [{"filename": p.name, "url": f"/api/videos/{video.id}/thumbnail/history/{p.name}"} for p in sorted(history_dir.glob('*.jpg'), reverse=True)]
    return {"current": f"/api/videos/{video.id}/thumbnail/download", "history": items[:20]}


@router.get("/{video_id}/thumbnail/history/{filename}")
def get_video_thumbnail_history(video_id: str, filename: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    base = (STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "thumbnail_history") if _video_is_remote(video) else (STORAGE_PATH / video.output_path).parent / "thumbnail_history"
    path = base / filename
    if not path.exists() or not path.is_relative_to(base):
        raise HTTPException(status_code=404, detail="Version introuvable")
    return FileResponse(path, media_type="image/jpeg")

@router.post("/{video_id}/thumbnail/history/{filename}/restore")
def restore_video_thumbnail_history(video_id: str, filename: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    current = (STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "thumbnail.jpg") if _video_is_remote(video) else (STORAGE_PATH / video.output_path).with_name("thumbnail.jpg")
    base = current.parent / "thumbnail_history"
    source = base / filename
    if not source.exists() or not source.is_relative_to(base):
        raise HTTPException(status_code=404, detail="Version introuvable")
    if current.exists():
        shutil.copy2(current, base / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.jpg")
    shutil.copy2(source, current)
    return {"status": "ok"}

@router.get("/channel/{channel_id}")
def list_channel_videos(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    videos = db.query(Video).filter(Video.channel_id == channel_id).order_by(Video.created_at.desc()).all()
    _backfill_trust_scores(db, videos)
    return [v.to_dict() for v in videos]

@router.post("/{video_id}/cancel")
def cancel_video(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lets a creator stop a video that's still queued or actively rendering
    — so they're never stuck watching a render play out to a result they
    already know they don't want. Just flips the status; the worker's own
    update_progress checks it between pipeline stages (see queue_runner.py's
    VideoCancelledError) and stops there, refunding whatever credits that
    attempt already spent. A queued-but-not-yet-picked-up video is simply
    never claimed by the worker (its claiming query filters status='queued')."""
    video = _get_owned_video(db, video_id, current_user)
    if video.status not in (VideoStatus.QUEUED.value, VideoStatus.RENDERING.value):
        raise HTTPException(status_code=409, detail="Cette vidéo n'est plus en cours de génération.")
    video.status = VideoStatus.CANCELLED.value
    video.progress_stage = "Annulée par le créateur"
    db.commit()
    db.refresh(video)
    return video.to_dict()


@router.post("/{video_id}/retry")
def retry_video(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = _get_owned_video(db, video_id, current_user)
    channel = video.channel
    # A retry isn't a new submission — the creator already waited once for
    # this exact video. Bumping admin_priority puts it ahead of freshly
    # launched (priority 0) videos in the worker's claiming order instead of
    # going to the back of the whole queue behind newer requests.
    video.admin_priority = max(video.admin_priority or 0, 1)

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
        # The render picker explicitly ignores automatic rows whose script is
        # still empty. Keeping this retry QUEUED gives it its proper FIFO
        # position before the background writer starts, rather than making a
        # newer retry look active ahead of an older render.
        video.status = VideoStatus.QUEUED.value
        # A manual retry starts a new interruption budget. Without this reset,
        # a video that already reached the automatic-restart ceiling fails
        # immediately on the very next server restart, making the Retry button
        # effectively useless.
        video.restart_count = 0
        video.error_message = None
        video.progress_stage = "En attente dans la file"
        video.progress_percent = 0
        db.commit()
        Thread(target=retry_auto_video_script_background, args=(video.id,), daemon=True).start()
        db.refresh(video)
        return video.to_dict()

    video.status = VideoStatus.QUEUED.value
    video.restart_count = 0
    video.error_message = None
    video.progress_stage = "En attente du moteur de rendu"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)

    return video.to_dict()


@router.post("/{video_id}/retry-visuals")
def retry_video_visuals(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Re-runs only the visual side of a rendered video — images/clips and
    the final assembly — while keeping the already-generated voiceover
    exactly as-is, for when the audio/narration is fine but the visuals
    weren't (e.g. an empty stock-footage/library pool that fell all the way
    through to plain synthetic gradient art). Unlike /retry (a full re-run)
    or the scene-editor's is_reassembly path (re-muxes EXISTING clips
    unchanged), this clears the images/clips/scenes.json so
    run_video_pipeline's own checkpoint logic regenerates every scene's
    visual fresh — while that same checkpoint logic reuses voiceover.mp3 +
    transcript.json untouched, since neither is deleted here."""
    video = _get_owned_video(db, video_id, current_user)
    if video.status not in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
        raise HTTPException(status_code=409, detail="Cette vidéo doit être terminée ou en échec pour relancer uniquement le montage.")
    channel = video.channel
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    source_dir = video_dir / "source"
    voiceover_path = source_dir / "voiceover.mp3"
    if not voiceover_path.exists():
        raise HTTPException(status_code=409, detail="La voix off de cette vidéo n'est plus disponible sur le serveur — utilise « Relancer » (rendu complet) à la place.")

    for rel in ("images", "clips"):
        target = source_dir / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    for filename in ("scenes.json", "subtitles.ass"):
        target = source_dir / filename
        if target.exists():
            target.unlink()

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = False
    video.restart_count = 0
    # This video already finished once — a creator retrying its visuals
    # shouldn't wait behind every freshly launched video ahead of it.
    video.admin_priority = max(video.admin_priority or 0, 1)
    video.error_message = None
    video.progress_stage = "En attente (relance du montage — voix off conservée)"
    video.progress_percent = 0
    db.commit()
    db.refresh(video)
    return video.to_dict()


# A finished video now lives on B2 (see _finalize_output_storage) for
# VIDEO_RETENTION_HOURS (30 days) free of charge before queue_runner.py's
# purge sweep archives it — see purge_old_videos_and_uploads. Beyond that,
# a creator pays to extend, and it's always a fresh purchase for a fixed
# window: no "à vie" tier, on purpose, so storage cost for an inactive video
# never goes permanently unpaid — each tier just moves retention_until
# further out from whichever is later (now or the current expiry), and
# lapses again once that window passes, same as before.
RETENTION_TIERS: Dict[str, Dict[str, Any]] = {
    "1d": {"days": 1, "credits": 200, "label": "1 jour"},
    "1w": {"days": 7, "credits": 800, "label": "1 semaine"},
    "1m": {"days": 30, "credits": 3000, "label": "1 mois"},
    "2m": {"days": 60, "credits": 5500, "label": "2 mois"},
    "3m": {"days": 90, "credits": 7500, "label": "3 mois"},
    "6m": {"days": 180, "credits": 13000, "label": "6 mois"},
    "1y": {"days": 365, "credits": 22000, "label": "1 an"},
}


class VideoUpdate(BaseModel):
    title: Optional[str] = None
    folder_id: Optional[str] = None
    clear_folder: bool = False
    approved_for_publish: Optional[bool] = None
    extended_retention: Optional[bool] = None
    retention_tier: Optional[str] = None
    # Opportunistic script edit — see update_video below: only takes effect
    # while the video is still 'queued', on a best-effort basis (no delay is
    # introduced to let a creator "catch" it; the worker may already have
    # claimed it by the time this request lands, in which case it's rejected
    # with a clear message instead of silently doing nothing).
    script_text: Optional[str] = None

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

    if payload.script_text is not None:
        # Opportunity window, not a gate: the worker isn't held up waiting
        # for a creator to look — this only succeeds if the video is still
        # sitting in 'queued' at the moment the request arrives. Once it's
        # been claimed ('rendering' or later), the script is already
        # committed to the pipeline and editing it here would silently do
        # nothing useful — better to say so plainly.
        if video.status != VideoStatus.QUEUED.value:
            raise HTTPException(status_code=409, detail="Le rendu de cette vidéo a déjà démarré — le script ne peut plus être modifié.")
        new_script = payload.script_text.strip()
        if video.input_type == "text" and len(new_script) < 40:
            raise HTTPException(status_code=400, detail="Le script est trop court (40 caractères minimum).")
        video.script_text = new_script
        video.estimated_duration_seconds = max(3.0, len(new_script.split()) / 2.5)

    if payload.retention_tier is not None:
        from datetime import timedelta
        from src.utils.billing import debit_credits, get_credit_balance
        from src.worker.queue_runner import VIDEO_RETENTION_HOURS
        tier = RETENTION_TIERS.get(payload.retention_tier)
        if not tier:
            raise HTTPException(status_code=400, detail=f"Palier de conservation inconnu. Choisissez parmi : {', '.join(RETENTION_TIERS)}.")
        if video.purged_at or not video.output_path:
            raise HTTPException(status_code=409, detail="Cette vidéo a déjà été supprimée du stockage.")
        cost = tier["credits"]
        if not debit_credits(db, current_user, cost, f"Conservation vidéo +{tier['label']}", video_id=video.id):
            raise HTTPException(status_code=402, detail=f"Crédits insuffisants : {cost} crédits requis, {get_credit_balance(db, current_user)} disponibles.")
        default_expiry = (video.finished_at or datetime.utcnow()) + timedelta(hours=VIDEO_RETENTION_HOURS)
        base = max(default_expiry, video.retention_until or default_expiry, datetime.utcnow())
        video.retention_until = base + timedelta(days=tier["days"])
        video.extended_retention = True
        video.purged_at = None

    if payload.extended_retention is False:
        # Cancelling prevents future purchases; credits already consumed are
        # not refundable and the normal free-window deletion rule applies again.
        video.extended_retention = False
        video.retention_until = None

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

def _extract_video_frame(video_source: Path, thumb_path: Path) -> Path:
    """Grabs (and caches) a single frame out of a video file as a jpg —
    shared by every place that needs a still to show for something that's
    actually a video clip (a video-slot scene, or an in-progress Ken Burns
    clip — see both call sites below)."""
    if not video_source.exists():
        raise HTTPException(status_code=404, detail="Le fichier source est introuvable.")
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    if not thumb_path.exists() or thumb_path.stat().st_mtime < video_source.stat().st_mtime:
        try:
            run_ffmpeg([
                "ffmpeg", "-y", "-ss", "0.3", "-i", str(video_source),
                "-frames:v", "1", "-q:v", "3", str(thumb_path),
            ])
        except Exception as e:
            logger.warning(f"Frame extraction failed for {video_source}: {e}")
            raise HTTPException(status_code=404, detail="Aperçu indisponible.")
    return thumb_path


def _resolve_scene_thumbnail(video: Video, scene: Dict[str, Any]) -> Path:
    """A real image file to show for this scene's thumbnail.

    Scenes built from an AI-generated or uploaded image have `image_path`
    pointing straight at that file. But a video-slot scene (B-roll/user clip
    — see orchestrator.py's scenes_manifest) has `image_path` set to None by
    design; both the live "Suivi" panel and the post-render Studio editor
    were still trying to load a still image for those scenes and silently
    showing a broken <img> for every one of them on any channel using video
    or mixed visuals. Grab a single frame out of the scene's own video
    source instead, cached on disk so repeated polls don't re-run ffmpeg.
    """
    image_path = scene.get("image_path")
    if image_path and Path(image_path).exists():
        return Path(image_path)

    visual_path = scene.get("visual_path")
    if not visual_path:
        raise HTTPException(status_code=404, detail="Aucune image pour cette scène.")
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    thumbs_dir = video_dir / "source" / "thumbnails"
    thumb_path = thumbs_dir / f"scene_{scene.get('index', 0)}.jpg"
    return _extract_video_frame(Path(visual_path), thumb_path)


def _load_scenes_manifest(video: Video, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    scenes_path = video_dir / "source" / "scenes.json"
    if not scenes_path.exists():
        # Fichiers d'édition purgés localement (voir EDIT_ASSETS_RETENTION_DAYS) —
        # mais archivés sur B2 avant suppression, donc récupérables à la demande
        # plutôt qu'une fin de partie pour l'édition.
        from src.worker.queue_runner import restore_edit_assets
        restored = restore_edit_assets(video)
        if restored and scenes_path.exists():
            if db is not None:
                video.edit_assets_purged_at = None
                video.edit_assets_restored_at = datetime.utcnow()
                db.commit()
        else:
            raise HTTPException(
                status_code=409,
                detail="Cette vidéo n'est plus éditable (fichiers sources introuvables, y compris dans l'archive, ou vidéo antérieure à cette fonctionnalité).",
            )
    import json
    return json.loads(scenes_path.read_text(encoding="utf-8"))


@router.get("/{video_id}/production-progress")
def get_video_production_progress(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Expose safe, progressively-created production artifacts to the owner."""
    video = _get_owned_video(db, video_id, current_user)
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    source_dir = video_dir / "source"
    transcript_path = source_dir / "transcript.json"
    scenes_path = source_dir / "scenes.json"
    audio_path = source_dir / "voiceover.mp3"
    subtitles_path = source_dir / "subtitles.ass"
    output_path = video_dir / "output.mp4"

    transcript = None
    if transcript_path.exists():
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except Exception:
            transcript = None

    scenes = []
    if scenes_path.exists():
        try:
            manifest = json.loads(scenes_path.read_text(encoding="utf-8"))
            scenes = [{
                "index": item.get("index", index),
                "start": item.get("start"),
                "end": item.get("end"),
                "duration": item.get("duration"),
                "text": item.get("text") or "",
                "visual_type": item.get("visual_type") or "image",
                # Routed through the same thumbnail endpoint as the
                # post-render editor (not production-assets/{filename}
                # directly) — a video-slot scene (B-roll/user clip) has no
                # still image file to serve as-is, and that endpoint only
                # ever grabs one frame out of the video source on demand.
                "image_url": f"/videos/{video.id}/scenes/{item.get('index', index)}/image",
            } for index, item in enumerate(manifest)]
        except Exception:
            scenes = []

    # scenes.json is written only after every clip is built (Step 6/7) — but
    # clips themselves are written one by one, in parallel, during Step 5/7
    # ("Animation des scènes"). Without this, a video-B-roll/mixed channel
    # (whose scenes never touch images_dir at all — see the fallback below)
    # showed a completely empty gallery through that whole stage, worse than
    # the placeholder text ("la galerie se remplira au fur et à mesure")
    # actually promised. Each clip already IS the real per-scene visual
    # (Ken Burns pan/zoom applied), so a frame grabbed from it previews
    # accurately for image-slot and video-slot scenes alike.
    if not scenes:
        clips_dir = source_dir / "clips"
        if clips_dir.exists():
            clip_files = sorted(
                (p for p in clips_dir.iterdir() if p.is_file() and re.match(r"^clip_(\d+)\.mp4$", p.name)),
                key=lambda p: int(re.match(r"^clip_(\d+)\.mp4$", p.name).group(1)),
            )
            for clip_path in clip_files:
                clip_number = int(re.match(r"^clip_(\d+)\.mp4$", clip_path.name).group(1))
                scenes.append({
                    "index": clip_number - 1,
                    "text": "",
                    "visual_type": "video",
                    "image_url": f"/videos/{video.id}/production-clip-thumbnail/{clip_number - 1}",
                })

    # Before clip-building even starts, show the raw fetched image files as
    # they land (image-slot channels only — a video-slot scene has nothing
    # here until its clip exists, covered above instead).
    if not scenes:
        images_dir = source_dir / "images"
        if images_dir.exists():
            for index, image_path in enumerate(sorted(p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_UPLOAD_EXTENSIONS)):
                scenes.append({
                    "index": index,
                    "text": "",
                    "visual_type": "image",
                    "image_url": f"/videos/{video.id}/production-assets/{image_path.name}",
                })

    subtitle_preview = ""
    if transcript:
        subtitle_preview = transcript.get("text") or ""
    elif subtitles_path.exists():
        subtitle_preview = subtitles_path.read_text(encoding="utf-8", errors="ignore")[-12000:]

    return {
        "video_id": video.id,
        "title": video.title,
        "status": video.status,
        "stage": video.progress_stage,
        "percent": video.progress_percent,
        "error": video.error_message,
        "script": video.script_text or "",
        "transcript": (transcript or {}).get("text") or "",
        "subtitle_preview": subtitle_preview,
        "audio_ready": audio_path.exists(),
        "audio_url": f"/videos/{video.id}/production-audio" if audio_path.exists() else None,
        "subtitles_ready": subtitles_path.exists() or bool(transcript),
        "scenes": scenes,
        "final_ready": output_path.exists() or bool(video.output_path),
        "updated_at": datetime.utcnow().isoformat(),
    }


@router.get("/{video_id}/production-clip-thumbnail/{index}")
def get_video_production_clip_thumbnail(video_id: str, index: int, db: Session = Depends(get_db)):
    """A still frame from a Ken Burns clip already built for this scene,
    while the video is still mid-render (Step 5/7, before scenes.json even
    exists) — used by the live "Suivi" panel so the gallery actually fills
    in during clip-building, not just once the whole video is nearly done.
    Intentionally unauthenticated, same reasoning as get_scene_image."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    clip_path = video_dir / "source" / "clips" / f"clip_{index + 1:03d}.mp4"
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail="Ce plan n'est pas encore prêt.")
    thumb_path = video_dir / "source" / "thumbnails" / f"clip_{index}.jpg"
    return FileResponse(_extract_video_frame(clip_path, thumb_path))


@router.get("/{video_id}/production-audio")
def get_video_production_audio(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    audio_path = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "source" / "voiceover.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="L’audio n’est pas encore disponible.")
    return FileResponse(audio_path, media_type="audio/mpeg", filename=f"kappgen-{video_id}-preview.mp3")


@router.get("/{video_id}/production-assets/{filename}")
def get_video_production_asset(video_id: str, filename: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Ressource introuvable.")
    source_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "source"
    candidates = [source_dir / "images" / filename, source_dir / "stock" / filename, source_dir / filename]
    asset = next((path for path in candidates if path.exists() and path.is_file()), None)
    if not asset:
        # Stock and B-roll files can live in provider-specific subfolders.
        asset = next((path for path in source_dir.rglob(filename) if path.is_file()), None)
    if not asset or asset.suffix.lower() not in IMAGE_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Ressource introuvable.")
    return FileResponse(asset)


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
    scenes = _load_scenes_manifest(video, db)
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
    scenes = _load_scenes_manifest(video, db)
    scene = next((s for s in scenes if s["index"] == scene_index), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    return FileResponse(_resolve_scene_thumbnail(video, scene))


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
    scenes = _load_scenes_manifest(video, db)
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
    # Already-finished video, quick reassembly — shouldn't queue behind
    # freshly launched (priority 0) full renders.
    video.admin_priority = max(video.admin_priority or 0, 1)
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

    scenes = _load_scenes_manifest(video, db)
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
    # Already-finished video, quick reassembly — shouldn't queue behind
    # freshly launched (priority 0) full renders.
    video.admin_priority = max(video.admin_priority or 0, 1)
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

    scenes = _load_scenes_manifest(video, db)
    scene = next((s for s in scenes if s["index"] == scene_index), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    if scene.get("word_start_idx") is None:
        raise HTTPException(status_code=409, detail="Cette scène n’a pas de plage audio modifiable (vidéo antérieure à cette fonctionnalité).")

    video.status = VideoStatus.QUEUED.value
    video.is_reassembly = True
    # Already-finished video, quick reassembly — shouldn't queue behind
    # freshly launched (priority 0) full renders.
    video.admin_priority = max(video.admin_priority or 0, 1)
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
    # Already-finished video, quick reassembly — shouldn't queue behind
    # freshly launched (priority 0) full renders.
    video.admin_priority = max(video.admin_priority or 0, 1)
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
        scenes = _load_scenes_manifest(video, db)
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
    # Already-finished video, quick reassembly — shouldn't queue behind
    # freshly launched (priority 0) full renders.
    video.admin_priority = max(video.admin_priority or 0, 1)
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
