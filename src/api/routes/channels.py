from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
import random
import re
import shutil
import time
import uuid
import httpx
from src.db.session import get_db
from src.db.models import Channel, Video, User
from src.models.project import ChannelCreate, ChannelUpdate, VideoStatus, IzivoiceConnectionPayload
from src.config import STORAGE_PATH, IZIVOICE_API_KEY, IZIVOICE_BASE_URL, FRONTEND_BASE_URL
from fastapi.responses import RedirectResponse
from datetime import datetime
from src.pipeline import youtube_publisher
from src.pipeline.niche_detector import suggest_niche
from src.utils.credentials import encrypt_credential, izivoice_key_for_user

router = APIRouter(prefix="/api/channels", tags=["channels"])

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ALLOWED_LIBRARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

def _user_and_izivoice_key(db: Session, user_id: Optional[str]):
    user = db.query(User).filter(User.id == user_id).first() if user_id else None
    return user, izivoice_key_for_user(user) if user else IZIVOICE_API_KEY


@router.get("/izivoice/status")
def izivoice_status(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    return {"connected": bool(user.izivoice_api_key_encrypted), "key_prefix": user.izivoice_key_prefix, "mode": "personal" if user.izivoice_api_key_encrypted else "nichecut"}


@router.post("/izivoice/connect")
def connect_izivoice(payload: IzivoiceConnectionPayload, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
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
def disconnect_izivoice(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    user.izivoice_api_key_encrypted = None
    user.izivoice_key_prefix = None
    user.izivoice_connected_at = None
    db.commit()
    return {"connected": False, "key_prefix": None, "mode": "nichecut"}


@router.get("/voice/catalog")
def list_voice_catalog(language: Optional[str] = None, user_id: Optional[str] = None, db: Session = Depends(get_db)):
    _, api_key = _user_and_izivoice_key(db, user_id)
    if not api_key:
        raise HTTPException(status_code=503, detail="Le catalogue de voix n'est pas configuré.")
    # Izivoice's `page` is 0-indexed — page=1 silently returns the *second*
    # page (empty for most accounts), which is why the picker only ever
    # showed the 4 hardcoded fallback voices instead of the real catalog.
    all_voices = []
    with httpx.Client(timeout=30) as client:
        for page in range(0, 4):  # up to ~400 voices; plenty for a picker
            params = {"page": page, "page_size": 100}
            if language:
                params["language"] = language
            response = client.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {api_key}"}, params=params)
            response.raise_for_status()
            data = (response.json().get("data") or {})
            batch = data.get("voices") or []
            all_voices.extend(batch)
            if not data.get("has_more") or not batch:
                break
    return {"voices": all_voices}

@router.post("/{channel_id}/voice/clone")
async def clone_channel_voice(channel_id: str, name: str = Form(...), consent_confirmed: bool = Form(...), audio: UploadFile = File(...), user_id: Optional[str] = Form(None), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if not consent_confirmed:
        raise HTTPException(status_code=400, detail="Le consentement du propriétaire de la voix est obligatoire.")
    contents = await audio.read()
    if not contents:
        raise HTTPException(status_code=400, detail="L'échantillon audio est vide.")
    owner_id = user_id or channel.user_id
    _, api_key = _user_and_izivoice_key(db, owner_id)
    if not api_key:
        raise HTTPException(status_code=503, detail="Izivoice n'est pas configuré.")
    response = httpx.post(
        f"{IZIVOICE_BASE_URL}/clone",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (audio.filename or "voice-sample.wav", contents, audio.content_type or "audio/wav")},
        data={"name": name.strip(), "removeNoise": "true", "optimizeAccent": "true"},
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    voice_id = data.get("voice_id") or ((data.get("data") or {}).get("voice_id"))
    if not voice_id:
        raise HTTPException(status_code=502, detail="Izivoice n'a retourné aucun identifiant de voix.")
    return {"voice_id": voice_id, "name": name.strip(), "cloned": True}

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
        effects_config=payload.effects_config.model_dump(),
        automation_mode=payload.automation_mode or "manual",
        automation_style_prompt=payload.automation_style_prompt,
        videos_per_day=max(1, payload.videos_per_day or 1),
        automation_window_start_hour=payload.automation_window_start_hour if payload.automation_window_start_hour is not None else 7,
        automation_window_end_hour=payload.automation_window_end_hour if payload.automation_window_end_hour is not None else 11,
        active_days=payload.active_days,
        script_structure=payload.script_structure,
        voice_id=payload.voice_id,
        voice_name=payload.voice_name,
        voice_settings=payload.voice_settings,
        publish_mode=payload.publish_mode or "manual",
        publish_schedule_hour=payload.publish_schedule_hour,
        publish_schedule_day_offset=payload.publish_schedule_day_offset,
        timezone=payload.timezone or "Africa/Douala",
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
    if payload.automation_mode is not None:
        channel.automation_mode = payload.automation_mode
    if payload.automation_style_prompt is not None:
        channel.automation_style_prompt = payload.automation_style_prompt
    if payload.videos_per_day is not None:
        channel.videos_per_day = max(1, payload.videos_per_day)
    if payload.automation_window_start_hour is not None:
        channel.automation_window_start_hour = payload.automation_window_start_hour
    if payload.automation_window_end_hour is not None:
        channel.automation_window_end_hour = payload.automation_window_end_hour
    if payload.active_days is not None:
        channel.active_days = payload.active_days
    if payload.script_structure is not None:
        channel.script_structure = payload.script_structure
    if payload.voice_id is not None:
        channel.voice_id = payload.voice_id
    if payload.voice_name is not None:
        channel.voice_name = payload.voice_name
    if payload.voice_settings is not None:
        channel.voice_settings = payload.voice_settings
    if payload.publish_mode is not None:
        channel.publish_mode = payload.publish_mode
    if payload.publish_schedule_hour is not None:
        channel.publish_schedule_hour = payload.publish_schedule_hour
    if payload.publish_schedule_day_offset is not None:
        channel.publish_schedule_day_offset = payload.publish_schedule_day_offset
    if payload.timezone is not None:
        channel.timezone = payload.timezone

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/generate-now")
def generate_now(channel_id: str, db: Session = Depends(get_db)):
    """On-demand equivalent of the daily auto pipeline for a single channel:
    only valid for automation_mode == "auto", where a creator clicking
    "Nouvelle vidéo" should never see the manual script/voice form — the
    Agent picks the topic and writes the script itself, immediately."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.automation_mode != "auto":
        raise HTTPException(status_code=409, detail="Cette chaîne n'est pas en mode automatique.")

    from src.worker.queue_runner import generate_and_queue_auto_video
    video = generate_and_queue_auto_video(db, channel)
    if not video:
        raise HTTPException(status_code=502, detail="La génération du script a échoué. Réessayez dans un instant.")
    return video.to_dict()

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

@router.post("/{channel_id}/avatar")
async def upload_channel_avatar(channel_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Uploads the channel's app-facing profile picture — shown in channel cards,
    lists and the sidebar. Distinct from the logo (branding.logo_path), which is
    the high-quality asset burned into the rendered video and never resized."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (png, jpg, webp, gif, svg).")

    channel_dir = STORAGE_PATH / "channels" / channel.id
    channel_dir.mkdir(parents=True, exist_ok=True)

    for old_avatar in channel_dir.glob("avatar.*"):
        old_avatar.unlink(missing_ok=True)

    dest_file = channel_dir / f"avatar{ext}"
    contents = await file.read()
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
):
    """Generates a short AI music preview on the spot, so the client can listen
    to it in the wizard before saving the channel — same prompt path used at
    render time (Claude-written prompt, or the client's own override)."""
    if not IZIVOICE_API_KEY:
        raise HTTPException(status_code=503, detail="La génération musicale IA n'est pas configurée sur le serveur.")

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

ALLOWED_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}

@router.post("/{channel_id}/music")
async def upload_channel_music(channel_id: str, files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Uploads one or more of the client's own background tracks. One is picked at
    random per render — this is the channel's own music, never third-party stock."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

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
def delete_channel_music_track(channel_id: str, track_path: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

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
async def analyze_style_image(file: UploadFile = File(...)):
    """Analyzes a reference image and returns a reusable image-generation style prompt."""
    ext = Path(file.filename or "").suffix.lower()
    media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(ext)
    if not media_type:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (png, jpg, webp).")

    contents = await file.read()
    from src.pipeline.vision import analyze_reference_image
    try:
        style_prompt = analyze_reference_image(contents, media_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Analyse de l'image impossible : {e}")

    return {"style_prompt": style_prompt}

@router.post("/library-images/staging")
async def stage_channel_library_images(
    files: List[UploadFile] = File(...),
    staging_token: Optional[str] = Form(None),
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
async def upload_channel_library_images(
    channel_id: str,
    files: List[UploadFile] = File(...),
    append: bool = Form(False),
    db: Session = Depends(get_db),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    library_dir = STORAGE_PATH / "channels" / channel.id / "library"
    _, rejected = await save_valid_library_images(files, library_dir, append=append)
    saved = len([item for item in library_dir.iterdir() if item.is_file()]) if library_dir.is_dir() else 0

    image_style = dict(channel.image_style or {})
    image_style["library_path"] = f"channels/{channel.id}/library"
    image_style["library_image_count"] = saved
    image_style["library_rejected_count"] = rejected
    channel.image_style = image_style

    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.get("/{channel_id}/youtube/auth-url")
def get_youtube_auth_url(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
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
            suggested_niche = _suggest_niche_for_channel(db, channel_info["title"], channel_info.get("description", ""))
            if suggested_niche:
                channel.niche = suggested_niche
        db.commit()
        return redirect_with("connected", channel_id=channel.id)
    except Exception as e:
        return redirect_with("error", str(e)[:200], channel_id=channel.id)


@router.post("/{channel_id}/youtube/refresh")
def refresh_youtube_identity(channel_id: str, db: Session = Depends(get_db)):
    """Re-fetches the connected YouTube channel's name/handle/avatar — the
    creator may have renamed the channel or changed its photo directly on
    YouTube since the initial connection, and NicheCut only ever pulled that
    info once (at connect time) until now."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
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
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.post("/{channel_id}/youtube/disconnect")
def disconnect_youtube(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
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
def delete_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete(channel)
    db.commit()
    return {"message": "Channel deleted successfully"}
