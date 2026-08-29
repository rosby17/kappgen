from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
import random
import re
import shutil
import time
import uuid
import httpx
from src.db.session import get_db
from src.db.models import Channel, Video, User, VoiceCloneJob, CommunityLibraryFolder, Voice
from src.models.project import ChannelCreate, ChannelUpdate, VideoStatus, IzivoiceConnectionPayload
from src.config import STORAGE_PATH, IZIVOICE_API_KEY, IZIVOICE_BASE_URL, FRONTEND_BASE_URL
from fastapi.responses import RedirectResponse
from datetime import datetime
from src.pipeline import youtube_publisher
from src.pipeline.niche_detector import suggest_niche
from src.pipeline.script_structure_analyzer import analyze_script_structure_text
from src.utils.credentials import encrypt_credential, izivoice_key_for_user
from src.utils.auth import get_current_user
from src.utils.billing import user_has_purchased_credits
from src.utils.logger import logger

router = APIRouter(prefix="/api/channels", tags=["channels"])

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ALLOWED_LIBRARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
MAX_IMAGE_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

# <script> tags and inline event-handler attributes (onload=, onclick=, ...) —
# an SVG opened directly (not just used as <img src>) executes any script it
# contains in the serving origin. Extension checks alone don't catch this; a
# raster-format upload also can't be trusted just because it has a .png name,
# so non-SVG uploads are verified by actually decoding them below.
_SVG_SCRIPT_PATTERN = re.compile(rb"<\s*script\b", re.IGNORECASE)
_SVG_EVENT_ATTR_PATTERN = re.compile(rb"\son\w+\s*=", re.IGNORECASE)


def validate_uploaded_image(contents: bytes, ext: str, filename: str = "") -> None:
    """Raises HTTPException if `contents` isn't actually a valid image of the
    claimed type, is over the size cap, or (for SVG) contains a script."""
    if len(contents) > MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Image trop volumineuse (max {MAX_IMAGE_UPLOAD_BYTES // (1024*1024)} Mo).")
    if ext == ".svg":
        if _SVG_SCRIPT_PATTERN.search(contents) or _SVG_EVENT_ATTR_PATTERN.search(contents):
            raise HTTPException(status_code=400, detail="Ce fichier SVG contient du code non autorisé.")
        return
    from PIL import Image, UnidentifiedImageError
    import io
    try:
        with Image.open(io.BytesIO(contents)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail=f"Le fichier {filename or ''} n'est pas une image valide.".strip())


def _sync_community_library_folder(db: Session, channel: Channel, share_with_community: bool, image_count: int) -> None:
    """Keeps CommunityLibraryFolder in sync with a channel's own opt-in flag
    and current image count, called after every library upload. Never
    touches `status` on an existing row — a creator re-uploading/adding more
    images shouldn't reset an admin's earlier "approved"/"flagged" call,
    only the raw image_count and eligibility (share or not) change here.
    Caller is responsible for the db.commit()."""
    existing = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel.id).first()
    if not share_with_community or image_count <= 0:
        if existing:
            db.delete(existing)
        return
    if existing:
        existing.image_count = image_count
        existing.niche = channel.niche
    else:
        db.add(CommunityLibraryFolder(
            channel_id=channel.id,
            user_id=channel.user_id,
            niche=channel.niche,
            image_count=image_count,
        ))


def _fill_logo_from_youtube_avatar(channel: Channel, thumbnail_url: Optional[str]) -> None:
    """Downloads the connected YouTube channel's own avatar and uses it as
    the video-overlay logo (branding.logo_path) — so a creator who connects
    their real YouTube channel gets a logo on their videos immediately,
    instead of the corner staying blank until they separately upload one
    manually. Never overwrites a logo that's already set (whether uploaded
    by hand or filled in by an earlier sync), mutates channel.branding
    in place; caller is responsible for the db.commit()."""
    if not thumbnail_url:
        return
    branding = dict(channel.branding or {})
    if branding.get("logo_path"):
        return
    try:
        resp = httpx.get(thumbnail_url, timeout=15)
        resp.raise_for_status()
        contents = resp.content
        content_type = resp.headers.get("content-type", "")
        ext = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
        validate_uploaded_image(contents, ext)
    except Exception as exc:
        logger.warning(f"Could not download YouTube avatar as logo for channel {channel.id}: {exc}")
        return
    channel_dir = STORAGE_PATH / "channels" / channel.id
    channel_dir.mkdir(parents=True, exist_ok=True)
    for old_logo in channel_dir.glob("logo.*"):
        old_logo.unlink(missing_ok=True)
    (channel_dir / f"logo{ext}").write_bytes(contents)
    branding["logo_path"] = f"channels/{channel.id}/logo{ext}"
    channel.branding = branding


@router.get("/izivoice/status")
def izivoice_status(current_user: User = Depends(get_current_user)):
    return {"connected": bool(current_user.izivoice_api_key_encrypted), "key_prefix": current_user.izivoice_key_prefix, "mode": "personal" if current_user.izivoice_api_key_encrypted else "nichecut"}


@router.post("/izivoice/connect")
def connect_izivoice(payload: IzivoiceConnectionPayload, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Saisissez une clé API Izivoice.")
    try:
        response = httpx.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {api_key}"}, params={"page": 1, "page_size": 1}, timeout=30)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Izivoice est temporairement inaccessible.") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=400, detail="Cette clé API Izivoice est invalide ou révoquée.")
    if not response.is_success:
        raise HTTPException(status_code=502, detail="Impossible de vérifier cette clé auprès d’Izivoice.")
    user.izivoice_api_key_encrypted = encrypt_credential(api_key)
    user.izivoice_key_prefix = f"{api_key[:8]}…{api_key[-4:]}" if len(api_key) > 14 else f"{api_key[:4]}…"
    user.izivoice_connected_at = datetime.utcnow()
    db.commit()
    return {"connected": True, "key_prefix": user.izivoice_key_prefix, "mode": "personal"}


@router.delete("/izivoice/connect")
def disconnect_izivoice(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.izivoice_api_key_encrypted = None
    user.izivoice_key_prefix = None
    user.izivoice_connected_at = None
    db.commit()
    return {"connected": False, "key_prefix": None, "mode": "nichecut"}


@router.get("/voice/catalog")
def list_voice_catalog(
    language: Optional[str] = None,
    search: Optional[str] = None,
    page: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """First checks the local `voices` database table for imported synthetic voices.
    If local voices exist, queries them with high-performance pagination and filtering.
    Otherwise, falls back to the remote Izivoice catalog endpoint."""
    local_count = db.query(Voice).filter(Voice.is_active == True).count()
    if local_count > 0:
        query = db.query(Voice).filter(Voice.is_active == True)
        if language:
            query = query.filter(Voice.language.ilike(f"%{language}%"))
        if search:
            query = query.filter(Voice.name.ilike(f"%{search}%"))

        page_num = page if page is not None else 0
        page_size = 100
        total_matching = query.count()
        voices = query.offset(page_num * page_size).limit(page_size).all()
        has_more = (page_num + 1) * page_size < total_matching

        return {
            "voices": [v.to_dict() for v in voices],
            "has_more": has_more,
            "page": page_num,
            "total": total_matching,
            "source": "local_db"
        }

    api_key = izivoice_key_for_user(current_user)
    if not api_key:
        raise HTTPException(status_code=503, detail="Le catalogue de voix n'est pas configuré.")
    if page is not None:
        params = {"page": page, "page_size": 100}
        if language:
            params["language"] = language
        if search:
            params["search"] = search
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {api_key}"}, params=params)
            response.raise_for_status()
            data = (response.json().get("data") or {})
        batch = data.get("voices") or []
        has_more = bool(data.get("has_more")) or len(batch) >= 100
        return {"voices": batch, "has_more": has_more, "page": page}

    max_pages = 3 if search else 10
    all_voices = []
    has_more = False
    with httpx.Client(timeout=30) as client:
        for p in range(0, max_pages):
            params = {"page": p, "page_size": 100}
            if language:
                params["language"] = language
            if search:
                params["search"] = search
            response = client.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {api_key}"}, params=params)
            response.raise_for_status()
            data = (response.json().get("data") or {})
            batch = data.get("voices") or []
            all_voices.extend(batch)
            has_more = bool(data.get("has_more")) or len(batch) >= 100
            if not batch:
                break
    return {"voices": all_voices, "has_more": has_more, "next_page": max_pages}

# Voice-cloning itself now runs on the worker (src/pipeline/voice_clone.py),
# tracked via VoiceCloneJob rows instead of an in-memory dict here — the
# API container gets redeployed far more often than the worker (routine
# API/frontend changes never touch it), and an in-memory job tied to the API
# process was getting silently wiped mid-clone by any redeploy, leaving the
# creator's "Clonage…" button stuck forever. See voice_clone.py's module
# docstring for the full story.
from src.pipeline.voice_clone import VOICE_PREVIEW_TEXT, CLONE_UPLOADS_DIR


@router.get("/voice/lookup/{voice_id}")
def lookup_voice_by_id(voice_id: str, current_user: User = Depends(get_current_user)):
    """Lets a creator who already has their own voice cloned directly on
    Izivoice (outside KappGen) attach it here by pasting its voice_id,
    instead of re-cloning it — useful as a fallback while the in-app clone
    flow above is unreliable, and generally faster for anyone who already
    knows their id."""
    api_key = izivoice_key_for_user(current_user)
    if not api_key:
        raise HTTPException(status_code=503, detail="Connecte d'abord ton compte Izivoice dans les paramètres.")
    voice_id_clean = voice_id.strip()
    if not voice_id_clean:
        raise HTTPException(status_code=400, detail="Identifiant de voix requis.")
    try:
        response = httpx.get(f"{IZIVOICE_BASE_URL}/voices/{voice_id_clean}", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Izivoice est temporairement inaccessible.") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Aucune voix Izivoice ne correspond à cet identifiant.")
    if response.status_code in (401, 403):
        raise HTTPException(status_code=403, detail="Cette voix n'est pas accessible avec ta clé Izivoice.")
    if not response.is_success:
        raise HTTPException(status_code=502, detail="Impossible de vérifier cet identifiant auprès d'Izivoice.")
    data = response.json()
    data = data.get("data") or data
    name = data.get("name") or f"Voix {voice_id_clean[:8]}"
    return {"voice_id": voice_id_clean, "name": name, "language": data.get("language"), "gender": data.get("gender")}


@router.post("/{channel_id}/voice/clone")
async def clone_channel_voice(channel_id: str, name: str = Form(...), audio: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    contents = await audio.read()
    if not contents:
        raise HTTPException(status_code=400, detail="L'échantillon audio est vide.")
    api_key = izivoice_key_for_user(current_user)
    if not api_key:
        raise HTTPException(status_code=503, detail="Izivoice n'est pas configuré.")

    job_id = uuid.uuid4().hex
    CLONE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    audio_rel_path = f"voice_clone_uploads/{job_id}{Path(audio.filename or '').suffix or '.bin'}"
    (STORAGE_PATH / audio_rel_path).write_bytes(contents)
    db.add(VoiceCloneJob(
        id=job_id,
        channel_id=channel_id,
        user_id=current_user.id,
        name=name.strip(),
        audio_path=audio_rel_path,
        status="pending",
    ))
    db.commit()
    return {"job_id": job_id, "status": "pending"}


@router.get("/voice/clone/status/{job_id}")
def clone_voice_status(job_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = db.query(VoiceCloneJob).filter(VoiceCloneJob.id == job_id, VoiceCloneJob.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Tâche de clonage introuvable ou expirée.")
    return job.to_dict()


@router.get("/voice/{voice_id}/preview")
def get_voice_preview(voice_id: str, current_user: User = Depends(get_current_user)):
    """Serves the short sample generated right after cloning (see
    _run_clone_job) — Izivoice's /clone itself never returns one."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise HTTPException(status_code=404, detail="Aperçu introuvable.")
    preview_path = STORAGE_PATH / "voice_previews" / f"{voice_id}.mp3"
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Aperçu introuvable.")
    return FileResponse(preview_path, media_type="audio/mpeg")


@router.get("/storage/voices/previews/{filename}")
def serve_imported_voice_preview(filename: str):
    """Serves imported voice preview audio files."""
    clean_name = Path(filename).name
    preview_path = STORAGE_PATH / "voices" / "previews" / clean_name
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Extrait audio introuvable.")
    media_type = "audio/wav" if clean_name.lower().endswith(".wav") else "audio/mpeg"
    return FileResponse(preview_path, media_type=media_type)


@router.post("/voice/{voice_id}/preview/generate")
def generate_voice_preview(voice_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """On-demand version of the preview generated automatically right after
    cloning — covers voices cloned before that existed, or whose best-effort
    generation failed at the time (see _run_clone_job)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise HTTPException(status_code=400, detail="Identifiant de voix invalide.")
    api_key = izivoice_key_for_user(current_user)
    if not api_key:
        raise HTTPException(status_code=503, detail="Connecte d'abord ton compte Izivoice dans les paramètres.")
    preview_path = STORAGE_PATH / "voice_previews" / f"{voice_id}.mp3"
    if not preview_path.exists():
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        from src.pipeline.voiceover import generate_voiceover
        try:
            generate_voiceover(VOICE_PREVIEW_TEXT, preview_path, voice_id=voice_id, api_key=api_key)
        except Exception as exc:
            logger.warning(f"On-demand voice preview generation failed for {voice_id}: {exc}")
            raise HTTPException(status_code=502, detail="Impossible de générer l'aperçu pour cette voix.")
    return {"preview_url": f"/channels/voice/{voice_id}/preview"}

async def save_valid_library_images(files: List[UploadFile], target_dir: Path, append: bool = False):
    """append=True writes straight into target_dir (creating it if needed)
    alongside whatever's already there — used when the frontend splits a
    large folder into several requests (Cloudflare hard-caps a single
    request body at 100MB, so a 140-photo folder has to arrive in batches).
    append=False keeps the original atomic swap-replace behavior for a
    single-shot upload."""
    if append:
        target_dir.mkdir(parents=True, exist_ok=True)
        existing = list(target_dir.glob("img_*"))
        start_index = len(existing)
        saved = 0
        rejected = 0
        for file in files:
            ext = Path(file.filename or "").suffix.lower()
            if ext not in ALLOWED_LIBRARY_EXTENSIONS:
                rejected += 1
                continue
            contents = await file.read()
            if not contents:
                rejected += 1
                continue
            (target_dir / f"img_{start_index + saved:04d}_{uuid.uuid4().hex[:8]}{ext}").write_bytes(contents)
            saved += 1
        return saved, rejected

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
            # Trust the extension instead of decoding every image with PIL —
            # for a large batch (100+ MB), a full open()+verify() per file was
            # slow enough to blow past Cloudflare's fixed ~100s proxy timeout,
            # which killed the upload client-side right as it finished
            # uploading. A quick non-empty check catches the obvious garbage;
            # a genuinely corrupt image just falls back gracefully at render
            # time like any other unusable library asset.
            if not contents:
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


def _suggest_niche_for_channel(db: Session, title: str, description: str) -> Optional[str]:
    """Best-effort — the creator's own choice (or the manual default) always
    stays if this fails or isn't confident."""
    existing = [r[0] for r in db.query(Channel.niche).distinct().all() if r[0]]
    return suggest_niche(title, description, existing)


class NicheSuggestRequest(BaseModel):
    name: str = ""
    description: str = ""


@router.post("/suggest-niche")
def suggest_niche_endpoint(payload: NicheSuggestRequest, db: Session = Depends(get_db)):
    """Manual counterpart to the automatic YouTube-connect suggestion — lets the
    wizard offer a niche guess from the name/description the creator just typed,
    without requiring a YouTube connection."""
    niche = _suggest_niche_for_channel(db, payload.name, payload.description)
    return {"niche": niche}


class ScriptStructurePart(BaseModel):
    name: str = ""
    word_count: int = 0
    guidance: str = ""


class ScriptStructureAnalyzeRequest(BaseModel):
    text: str = ""
    parts: List[ScriptStructurePart] = []


@router.post("/analyze-script-structure")
def analyze_script_structure_endpoint(payload: ScriptStructureAnalyzeRequest, current_user: User = Depends(get_current_user)):
    """Lets a creator paste one full block of instructions/script text instead
    of filling each structure part by hand — the AI splits it across the
    existing parts (matched by name) and returns their filled-in guidance."""
    try:
        parts = analyze_script_structure_text(payload.text, [p.model_dump() for p in payload.parts])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"[analyze-script-structure] failed: {e}")
        raise HTTPException(status_code=502, detail="L'analyse IA a échoué, réessaie.")
    return {"parts": parts}

@router.get("", response_model=List[Dict[str, Any]])
def list_channels(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channels = db.query(Channel).filter(Channel.user_id == current_user.id).order_by(Channel.created_at.desc()).all()
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
def create_channel(payload: ChannelCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Same paid-feature gate as update_channel below — without this, someone
    # who never bought a credit pack could just create a brand-new channel
    # with the watermark disabled from the start, skipping the PUT-only
    # check entirely. Free-trial creators (running on free_video_quota, never
    # having paid) always keep the watermark — this is a lifetime unlock tied
    # to having paid at least once, not to a currently-active subscription.
    # Silently corrected rather than rejecting the whole request: the render
    # pipeline re-enforces this anyway (see _channel_config_for_render in
    # queue_runner.py), so failing the entire channel save over one field a
    # client could have simply omitted would only be worse UX for no real
    # security benefit.
    if not payload.effects_config.watermark_enabled and not user_has_purchased_credits(db, current_user):
        payload.effects_config.watermark_enabled = True

    from src.utils.billing import user_max_channels
    max_channels = user_max_channels(db, current_user)
    if max_channels is not None:
        existing_count = db.query(Channel).filter(Channel.user_id == current_user.id).count()
        if existing_count >= max_channels:
            raise HTTPException(
                status_code=403,
                detail=f"Ton abonnement actuel est limité à {max_channels} chaîne{'s' if max_channels > 1 else ''}. Passe à un palier supérieur pour en créer davantage.",
            )

    channel = Channel(
        user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        niche=payload.niche,
        content_type=payload.content_type or "narration",
        music_channel_config=payload.music_channel_config,
        subtitle_style=payload.subtitle_style.model_dump(),
        branding=payload.branding.model_dump(),
        music_preference=payload.music_preference.model_dump(),
        image_style=payload.image_style.model_dump(),
        effects_config=payload.effects_config.model_dump(),
        automation_mode=payload.automation_mode or "manual",
        automation_style_prompt=payload.automation_style_prompt,
        topic_examples=payload.topic_examples,
        use_web_trends=bool(payload.use_web_trends),
        videos_per_day=max(1, payload.videos_per_day or 1),
        automation_window_start_hour=payload.automation_window_start_hour if payload.automation_window_start_hour is not None else 7,
        automation_window_end_hour=payload.automation_window_end_hour if payload.automation_window_end_hour is not None else 11,
        active_days=payload.active_days,
        script_generation_hour=None if (payload.script_generation_hour is None or payload.script_generation_hour < 0) else payload.script_generation_hour,
        script_generation_minute=max(0, min(59, payload.script_generation_minute or 0)),
        script_generation_second=max(0, min(59, payload.script_generation_second or 0)),
        script_generation_days=payload.script_generation_days,
        script_structure=payload.script_structure,
        voice_id=payload.voice_id,
        voice_name=payload.voice_name,
        voice_settings=payload.voice_settings,
        publish_mode=payload.publish_mode or "manual",
        publish_time_mode=payload.publish_time_mode or "range",
        publish_schedule_hour=payload.publish_schedule_hour,
        publish_schedule_day_offset=payload.publish_schedule_day_offset,
        timezone=payload.timezone or "Africa/Douala",
        transcribe_audio_default=payload.transcribe_audio_default if payload.transcribe_audio_default is not None else True,
        thumbnail_style=payload.thumbnail_style,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.get("/{channel_id}")
def get_channel(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
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


@router.get("/community-library/availability")
def community_library_availability(niche: str, db: Session = Depends(get_db)):
    """Whether the "Bibliothèque collaborative" visual source is usable for
    a given niche — lets the wizard enable/disable that option without a
    creator having to try it first and hit a render-time error."""
    folders = (
        db.query(CommunityLibraryFolder)
        .filter(CommunityLibraryFolder.status == "approved", CommunityLibraryFolder.niche.ilike(niche))
        .all()
    )
    return {
        "available": len(folders) > 0,
        "folder_count": len(folders),
        "image_count": sum(f.image_count for f in folders),
    }

@router.put("/{channel_id}")
def update_channel(channel_id: str, payload: ChannelUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    # Watermark removal is a lifetime unlock tied to having received real
    # credits at least once (purchase or admin grant) — without this,
    # anyone could just flip the toggle themselves, free-trial creators
    # included. Silently corrected rather than rejecting the whole update:
    # the render pipeline re-enforces this anyway (_channel_config_for_render
    # in queue_runner.py), so failing the entire channel save over one field
    # a client could have simply omitted would only be worse UX for no real
    # security benefit.
    if payload.effects_config is not None and not payload.effects_config.watermark_enabled:
        if not user_has_purchased_credits(db, current_user):
            payload.effects_config.watermark_enabled = True

    if payload.name is not None:
        channel.name = payload.name
    if payload.description is not None:
        channel.description = payload.description
    if payload.niche is not None:
        channel.niche = payload.niche
    if payload.content_type is not None:
        channel.content_type = payload.content_type
    if payload.music_channel_config is not None:
        channel.music_channel_config = payload.music_channel_config
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
    if payload.automation_mode is not None:
        channel.automation_mode = payload.automation_mode
    if payload.automation_style_prompt is not None:
        channel.automation_style_prompt = payload.automation_style_prompt
    if payload.topic_examples is not None:
        channel.topic_examples = payload.topic_examples
    if payload.use_web_trends is not None:
        channel.use_web_trends = payload.use_web_trends
    if payload.videos_per_day is not None:
        channel.videos_per_day = max(1, payload.videos_per_day)
    if payload.automation_window_start_hour is not None:
        channel.automation_window_start_hour = payload.automation_window_start_hour
    if payload.automation_window_end_hour is not None:
        channel.automation_window_end_hour = payload.automation_window_end_hour
    if payload.active_days is not None:
        channel.active_days = payload.active_days
    if payload.script_generation_hour is not None:
        # -1 is the frontend's explicit "back to as-soon-as-possible" sentinel —
        # plain None can't be told apart from "field omitted from this PATCH".
        channel.script_generation_hour = None if payload.script_generation_hour < 0 else payload.script_generation_hour
    if payload.script_generation_minute is not None:
        channel.script_generation_minute = max(0, min(59, payload.script_generation_minute))
    if payload.script_generation_second is not None:
        channel.script_generation_second = max(0, min(59, payload.script_generation_second))
    if payload.script_generation_days is not None:
        channel.script_generation_days = payload.script_generation_days
    if payload.script_structure is not None:
        channel.script_structure = payload.script_structure
    if payload.voice_id is not None:
        channel.voice_id = payload.voice_id
    if payload.voice_name is not None:
        channel.voice_name = payload.voice_name
    if payload.voice_settings is not None:
        channel.voice_settings = payload.voice_settings
    if payload.publish_mode is not None:
        if payload.publish_mode in ("auto", "scheduled"):
            from src.utils.billing import user_autopublish_enabled
            if not user_autopublish_enabled(db, current_user):
                raise HTTPException(
                    status_code=403,
                    detail="La publication automatique sur YouTube n'est pas incluse dans ton abonnement actuel. Passe à un palier supérieur pour l'activer.",
                )
        channel.publish_mode = payload.publish_mode
    if payload.publish_time_mode is not None:
        channel.publish_time_mode = payload.publish_time_mode
    if payload.publish_schedule_hour is not None:
        channel.publish_schedule_hour = payload.publish_schedule_hour
    if payload.publish_schedule_day_offset is not None:
        channel.publish_schedule_day_offset = payload.publish_schedule_day_offset
    if payload.timezone is not None:
        channel.timezone = payload.timezone
    if payload.transcribe_audio_default is not None:
        channel.transcribe_audio_default = payload.transcribe_audio_default
    if payload.is_active is not None:
        channel.is_active = payload.is_active
    if payload.thumbnail_style is not None:
        # Merge rather than replace: this path is how a creator hand-types
        # their own style_prompt (no reference images involved), so it must
        # not wipe out reference_image_paths a prior upload already set —
        # only the upload/delete endpoints below own that list.
        merged = dict(channel.thumbnail_style or {})
        merged.update(payload.thumbnail_style)
        channel.thumbnail_style = merged

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/generate-now")
def generate_now(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """On-demand equivalent of the daily auto pipeline for a single channel:
    only valid for automation_mode == "auto", where a creator clicking
    "Nouvelle vidéo" should never see the manual script/voice form — the
    Agent picks the topic and writes the script itself, immediately.

    Runs the actual generation in a background thread and returns right
    away: script generation makes several sequential Claude calls and can
    run past a proxy/gateway's request timeout, which shows up in the
    browser as a misleading CORS error instead of a timeout. The frontend
    already just checks res.ok and polls for the new video, so there's
    nothing in the immediate response for it to consume."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if not channel.is_active:
        raise HTTPException(status_code=409, detail="Cette chaîne est désactivée. Réactive-la pour générer de nouvelles vidéos.")

    # Music channels have no per-video form to skip (everything needed lives
    # in music_channel_config, set once at channel setup) — a click here is
    # valid regardless of automation_mode, unlike narration's "auto"-only gate.
    if channel.content_type == "music":
        from threading import Thread
        from src.worker.queue_runner import generate_and_queue_music_video_background
        Thread(target=generate_and_queue_music_video_background, args=(channel.id,), daemon=True).start()
        return {"status": "started"}

    if channel.automation_mode != "auto":
        raise HTTPException(status_code=409, detail="Cette chaîne n'est pas en mode automatique.")
    from src.utils.billing import user_ai_script_enabled
    if not user_ai_script_enabled(db, current_user):
        raise HTTPException(
            status_code=403,
            detail="La génération automatique de script (IA) n'est pas incluse dans ton abonnement actuel. Passe à un palier supérieur pour l'utiliser.",
        )

    from threading import Thread
    from src.worker.queue_runner import generate_and_queue_auto_video_background
    Thread(target=generate_and_queue_auto_video_background, args=(channel.id,), daemon=True).start()
    return {"status": "started"}

@router.post("/{channel_id}/logo")
async def upload_channel_logo(channel_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

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
    validate_uploaded_image(contents, ext, file.filename or "")
    dest_file.write_bytes(contents)

    branding = dict(channel.branding or {})
    branding["logo_path"] = f"channels/{channel.id}/logo{ext}"
    channel.branding = branding

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/overlays")
async def upload_channel_overlay(channel_id: str, file: UploadFile = File(...), replace_id: Optional[str] = Form(None), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Adds one extra sticker overlay (e.g. a "Subscribe" button, a bell icon —
    the kind of thing creators used to paste on by hand) burned into every
    render of this channel, on top of the single channel logo. Unlike the logo
    endpoint above, this appends a new slot instead of replacing one — a
    channel can stack several of these in different corners.

    `replace_id`, when set, is an existing overlay's id the frontend is about
    to swap this new upload in for — it's excluded from the 6-slot cap check
    so "replace this image" doesn't spuriously fail at the cap, and lets the
    frontend upload the new file *before* deleting the old one instead of the
    other way round (a failed upload after an eager delete previously could
    leave an overlay referencing nothing at all)."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (png, jpg, webp, gif, svg).")

    branding = dict(channel.branding or {})
    overlays = list(branding.get("overlays") or [])
    counted_overlays = [o for o in overlays if o.get("id") != replace_id] if replace_id else overlays
    if len(counted_overlays) >= 6:
        raise HTTPException(status_code=400, detail="Maximum de 6 incrustations par chaîne.")

    overlay_id = str(uuid.uuid4())
    overlays_dir = STORAGE_PATH / "channels" / channel.id / "overlays"
    overlays_dir.mkdir(parents=True, exist_ok=True)
    dest_file = overlays_dir / f"{overlay_id}{ext}"
    contents = await file.read()
    validate_uploaded_image(contents, ext, file.filename or "")
    dest_file.write_bytes(contents)

    overlays.append({
        "id": overlay_id,
        "image_path": f"channels/{channel.id}/overlays/{overlay_id}{ext}",
        "enabled": True,
        "corner": "top-right",
        "size_percent": 12,
        "opacity": 1.0,
    })
    branding["overlays"] = overlays
    channel.branding = branding

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.delete("/{channel_id}/overlays/{overlay_id}")
def delete_channel_overlay(channel_id: str, overlay_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    branding = dict(channel.branding or {})
    overlays = list(branding.get("overlays") or [])
    remaining = [o for o in overlays if o.get("id") != overlay_id]
    removed = [o for o in overlays if o.get("id") == overlay_id]
    if not removed:
        raise HTTPException(status_code=404, detail="Incrustation introuvable.")

    for o in removed:
        image_path = o.get("image_path")
        if image_path:
            (STORAGE_PATH / image_path).unlink(missing_ok=True)

    branding["overlays"] = remaining
    channel.branding = branding
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/avatar")
async def upload_channel_avatar(channel_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Uploads the channel's app-facing profile picture — shown in channel cards,
    lists and the sidebar. Distinct from the logo (branding.logo_path), which is
    the high-quality asset burned into the rendered video and never resized."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (png, jpg, webp, gif, svg).")

    channel_dir = STORAGE_PATH / "channels" / channel.id
    channel_dir.mkdir(parents=True, exist_ok=True)

    for old_avatar in channel_dir.glob("avatar.*"):
        old_avatar.unlink(missing_ok=True)

    dest_file = channel_dir / f"avatar{ext}"
    contents = await file.read()
    validate_uploaded_image(contents, ext, file.filename or "")
    dest_file.write_bytes(contents)

    branding = dict(channel.branding or {})
    branding["avatar_path"] = f"channels/{channel.id}/avatar{ext}"
    channel.branding = branding

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/preview-ai-music")
async def preview_ai_music(
    niche: str = Form(""),
    ai_prompt: Optional[str] = Form(None),
    script_excerpt: Optional[str] = Form(None),
    duration: float = Form(20.0),
    current_user: User = Depends(get_current_user),
):
    """Generates a short AI music preview on the spot, so the client can listen
    to it in the wizard before saving the channel — same prompt path used at
    render time (Claude-written prompt, or the client's own override)."""
    if not IZIVOICE_API_KEY:
        raise HTTPException(status_code=503, detail="La génération musicale IA n'est pas configurée sur le serveur.")

    from src.utils.billing import debit_izivoice_usage_by_user_id, IZIVOICE_MUSIC_CREDITS
    if not debit_izivoice_usage_by_user_id(current_user.id, IZIVOICE_MUSIC_CREDITS, "ai_music_preview"):
        raise HTTPException(status_code=402, detail=f"Crédits insuffisants — un aperçu musical IA coûte {IZIVOICE_MUSIC_CREDITS} crédits.")

    prompt = (ai_prompt or "").strip()
    if not prompt:
        from src.pipeline.vision import generate_music_prompt
        try:
            prompt = generate_music_prompt(niche, script_excerpt or "")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Génération du prompt impossible : {e}")

    from src.pipeline.music import generate_music_izivoice
    tmp_dir = STORAGE_PATH / "tmp" / "music-previews"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4()}.mp3"
    try:
        generate_music_izivoice(prompt, max(5.0, min(duration, 30.0)), tmp_path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Génération musicale impossible : {e}")

    return FileResponse(tmp_path, media_type="audio/mpeg", filename="preview.mp3")


@router.post("/music-video/preview")
async def preview_music_video_track(
    style_prompt: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    """Free preview for the Music Video channel setup wizard (content_type
    "music") — lets a creator hear their chosen style before committing to a
    channel, with NO credit debit. Unlike preview-ai-music (used by narration
    channels' background-music picker, which does debit), the plan here is
    to only ever charge once a real video finishes rendering — see
    src/utils/billing.py's future per-music-video charge (Phase 4) — so
    experimenting with style during setup has to stay free or creators would
    pay just to find the sound that fits their channel."""
    if not IZIVOICE_API_KEY:
        raise HTTPException(status_code=503, detail="La génération musicale IA n'est pas configurée sur le serveur.")

    prompt = style_prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Décris le style musical voulu.")

    from src.pipeline.music import generate_music_izivoice
    tmp_dir = STORAGE_PATH / "tmp" / "music-previews"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4()}.mp3"
    try:
        generate_music_izivoice(prompt, 20.0, tmp_path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Génération musicale impossible : {e}")

    return FileResponse(tmp_path, media_type="audio/mpeg", filename="preview.mp3")


ALLOWED_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}

@router.post("/{channel_id}/music")
async def upload_channel_music(channel_id: str, files: List[UploadFile] = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Uploads one or more of the client's own background tracks. One is picked at
    random per render — this is the channel's own music, never third-party stock."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    music_dir = STORAGE_PATH / "channels" / channel.id / "music"
    music_dir.mkdir(parents=True, exist_ok=True)

    music_pref = dict(channel.music_preference or {})
    tracks = list(music_pref.get("tracks") or [])
    saved = 0
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_MUSIC_EXTENSIONS:
            continue
        # Keep a sanitized version of the original filename after a short unique
        # prefix — the prefix guarantees no collisions, and the frontend recap
        # strips it back off to show the client their real track name instead
        # of a bare UUID.
        stem = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(file.filename or "track").stem)[:60] or "track"
        dest_name = f"{uuid.uuid4().hex[:8]}_{stem}{ext}"
        dest_path = music_dir / dest_name
        dest_path.write_bytes(await file.read())
        tracks.append(f"channels/{channel.id}/music/{dest_name}")
        saved += 1

    if saved == 0:
        raise HTTPException(status_code=400, detail="Aucun fichier audio valide (mp3, wav, m4a, ogg).")

    music_pref["tracks"] = tracks
    channel.music_preference = music_pref
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.delete("/{channel_id}/music")
def delete_channel_music_track(channel_id: str, track_path: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    music_pref = dict(channel.music_preference or {})
    tracks = list(music_pref.get("tracks") or [])
    if track_path not in tracks:
        raise HTTPException(status_code=404, detail="Track not found on this channel.")
    tracks.remove(track_path)
    music_pref["tracks"] = tracks
    channel.music_preference = music_pref

    file_path = STORAGE_PATH / track_path
    if file_path.exists() and file_path.is_relative_to(STORAGE_PATH / "channels" / channel.id / "music"):
        file_path.unlink(missing_ok=True)

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

ALLOWED_STYLE_REFERENCE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

@router.post("/analyze-style-image")
async def analyze_style_image(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Analyzes a reference image and returns a reusable image-generation style prompt."""
    ext = Path(file.filename or "").suffix.lower()
    media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(ext)
    if not media_type:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (png, jpg, webp).")

    contents = await file.read()
    validate_uploaded_image(contents, ext, file.filename or "")
    from src.pipeline.vision import analyze_reference_image
    try:
        style_prompt = analyze_reference_image(contents, media_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Analyse de l'image impossible : {e}")

    return {"style_prompt": style_prompt}

def _thumbnail_reference_paths(thumbnail_style: dict) -> List[str]:
    """thumbnail_style used to store a single 'reference_image_path' — normalize both
    the old and current ('reference_image_paths', a list) shapes into a list."""
    if not thumbnail_style:
        return []
    paths = thumbnail_style.get("reference_image_paths")
    if paths:
        return list(paths)
    single = thumbnail_style.get("reference_image_path")
    return [single] if single else []


@router.post("/{channel_id}/thumbnail-style")
async def upload_channel_thumbnail_style(channel_id: str, files: List[UploadFile] = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Adds one or more thumbnail-specific reference images for this channel, then
    re-analyzes ALL of them together (existing + newly uploaded) into a single
    reusable style prompt — kept separate from image_style, since the thumbnail
    look is often deliberately different from the video's own body-image style."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    channel_dir = STORAGE_PATH / "channels" / channel.id / "thumbnail_references"
    channel_dir.mkdir(parents=True, exist_ok=True)

    existing_paths = _thumbnail_reference_paths(channel.thumbnail_style or {})
    new_paths = []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(ext)
        if not media_type:
            raise HTTPException(status_code=400, detail=f"Format d'image non supporté pour {file.filename} (png, jpg, webp).")
        contents = await file.read()
        validate_uploaded_image(contents, ext, file.filename or "")
        dest_file = channel_dir / f"{uuid.uuid4().hex}{ext}"
        dest_file.write_bytes(contents)
        new_paths.append(f"channels/{channel.id}/thumbnail_references/{dest_file.name}")

    all_paths = existing_paths + new_paths
    images = []
    for rel_path in all_paths:
        abs_path = STORAGE_PATH / rel_path
        ext = abs_path.suffix.lower()
        media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(ext)
        if abs_path.exists() and media_type:
            images.append((abs_path.read_bytes(), media_type))

    from src.pipeline.vision import analyze_thumbnail_reference_images
    try:
        style_prompt = analyze_thumbnail_reference_images(images)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Analyse des images impossible : {e}")

    channel.thumbnail_style = {"reference_image_paths": all_paths, "style_prompt": style_prompt}
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.post("/{channel_id}/thumbnail-concept/propose")
def propose_channel_thumbnail_concept(channel_id: str, payload: dict = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Invents ONE concrete, niche-appropriate thumbnail identity (illustration
    style, recurring subject/character, palette — see propose_thumbnail_concept)
    and renders a real preview image so the creator can actually see it before
    committing, instead of approving a text description blind. Pass
    {"rejected_concepts": [...]} (concept_name + style_prompt strings the
    creator already declined) to get something meaningfully different on a
    "propose another style" request rather than a palette shuffle of the same idea.
    Nothing is saved to the channel here — see the /approve endpoint for that."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    from src.utils.billing import user_ai_images_enabled, get_credit_balance
    if not user_ai_images_enabled(db, current_user):
        raise HTTPException(status_code=403, detail="Les fonctionnalités IA ne sont pas incluses dans ton abonnement actuel.")
    has_own_key = bool(current_user.izivoice_api_key_encrypted)
    if not has_own_key and get_credit_balance(db, current_user) <= 0:
        raise HTTPException(status_code=402, detail="Génération de miniature : solde de crédits insuffisant.")

    rejected_concepts = (payload or {}).get("rejected_concepts") or []
    recent_titles = [
        v.title for v in
        db.query(Video).filter(Video.channel_id == channel.id, Video.title.isnot(None))
        .order_by(Video.created_at.desc()).limit(5).all()
    ]

    from src.pipeline.youtube_metadata import propose_thumbnail_concept, generate_thumbnail
    try:
        concept = propose_thumbnail_concept(channel.niche, recent_titles, rejected_concepts)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proposition de style impossible : {e}")

    # Render a real preview using a throwaway channel-like view: a plain
    # object carrying just the fields _generate_ai_thumbnail_background reads,
    # with thumbnail_style set to the PROPOSED (not yet saved) concept — so
    # this preview call renders exactly what future thumbnails would look
    # like if approved, without writing anything to the real channel row.
    class _PreviewChannel:
        pass
    preview_channel = _PreviewChannel()
    preview_channel.thumbnail_style = {"style_prompt": concept["style_prompt"]}
    preview_channel.image_style = channel.image_style
    preview_channel.niche = channel.niche
    preview_channel.user_id = channel.user_id

    preview_dir = STORAGE_PATH / "channels" / channel.id / "thumbnail_references"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_dest = preview_dir / f"concept_preview_{uuid.uuid4().hex}.jpg"
    sample_title = recent_titles[0] if recent_titles else (channel.name or channel.niche or "Exemple de titre")
    try:
        # video_path is only ever touched by the ffmpeg-frame fallback, which
        # doesn't run when the AI background step below succeeds — there is
        # no real rendered video for a not-yet-generated preview.
        generate_thumbnail(preview_dir / "__no_video__.mp4", preview_dest, sample_title, channel=preview_channel)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Génération de l'aperçu impossible : {e}")

    return {
        "concept": concept,
        "preview_url": f"/api/channels/thumbnail-preview/{channel.id}/{preview_dest.name}",
    }


@router.get("/thumbnail-preview/{channel_id}/{filename}")
def get_thumbnail_concept_preview(channel_id: str, filename: str):
    path = STORAGE_PATH / "channels" / channel_id / "thumbnail_references" / filename
    if not path.exists() or not path.is_relative_to(STORAGE_PATH / "channels" / channel_id):
        raise HTTPException(status_code=404, detail="Aperçu introuvable.")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/{channel_id}/thumbnail-concept/approve")
def approve_channel_thumbnail_concept(channel_id: str, payload: dict, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Locks in a proposed concept as this channel's permanent thumbnail
    identity — every future thumbnail (generated right after a render, and
    at publish time as a fallback) already prioritizes channel.thumbnail_style
    over the generic per-video style, so writing it here is the only wiring
    needed for it to apply automatically to this channel going forward,
    without affecting any other channel or user."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    style_prompt = (payload or {}).get("style_prompt")
    if not style_prompt:
        raise HTTPException(status_code=400, detail="style_prompt manquant.")

    existing = dict(channel.thumbnail_style or {})
    existing["style_prompt"] = style_prompt
    existing["concept_name"] = (payload or {}).get("concept_name")
    existing["text_style"] = (payload or {}).get("text_style")
    channel.thumbnail_style = existing
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.delete("/{channel_id}/thumbnail-style")
def delete_channel_thumbnail_style(channel_id: str, image_path: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Without `image_path`: clears the whole thumbnail style (reverts to the default
    background source — video frame, or image_style's own prompt). With `image_path`:
    removes just that one reference image and re-analyzes the remaining ones."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    channel_dir = STORAGE_PATH / "channels" / channel.id
    existing_paths = _thumbnail_reference_paths(channel.thumbnail_style or {})

    if image_path is None:
        for rel_path in existing_paths:
            file_path = STORAGE_PATH / rel_path
            if file_path.exists() and file_path.is_relative_to(channel_dir):
                file_path.unlink(missing_ok=True)
        channel.thumbnail_style = None
        db.commit()
        db.refresh(channel)
        return channel.to_dict()

    if image_path not in existing_paths:
        raise HTTPException(status_code=404, detail="Image de référence introuvable.")
    file_path = STORAGE_PATH / image_path
    if file_path.exists() and file_path.is_relative_to(channel_dir):
        file_path.unlink(missing_ok=True)
    remaining_paths = [p for p in existing_paths if p != image_path]

    if not remaining_paths:
        channel.thumbnail_style = None
    else:
        images = []
        for rel_path in remaining_paths:
            abs_path = STORAGE_PATH / rel_path
            media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(abs_path.suffix.lower())
            if abs_path.exists() and media_type:
                images.append((abs_path.read_bytes(), media_type))
        from src.pipeline.vision import analyze_thumbnail_reference_images
        try:
            style_prompt = analyze_thumbnail_reference_images(images)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Analyse des images impossible : {e}")
        channel.thumbnail_style = {"reference_image_paths": remaining_paths, "style_prompt": style_prompt}

    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.post("/library-images/staging")
async def stage_channel_library_images(
    files: List[UploadFile] = File(...),
    staging_token: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Large folders (Cloudflare hard-caps a single request body at 100MB)
    arrive here in several requests: the first call gets no staging_token and
    starts a fresh batch; the frontend then repeats the call with that same
    token for every subsequent batch, appended into the same staging dir."""
    staging_root = STORAGE_PATH / "staging" / "channel-libraries"
    staging_root.mkdir(parents=True, exist_ok=True)

    # Opportunistic cleanup of abandoned uploads older than 24 hours.
    cutoff = time.time() - 86400
    for old_dir in staging_root.iterdir():
        if old_dir.is_dir() and old_dir.stat().st_mtime < cutoff:
            shutil.rmtree(old_dir, ignore_errors=True)

    if staging_token:
        try:
            token = str(uuid.UUID(staging_token))
        except ValueError:
            raise HTTPException(status_code=400, detail="Import temporaire invalide.")
        saved, rejected = await save_valid_library_images(files, staging_root / token, append=True)
        total = len([item for item in (staging_root / token).iterdir() if item.is_file()])
    else:
        token = str(uuid.uuid4())
        saved, rejected = await save_valid_library_images(files, staging_root / token)
        total = saved
    return {"staging_token": token, "library_image_count": total, "batch_saved": saved, "rejected_count": rejected}

@router.post("/{channel_id}/library-images/staging")
def attach_staged_channel_library(
    channel_id: str,
    staging_token: str = Form(...),
    share_with_community: bool = Form(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
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
    image_style["share_with_community"] = share_with_community
    channel.image_style = image_style
    _sync_community_library_folder(db, channel, share_with_community, saved)
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/library-images")
async def upload_channel_library_images(
    channel_id: str,
    files: List[UploadFile] = File(...),
    append: bool = Form(False),
    share_with_community: bool = Form(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    library_dir = STORAGE_PATH / "channels" / channel.id / "library"
    _, rejected = await save_valid_library_images(files, library_dir, append=append)
    saved = len([item for item in library_dir.iterdir() if item.is_file()]) if library_dir.is_dir() else 0

    image_style = dict(channel.image_style or {})
    image_style["library_path"] = f"channels/{channel.id}/library"
    image_style["library_image_count"] = saved
    image_style["library_rejected_count"] = rejected
    image_style["share_with_community"] = share_with_community
    channel.image_style = image_style
    _sync_community_library_folder(db, channel, share_with_community, saved)

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.get("/{channel_id}/youtube/auth-url")
def get_youtube_auth_url(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if not youtube_publisher.is_configured():
        raise HTTPException(status_code=503, detail="La connexion YouTube n'est pas configurée sur le serveur.")
    return {"auth_url": youtube_publisher.build_auth_url(channel_id)}


@router.get("/youtube/callback")
def youtube_oauth_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None, db: Session = Depends(get_db)):
    """Google redirects here after the creator grants (or denies) YouTube access."""
    def redirect_with(status_str: str, message: str = "", channel_id: str = None):
        from urllib.parse import quote
        params = f"youtube={status_str}"
        if message:
            params += f"&youtube_message={quote(message)}"
        if channel_id:
            params += f"&youtube_channel_id={quote(channel_id)}"
        return RedirectResponse(f"{FRONTEND_BASE_URL}/channels?{params}")

    if error:
        return redirect_with("error", error, channel_id=state)
    if not code or not state:
        return redirect_with("error", "Réponse OAuth incomplète.")

    channel = db.query(Channel).filter(Channel.id == state).first()
    if not channel:
        return redirect_with("error", "Chaîne introuvable.")

    try:
        tokens = youtube_publisher.exchange_code(code)
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            # Happens if the account already granted consent before without
            # "prompt=consent" forcing a fresh one — ask the creator to retry.
            return redirect_with("error", "Aucun jeton de rafraîchissement reçu. Réessaie la connexion YouTube.", channel_id=channel.id)

        channel_info = youtube_publisher.fetch_own_channel_info(access_token)

        channel.youtube_access_token = access_token
        channel.youtube_refresh_token = refresh_token
        channel.youtube_token_expiry = datetime.utcnow()
        channel.youtube_connected_at = datetime.utcnow()
        if channel_info:
            channel.youtube_channel_id = channel_info["id"]
            channel.youtube_channel_title = channel_info["title"]
            channel.youtube_channel_handle = channel_info.get("handle")
            channel.youtube_channel_thumbnail_url = channel_info.get("thumbnail_url")
            # Replace the placeholder identity set during setup with the
            # creator's real YouTube channel name, now that we know it.
            channel.name = channel_info["title"]
            # Only fill the description if the creator hasn't already written
            # their own — YouTube's "About" text is a reasonable starting
            # point, not something that should silently overwrite their input.
            if not channel.description:
                channel.description = channel_info.get("description") or None
            # If this channel has no logo yet, use the real YouTube avatar as
            # the video-overlay logo right away — a creator connecting their
            # real channel shouldn't have to separately hunt down and upload
            # a logo file just to see it on their videos. A manual upload
            # (now or later) always takes priority and is never overwritten.
            _fill_logo_from_youtube_avatar(channel, channel_info.get("thumbnail_url"))
            suggested_niche = _suggest_niche_for_channel(db, channel_info["title"], channel_info.get("description", ""))
            if suggested_niche:
                channel.niche = suggested_niche
        db.commit()
        return redirect_with("connected", channel_id=channel.id)
    except Exception as e:
        return redirect_with("error", str(e)[:200], channel_id=channel.id)


@router.post("/{channel_id}/youtube/refresh")
def refresh_youtube_identity(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Re-fetches the connected YouTube channel's name/handle/avatar — the
    creator may have renamed the channel or changed its photo directly on
    YouTube since the initial connection, and NicheCut only ever pulled that
    info once (at connect time) until now."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if not channel.youtube_refresh_token:
        raise HTTPException(status_code=409, detail="Cette chaîne n'est pas connectée à YouTube.")

    access_token = youtube_publisher.get_valid_access_token(channel)
    if not access_token:
        raise HTTPException(status_code=502, detail="Jeton YouTube expiré ou révoqué — reconnecte la chaîne.")

    channel_info = youtube_publisher.fetch_own_channel_info(access_token)
    if not channel_info:
        raise HTTPException(status_code=502, detail="Impossible de récupérer les informations de la chaîne YouTube.")

    channel.youtube_channel_id = channel_info["id"]
    channel.youtube_channel_title = channel_info["title"]
    channel.youtube_channel_handle = channel_info.get("handle")
    channel.youtube_channel_thumbnail_url = channel_info.get("thumbnail_url")
    channel.name = channel_info["title"]
    if not channel.description:
        channel.description = channel_info.get("description") or None
    _fill_logo_from_youtube_avatar(channel, channel_info.get("thumbnail_url"))
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.post("/{channel_id}/youtube/disconnect")
def disconnect_youtube(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    channel.youtube_access_token = None
    channel.youtube_refresh_token = None
    channel.youtube_token_expiry = None
    channel.youtube_connected_at = None
    channel.youtube_channel_id = None
    channel.youtube_channel_title = None
    channel.youtube_channel_handle = None
    channel.youtube_channel_thumbnail_url = None
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.delete("/{channel_id}")
def delete_channel(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    # Video cascades via the ORM relationship (Channel.videos,
    # cascade="all, delete-orphan"), but these three tables have a
    # channel_id foreign key with no cascade defined at all (ORM or DB) —
    # deleting a channel that had ever generated a script/video (so has
    # ApiUsageLog rows), cloned a voice, or shared a library folder was
    # silently failing with a 500 (FK violation) that the frontend's
    # "Supprimer" button never surfaced, since it ignores a non-ok response.
    from src.db.models import ApiUsageLog, VoiceCloneJob, CommunityLibraryFolder
    db.query(ApiUsageLog).filter(ApiUsageLog.channel_id == channel_id).delete()
    db.query(VoiceCloneJob).filter(VoiceCloneJob.channel_id == channel_id).delete()
    db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).delete()
    db.delete(channel)
    db.commit()
    return {"message": "Channel deleted successfully"}
