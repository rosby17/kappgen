from pathlib import Path
from functools import lru_cache
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Dict, Any, Optional
import random
import json
import re
import hashlib
import shutil
import subprocess
import time
import uuid
import httpx
from urllib.parse import quote, urlparse
from src.db.session import get_db
from src.db.models import Channel, Video, User, VoiceCloneJob, CommunityLibraryFolder, CommunityLibraryImagePlacement, CommunityLibraryImageTag, Voice, ChannelPipelineShare, ChannelSoundEffect
from src.models.project import ChannelCreate, ChannelUpdate, VideoStatus, IzivoiceConnectionPayload, MusicPreference
from src.config import STORAGE_PATH, IZIVOICE_API_KEY, IZIVOICE_BASE_URL, FRONTEND_BASE_URL, IMAGE_UPLOAD_EXTENSIONS, HEIC_EXTENSIONS, PEXELS_API_KEY
from fastapi.responses import RedirectResponse
from datetime import datetime, timedelta
from src.pipeline import youtube_publisher
from src.pipeline.niche_detector import suggest_niche
from src.pipeline.script_structure_analyzer import analyze_script_structure_text
from src.utils.credentials import encrypt_credential, izivoice_key_for_user
from src.utils.auth import get_current_user
from src.utils.billing import user_has_purchased_credits
from src.utils.logger import logger

router = APIRouter(prefix="/api/channels", tags=["channels"])

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ALLOWED_LIBRARY_EXTENSIONS = IMAGE_UPLOAD_EXTENSIONS
ALLOWED_BROLL_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
MAX_IMAGE_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB
# api.kappgen.com is proxied through Cloudflare, whose own request-body cap
# sits around 100MB regardless of what this backend allows — a clip this
# server would happily accept still silently fails at the Cloudflare edge
# before ever reaching here. Capped here to match that real ceiling (with a
# small margin) so the error message a creator sees is actually true, and so
# a too-large file gets rejected quickly by this check rather than being
# accepted request-body-wise but still failing to actually arrive in
# practice. The frontend enforces the same 95MB limit client-side, before
# even attempting the upload (see CLOUDFLARE_UPLOAD_LIMIT_BYTES in App.jsx).
MAX_BROLL_UPLOAD_BYTES = 95 * 1024 * 1024  # 95 MB per creator clip


class VoiceSettingsPreviewRequest(BaseModel):
    """The four controls exposed in the voice step, used for a short TTS test."""
    speed: float = 0.845
    stability: float = 0.8
    similarity_boost: float = 0.9
    style: float = 0.0

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
        # Never delete a row the admin deliberately force-shared (see
        # admin.py's force-share endpoint) just because the owner's own
        # toggle is off — that toggle is their default/starting point, not a
        # hard block on the admin's own call, and this function runs on
        # every channel save, not just sharing-related ones.
        if existing and not existing.admin_forced:
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
    manually. Never overwrites a logo a creator uploaded by hand
    (branding.logo_source == "manual", set by the direct /logo upload
    endpoint) — but DOES refresh one this same function filled in earlier
    (logo_source == "youtube_auto") whenever the YouTube avatar URL has
    actually changed since. Without this, a channel connected before its
    owner had gotten around to setting a real profile picture on YouTube
    permanently kept whatever YouTube's own placeholder avatar looked like
    at that moment (often a plain circle+initial), even after a real photo
    was set later — this runs on every identity sync (every ~6h), so a
    changed avatar catches up within one cycle instead of never.

    Bug history: a channel connected before `logo_source` existed as a field
    has logo_path set but logo_source entirely absent (neither "manual" nor
    "youtube_auto") — `!= "youtube_auto"` treated that missing value exactly
    like an explicit "manual" and refused to ever touch it again, so a
    creator who disconnected the wrong YouTube channel and reconnected the
    right one kept the WRONG channel's avatar forever, with no way back
    short of deleting branding.logo_path by hand. Only an explicit "manual"
    now blocks the sync; unset is legacy auto-sync, not a protected upload.
    Mutates channel.branding in place; caller is responsible for the
    db.commit()."""
    if not thumbnail_url:
        return
    branding = dict(channel.branding or {})
    if branding.get("logo_path"):
        if branding.get("logo_source") == "manual":
            return  # a manually-uploaded logo is never touched
        if branding.get("youtube_avatar_synced_url") == thumbnail_url:
            return  # already synced to this exact avatar, nothing changed
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
    branding["logo_source"] = "youtube_auto"
    branding["youtube_avatar_synced_url"] = thumbnail_url
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
async def clone_channel_voice(channel_id: str, name: str = Form(...), gender: str = Form("neutral"), audio: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if channel.user_id != current_user.id and not current_user.is_admin:
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
        gender=gender if gender in ("male", "female") else "neutral",
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


@router.get("/my-cloned-voices")
def list_my_cloned_voices(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lists all personal clones available to the current creator.

    KappGen's VoiceCloneJob rows are durable ownership records for clones
    created in KappGen. They cannot, however, know about clones created
    directly in the creator's Izivoice account. When a personal Izivoice key
    is connected, merge `/voices?mine=true` as the authoritative external
    inventory so a new channel sees every existing clone, not only the ones
    created through this UI.
    """
    jobs = (
        db.query(VoiceCloneJob)
        .filter(VoiceCloneJob.user_id == current_user.id, VoiceCloneJob.status == "done", VoiceCloneJob.voice_id.isnot(None))
        .order_by(VoiceCloneJob.created_at.desc())
        .all()
    )
    seen = set()
    voices = []
    for job in jobs:
        if job.voice_id in seen:
            continue
        seen.add(job.voice_id)
        voices.append({"id": job.voice_id, "name": job.name, "gender": job.gender, "preview_url": f"/channels/voice/{job.voice_id}/preview" if job.preview_url else None})

    # Never query `mine=true` with KappGen's shared service key: that key is
    # not a creator identity and could expose another user's private clones.
    # A connected personal key is exactly the scope Izivoice needs here.
    if current_user.izivoice_api_key_encrypted:
        try:
            api_key = izivoice_key_for_user(current_user)
            page = 0
            with httpx.Client(timeout=20) as client:
                while True:
                    response = client.get(
                        f"{IZIVOICE_BASE_URL}/voices",
                        headers={"Authorization": f"Bearer {api_key}"},
                        params={"page": page, "page_size": 100, "mine": "true"},
                    )
                    if not response.is_success:
                        logger.warning("Izivoice clone sync failed for user %s: HTTP %s", current_user.id, response.status_code)
                        break
                    data = (response.json() or {}).get("data") or {}
                    batch = data.get("voices") or []
                    for voice in batch:
                        voice_id = str(voice.get("voice_id") or voice.get("id") or "").strip()
                        if not voice_id or voice_id in seen:
                            continue
                        seen.add(voice_id)
                        voices.append({
                            "id": voice_id,
                            "name": voice.get("name") or f"Voix {voice_id[:8]}",
                            "gender": voice.get("gender") or "neutral",
                            "preview_url": voice.get("preview_url"),
                        })
                    if not data.get("has_more") or not batch:
                        break
                    page += 1
                    if page >= 50:  # defensive ceiling against an upstream pagination loop
                        break
        except Exception as exc:  # Existing KappGen clones must remain usable offline.
            logger.warning("Izivoice clone sync failed for user %s: %s", current_user.id, exc)
    return {"voices": voices}


@router.delete("/my-cloned-voices/{voice_id}")
def delete_my_cloned_voice(voice_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Deletes a cloned voice: removes it from Izivoice itself (best-effort —
    an Izivoice-side failure doesn't block clearing it from KappGen, since a
    voice_id that no longer resolves there is useless here too) and from every
    VoiceCloneJob row that reference it, so it stops appearing in "Mes voix
    clonées". Refuses if any of the user's channels still has it selected as
    their active voiceover voice, to avoid silently breaking future renders —
    the creator has to pick a different voice on that channel first."""
    owns_it = (
        db.query(VoiceCloneJob)
        .filter(VoiceCloneJob.user_id == current_user.id, VoiceCloneJob.voice_id == voice_id)
        .first()
    )
    if not owns_it:
        raise HTTPException(status_code=404, detail="Voix clonée introuvable.")

    channels_using_it = (
        db.query(Channel)
        .filter(Channel.user_id == current_user.id, Channel.voice_id == voice_id)
        .all()
    )
    if channels_using_it:
        names = ", ".join(c.name for c in channels_using_it)
        raise HTTPException(
            status_code=409,
            detail=f"Cette voix est encore utilisée par : {names}. Choisis une autre voix sur cette/ces chaîne(s) avant de la supprimer.",
        )

    api_key = izivoice_key_for_user(current_user)
    if api_key:
        try:
            resp = httpx.delete(
                f"{IZIVOICE_BASE_URL}/clone/{voice_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30.0,
            )
            if resp.status_code not in (200, 204, 404):
                logger.warning(f"Izivoice refused deleting voice {voice_id}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Failed to delete voice {voice_id} on Izivoice ({e}) — clearing it from KappGen anyway.")

    db.query(VoiceCloneJob).filter(VoiceCloneJob.user_id == current_user.id, VoiceCloneJob.voice_id == voice_id).delete()
    db.commit()
    return {"status": "deleted"}


@router.get("/voice/{voice_id}/preview")
def get_voice_preview(voice_id: str, variant: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """Serves the short sample generated right after cloning (see
    _run_clone_job) — Izivoice's /clone itself never returns one."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise HTTPException(status_code=404, detail="Aperçu introuvable.")
    # A settings-aware preview is a separate cached file.  Keeping the
    # original no-query URL intact means previews generated after cloning and
    # the voice picker keep working exactly as before.
    if variant and not re.fullmatch(r"[a-f0-9]{16}", variant):
        raise HTTPException(status_code=404, detail="Aperçu introuvable.")
    suffix = f"_{variant}" if variant else ""
    preview_path = STORAGE_PATH / "voice_previews" / f"{voice_id}{suffix}.mp3"
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


@router.post("/voice/{voice_id}/preview/settings")
def generate_voice_settings_preview(
    voice_id: str,
    payload: VoiceSettingsPreviewRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate a short sample with the exact controls selected in the wizard.

    The result is cached by voice + settings, so clicking preview twice without
    changing a control does not trigger another Izivoice generation.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise HTTPException(status_code=400, detail="Identifiant de voix invalide.")
    api_key = izivoice_key_for_user(current_user)
    if not api_key:
        raise HTTPException(status_code=503, detail="Connecte d'abord ton compte Izivoice dans les paramètres.")

    settings = {
        "speed": round(min(1.5, max(0.5, payload.speed)), 2),
        "stability": round(min(1.0, max(0.0, payload.stability)), 2),
        "similarity_boost": round(min(1.0, max(0.0, payload.similarity_boost)), 2),
        "style": round(min(1.0, max(0.0, payload.style)), 2),
    }
    variant = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]
    preview_path = STORAGE_PATH / "voice_previews" / f"{voice_id}_{variant}.mp3"
    if not preview_path.exists():
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        from src.pipeline.voiceover import generate_voiceover
        try:
            # A preview must stay quick and must not pay for an unnecessary
            # second transcription pass just to obtain subtitle timings.
            generate_voiceover(
                VOICE_PREVIEW_TEXT,
                preview_path,
                voice_id=voice_id,
                api_key=api_key,
                voice_settings=settings,
                transcribe=False,
            )
        except Exception as exc:
            logger.warning(f"Voice settings preview generation failed for {voice_id}: {exc}")
            raise HTTPException(status_code=502, detail="Impossible de générer l'aperçu avec ces réglages.")
    return {"preview_url": f"/channels/voice/{voice_id}/preview?variant={variant}"}

def _write_library_image(dest_path: Path, ext: str, contents: bytes) -> None:
    """Writes an uploaded image's bytes to disk, converting HEIC/HEIF (the
    default photo format on iPhone since iOS 11) to JPEG first — nothing
    downstream (ffmpeg included) reliably reads raw HEIC, so accepting the
    extension without converting would just turn a clean rejection into a
    silently-broken library image at render time."""
    if ext in HEIC_EXTENSIONS:
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(contents)).convert("RGB")
        dest_path = dest_path.with_suffix(".jpg")
        image.save(dest_path, "JPEG", quality=92)
    else:
        dest_path.write_bytes(contents)


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
            try:
                _write_library_image(target_dir / f"img_{start_index + saved:04d}_{uuid.uuid4().hex[:8]}{ext}", ext, contents)
            except Exception:
                rejected += 1
                continue
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
            try:
                _write_library_image(incoming_dir / f"img_{saved:04d}{ext}", ext, contents)
            except Exception:
                rejected += 1
                continue
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


@lru_cache(maxsize=1)
def _installed_font_families() -> tuple[str, ...]:
    """Return the real family names known by the render container.

    The picker is deliberately backed by fontconfig rather than a large
    hard-coded list: every displayed family can therefore be resolved by the
    same renderer that burns the subtitles into the finished video.

    Filtered to families that cover French (``:lang=fr``) — the render
    container also ships full script coverage (Tamil, Thai, CJK, etc.) for
    non-Latin niches, which otherwise buries the picker under dozens of
    "Noto Sans <Script> UI <Weight>" variants nobody writing French
    subtitles would ever pick.
    """
    try:
        result = subprocess.run(
            ["fc-list", ":lang=fr", "--format=%{family}\n"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"Could not enumerate installed fonts: {exc}")
        return ()

    families: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # fontconfig may return several aliases for one face, comma-separated.
        for raw_name in line.split(","):
            name = re.sub(r"\s+", " ", raw_name).strip()
            if name and not name.startswith(".") and "noto" not in name.casefold():
                families.setdefault(name.casefold(), name)
    return tuple(sorted(families.values(), key=str.casefold))


@router.get("/fonts")
def list_render_fonts():
    """Font families actually available to subtitle rendering."""
    return {"fonts": list(_installed_font_families())}


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
        # "Published" treats a manual download as equivalent to a real YouTube
        # publish (the creator's own framing: both mean "this video has left
        # the queue and is out in the world") — either timestamp counts.
        published_filter = (
            Video.channel_id == c.id, Video.status == VideoStatus.DONE.value,
            or_(Video.youtube_published_at.isnot(None), Video.downloaded_at.isnot(None)),
        )
        data["published_count"] = db.query(Video).filter(*published_filter).count()
        data["unpublished_count"] = data["done_count"] - data["published_count"]
        data["total_count"] = db.query(Video).filter(Video.channel_id == c.id).count()
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
        youtube_topic_sources=payload.youtube_topic_sources,
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
        youtube_made_for_kids=bool(payload.youtube_made_for_kids),
        youtube_default_description=payload.youtube_default_description,
        youtube_default_tags=payload.youtube_default_tags or [],
        youtube_category_id=payload.youtube_category_id or "22",
        youtube_privacy_status=payload.youtube_privacy_status or "public",
        youtube_contains_synthetic_media=bool(payload.youtube_contains_synthetic_media),
        youtube_license=payload.youtube_license or "youtube",
        youtube_notify_subscribers=bool(payload.youtube_notify_subscribers),
        youtube_embeddable=bool(payload.youtube_embeddable),
        youtube_public_stats_viewable=bool(payload.youtube_public_stats_viewable),
        publish_time_mode=payload.publish_time_mode or "range",
        publish_schedule_hour=payload.publish_schedule_hour,
        publish_schedule_day_offset=payload.publish_schedule_day_offset,
        timezone=payload.timezone or "Africa/Douala",
        transcribe_audio_default=payload.transcribe_audio_default if payload.transcribe_audio_default is not None else True,
        thumbnail_style=payload.thumbnail_style,
        sfx_enabled=payload.sfx_enabled if payload.sfx_enabled is not None else True,
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
    # Admin channel management (the admin dashboard's "Gérer le pipeline"
    # action, reached from a video's owner) intentionally spans every
    # creator's channel; regular users remain restricted to their own.
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    data = channel.to_dict()
    data["queued_count"] = db.query(Video).filter(Video.channel_id == channel.id, Video.status == VideoStatus.QUEUED.value).count()
    data["rendering_count"] = db.query(Video).filter(Video.channel_id == channel.id, Video.status == VideoStatus.RENDERING.value).count()
    data["done_count"] = db.query(Video).filter(Video.channel_id == channel.id, Video.status == VideoStatus.DONE.value).count()
    data["published_count"] = db.query(Video).filter(
        Video.channel_id == channel.id, Video.status == VideoStatus.DONE.value,
        or_(Video.youtube_published_at.isnot(None), Video.downloaded_at.isnot(None)),
    ).count()
    data["unpublished_count"] = data["done_count"] - data["published_count"]
    data["total_count"] = db.query(Video).filter(Video.channel_id == channel.id).count()
    return data


# Same field set the in-account "Réutiliser le pipeline" duplicate copies in
# the frontend (openDuplicateWizard) — kept in sync deliberately: whatever's
# safe to hand a brand new channel of your own is exactly what's safe to
# hand a completely different account. Explicitly excludes: identity (name,
# description), the source's own YouTube connection, its cloned voice id,
# its logo file, and its analyzed thumbnail style — none of those can be
# shared as-is with someone else's channel.
def build_pipeline_share_template(channel: Channel) -> Dict[str, Any]:
    branding = dict(channel.branding or {})
    branding.pop("logo_path", None)
    return {
        "content_type": channel.content_type or "narration",
        "niche": channel.niche,
        "subtitle_style": channel.subtitle_style or {},
        "branding": branding,
        "music_preference": channel.music_preference or {},
        "image_style": channel.image_style or {},
        "effects_config": channel.effects_config or {},
        "automation_mode": channel.automation_mode or "manual",
        "automation_style_prompt": channel.automation_style_prompt,
        "topic_examples": channel.topic_examples,
        "use_web_trends": bool(channel.use_web_trends),
        "youtube_topic_sources": channel.youtube_topic_sources,
        "videos_per_day": channel.videos_per_day or 1,
        "automation_window_start_hour": channel.automation_window_start_hour if channel.automation_window_start_hour is not None else 7,
        "automation_window_end_hour": channel.automation_window_end_hour if channel.automation_window_end_hour is not None else 11,
        "active_days": channel.active_days,
        "script_generation_hour": channel.script_generation_hour,
        "script_generation_minute": channel.script_generation_minute or 0,
        "script_generation_second": channel.script_generation_second or 0,
        "script_generation_days": channel.script_generation_days,
        "timezone": channel.timezone or "Africa/Douala",
        "publish_mode": channel.publish_mode or "manual",
        "youtube_made_for_kids": bool(channel.youtube_made_for_kids),
        "youtube_default_description": channel.youtube_default_description,
        "youtube_default_tags": channel.youtube_default_tags or [],
        "youtube_category_id": channel.youtube_category_id or "22",
        "youtube_privacy_status": channel.youtube_privacy_status or "public",
        "youtube_contains_synthetic_media": channel.youtube_contains_synthetic_media is not False,
        "youtube_license": channel.youtube_license or "youtube",
        "youtube_notify_subscribers": channel.youtube_notify_subscribers is not False,
        "youtube_embeddable": channel.youtube_embeddable is not False,
        "youtube_public_stats_viewable": channel.youtube_public_stats_viewable is not False,
        "publish_time_mode": channel.publish_time_mode or "range",
        "publish_schedule_hour": channel.publish_schedule_hour if channel.publish_schedule_hour is not None else 8,
        "publish_schedule_day_offset": channel.publish_schedule_day_offset if channel.publish_schedule_day_offset is not None else 1,
        "script_structure": channel.script_structure,
        "voice_settings": channel.voice_settings,
    }


_PIPELINE_SHARE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L — read aloud/typed by hand
_PIPELINE_SHARE_TTL_DAYS = 30


@router.post("/{channel_id}/pipeline-share")
def create_pipeline_share(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generates a short redeemable code handing this channel's template
    settings to a DIFFERENT account (another director on the platform) —
    the cross-account counterpart to "Réutiliser le pipeline". The snapshot
    is frozen now, at share time: editing or deleting this channel later
    never changes what the code redeems, and redeeming it only pre-fills
    the recipient's own create-channel wizard once — the two channels are
    fully independent from that point on, by design (see the "Copie unique"
    choice made for this feature over a live-synced alternative)."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    code = "".join(random.choices(_PIPELINE_SHARE_CODE_ALPHABET, k=8))
    share = ChannelPipelineShare(
        code=code,
        channel_id=channel.id,
        owner_user_id=current_user.id,
        source_channel_name=channel.name,
        template=build_pipeline_share_template(channel),
        expires_at=datetime.utcnow() + timedelta(days=_PIPELINE_SHARE_TTL_DAYS),
    )
    db.add(share)
    db.commit()
    return {"code": code, "expires_at": share.expires_at.isoformat()}


@router.get("/pipeline-share/{code}")
def redeem_pipeline_share(code: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Any authenticated user can redeem a code — that's the whole point,
    it's meant to cross accounts. The code itself (8 chars from a
    36-character alphabet, shared out of band by the owner) is the only
    gate; not tied to who redeems it, and reusable until it expires."""
    share = db.query(ChannelPipelineShare).filter(ChannelPipelineShare.code == code.strip().upper()).first()
    if not share or share.revoked:
        raise HTTPException(status_code=404, detail="Code de partage introuvable ou révoqué.")
    if share.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Ce code de partage a expiré.")
    share.redeemed_count = (share.redeemed_count or 0) + 1
    db.commit()
    return {
        "source_channel_name": share.source_channel_name,
        "template": share.template,
    }


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
        .filter(CommunityLibraryFolder.status != "flagged", CommunityLibraryFolder.niche.ilike(niche))
        .all()
    )
    moved_count = db.query(CommunityLibraryImagePlacement).join(
        CommunityLibraryFolder,
        CommunityLibraryFolder.channel_id == CommunityLibraryImagePlacement.channel_id,
    ).filter(
        CommunityLibraryFolder.status != "flagged",
        CommunityLibraryImagePlacement.niche.ilike(niche),
        ~CommunityLibraryFolder.niche.ilike(niche),
    ).count()
    moved_out_count = db.query(CommunityLibraryImagePlacement).join(
        CommunityLibraryFolder,
        CommunityLibraryFolder.channel_id == CommunityLibraryImagePlacement.channel_id,
    ).filter(
        CommunityLibraryFolder.status != "flagged",
        CommunityLibraryFolder.niche.ilike(niche),
        ~CommunityLibraryImagePlacement.niche.ilike(niche),
    ).count()
    image_count = max(0, sum(f.image_count for f in folders) - moved_out_count) + moved_count
    return {
        # Pexels is the public source behind this option, so it is usable
        # even before another KappGen creator shares media for this niche.
        "available": bool(PEXELS_API_KEY) or image_count > 0,
        "folder_count": len(folders),
        "image_count": image_count,
    }


@router.get("/public-library/search")
def search_public_library(
    query: str = "",
    media_type: str = "photos",
    page: int = 1,
    per_page: int = 24,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Browse Pexels and creator-approved community images in one catalogue.

    Personal libraries remain private. Only images from folders explicitly
    shared *and approved* for the community are exposed here.
    """
    del current_user  # Authentication is required; no per-user data is read.
    if media_type not in {"photos", "videos"}:
        raise HTTPException(status_code=400, detail="Type de média invalide.")
    clean_query = " ".join((query or "").split())[:120]
    if not clean_query:
        clean_query = "cinematic background"
    safe_page = max(1, min(int(page or 1), 100))
    safe_per_page = max(8, min(int(per_page or 24), 40))
    # Community uploads are images today. Put them first so a creator can
    # actually discover the material that other KappGen creators shared,
    # rather than it being buried under a third-party catalogue.
    items = []
    if media_type == "photos":
        normalized_query = clean_query.casefold()
        query_words = {word for word in re.findall(r"\w+", normalized_query) if len(word) > 2}
        folders = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.status != "flagged").all()
        placements = {
            (placement.channel_id, placement.filename): placement.niche
            for placement in db.query(CommunityLibraryImagePlacement).join(
                CommunityLibraryFolder,
                CommunityLibraryFolder.channel_id == CommunityLibraryImagePlacement.channel_id,
            ).filter(CommunityLibraryFolder.status != "flagged").all()
        }
        # Real content keywords a background vision pass tagged each image
        # with (see queue_runner.py's tag_untagged_community_images) — lets a
        # search for what's literally IN the picture ("chessboard") find it
        # regardless of the sharing channel's niche wording. A freshly shared
        # image simply has none yet until that pass reaches it; niche
        # matching alone still covers it in the meantime.
        content_tags = {
            (tag.channel_id, tag.filename): set(json.loads(tag.tags_json) or [])
            for tag in db.query(CommunityLibraryImageTag).join(
                CommunityLibraryFolder,
                CommunityLibraryFolder.channel_id == CommunityLibraryImageTag.channel_id,
            ).filter(CommunityLibraryFolder.status != "flagged").all()
        }
        for folder in folders:
            library_dir = STORAGE_PATH / "channels" / folder.channel_id / "library"
            if not library_dir.is_dir():
                continue
            for asset in sorted(library_dir.iterdir(), key=lambda item: item.name, reverse=True):
                if not asset.is_file() or asset.suffix.lower() not in ALLOWED_LIBRARY_EXTENSIONS:
                    continue
                asset_niche = placements.get((folder.channel_id, asset.name), folder.niche)
                # Exact niche matching keeps category browsing useful, while
                # the substring fallback also supports a creator's own niche
                # wording (for example "astronomie et espace").
                niche_words = {word for word in re.findall(r"\w+", asset_niche.casefold()) if len(word) > 2}
                asset_tags = content_tags.get((folder.channel_id, asset.name), set())
                content_matches = bool(asset_tags) and any(
                    tag == normalized_query or tag in query_words or normalized_query in tag
                    for tag in asset_tags
                )
                niche_matches = (
                    normalized_query in {"", "cinematic background"}
                    or normalized_query in asset_niche.casefold()
                    or asset_niche.casefold() in normalized_query
                    or bool(query_words & niche_words)
                    or content_matches
                )
                if not niche_matches:
                    continue
                # The frontend prepends its own API_BASE, which already ends
                # in /api (https://api.kappgen.com/api) — a leading /api here
                # doubled it into /api/api/channels/... and 404'd every
                # community thumbnail, while Pexels items (whose URLs are
                # already absolute) loaded fine and hid the bug.
                asset_url = f"/channels/public-library/community/{folder.channel_id}/{quote(asset.name)}"
                # _persist_generated_images_to_channel_library (images.py) names
                # every scene image it auto-copies into a channel's shared
                # library "generated_N.ext" — a reliable, already-existing
                # marker for "KappGen's own AI made this", as opposed to
                # something the creator uploaded themselves. Surfaced as a
                # distinct provider so a browsing creator can tell which is
                # which, rather than both reading as generic "community".
                is_ai_generated = asset.name.startswith("generated_")
                items.append({
                    "id": f"community-{folder.channel_id}-{asset.name}",
                    "type": "photos",
                    "provider": "ai_generated" if is_ai_generated else "community",
                    # Small cached JPEG for the grid; the original (asset_url,
                    # unchanged) is what the preview/import/render actually use.
                    "thumbnail_url": f"{asset_url}/thumb",
                    "asset_url": asset_url,
                    "source_url": None,
                    "author": "KappGen AI" if is_ai_generated else "Communauté KappGen",
                    "author_url": None,
                    "width": None,
                    "height": None,
                    "duration": None,
                    "alt": asset_niche,
                    # Kept explicit (rather than parsed back out of `id`) so the
                    # import endpoint gets an unambiguous, unencoded source
                    # instead of re-splitting a hyphen-joined id string.
                    "source_channel_id": folder.channel_id,
                    "source_filename": asset.name,
                })
                if len(items) >= safe_per_page:
                    break
            if len(items) >= safe_per_page:
                break

    payload = {}
    if PEXELS_API_KEY and len(items) < safe_per_page:
        url = "https://api.pexels.com/v1/search" if media_type == "photos" else "https://api.pexels.com/videos/search"
        params = {"query": clean_query, "page": safe_page, "per_page": safe_per_page - len(items), "orientation": "landscape"}
        try:
            response = httpx.get(url, headers={"Authorization": PEXELS_API_KEY}, params=params, timeout=12.0)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Pexels public library search failed: %s", exc)
            if not items:
                raise HTTPException(status_code=502, detail="Impossible de charger les ressources publiques. Réessaie dans un instant.")

    if not PEXELS_API_KEY and not items:
        raise HTTPException(status_code=503, detail="La bibliothèque publique est momentanément indisponible.")

    raw_items = payload.get("photos", []) if media_type == "photos" else payload.get("videos", [])
    for item in raw_items:
        if media_type == "photos":
            source = item.get("src") or {}
            thumbnail = source.get("large") or source.get("medium") or source.get("original")
            author = item.get("photographer") or "Pexels"
            author_url = item.get("photographer_url")
            duration = None
        else:
            pictures = item.get("image") or ""
            files = item.get("video_files") or []
            playable = next((f.get("link") for f in files if f.get("quality") in {"sd", "hd"} and f.get("link")), None)
            thumbnail = pictures
            author = (item.get("user") or {}).get("name") or "Pexels"
            author_url = (item.get("user") or {}).get("url")
            duration = item.get("duration")
            source = {"original": playable}
        if not thumbnail:
            continue
        items.append({
            "id": str(item.get("id")),
            "type": media_type,
            "provider": "pexels",
            "thumbnail_url": thumbnail,
            "source_url": item.get("url"),
            "asset_url": source.get("original"),
            "author": author,
            "author_url": author_url,
            "width": item.get("width"),
            "height": item.get("height"),
            "duration": duration,
            "alt": item.get("alt") or clean_query,
        })
    return {"query": clean_query, "media_type": media_type, "page": safe_page, "per_page": safe_per_page, "items": items, "has_next": bool(payload.get("next_page"))}


def _resolve_approved_community_asset(db: Session, channel_id: str, filename: str) -> Path:
    folder = db.query(CommunityLibraryFolder).filter(
        CommunityLibraryFolder.channel_id == channel_id,
        CommunityLibraryFolder.status != "flagged",
    ).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Ressource publique introuvable.")
    library_dir = (STORAGE_PATH / "channels" / channel_id / "library").resolve()
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_LIBRARY_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Ressource publique introuvable.")
    return candidate


@router.get("/public-library/community/{channel_id}/{filename}")
def get_public_community_library_image(channel_id: str, filename: str, db: Session = Depends(get_db)):
    """Serve one approved shared image, full resolution — used for the
    expanded preview, the video render, and 'Ajouter à cette chaîne', never
    for the browsing grid (see the /thumb endpoint below for that)."""
    candidate = _resolve_approved_community_asset(db, channel_id, filename)
    return FileResponse(candidate, headers={"Cache-Control": "public, max-age=3600"})


_PUBLIC_LIBRARY_THUMB_MAX_WIDTH = 480
_PUBLIC_LIBRARY_THUMB_QUALITY = 70


@router.get("/public-library/community/{channel_id}/{filename}/thumb")
def get_public_community_library_thumbnail(channel_id: str, filename: str, db: Session = Depends(get_db)):
    """A small, cached JPEG for the browsing grid.

    The grid was serving each community image at its full render resolution
    — the same file the pipeline burns into a video — which is exactly why it
    loaded far slower than the Pexels rows above it (Pexels already returns a
    pre-sized 'large' variant for thumbnails). Resized once, cached to disk
    next to the original, and reused on every later request; the full-quality
    original is untouched and still what actually gets imported/rendered."""
    from PIL import Image
    import io

    candidate = _resolve_approved_community_asset(db, channel_id, filename)
    thumb_dir = candidate.parent / ".public_thumbs"
    thumb_path = thumb_dir / f"{candidate.name}.jpg"
    if not thumb_path.is_file() or thumb_path.stat().st_mtime < candidate.stat().st_mtime:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(candidate) as image:
            image = image.convert("RGB")
            if image.width > _PUBLIC_LIBRARY_THUMB_MAX_WIDTH:
                ratio = _PUBLIC_LIBRARY_THUMB_MAX_WIDTH / image.width
                image = image.resize((_PUBLIC_LIBRARY_THUMB_MAX_WIDTH, max(1, round(image.height * ratio))), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=_PUBLIC_LIBRARY_THUMB_QUALITY, optimize=True)
            thumb_path.write_bytes(buffer.getvalue())
    return FileResponse(thumb_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


class PublicLibraryImportPayload(BaseModel):
    provider: str  # "pexels" | "community"
    media_type: str  # "photos" | "videos"
    asset_url: Optional[str] = None  # required for provider="pexels"
    source_channel_id: Optional[str] = None  # required for provider="community"
    source_filename: Optional[str] = None  # required for provider="community"


# Only ever fetched server-side for a provider="pexels" import — restricting
# the host before making the request closes the SSRF hole a client-supplied
# URL would otherwise open (a creator's browser can only ever hand us a real
# Pexels asset_url to begin with, but the backend must not trust that alone).
_PEXELS_ASSET_HOSTS = {"images.pexels.com", "videos.pexels.com", "player.vimeo.com"}


@router.post("/{channel_id}/public-library/import")
async def import_public_library_asset(
    channel_id: str,
    payload: PublicLibraryImportPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Copies one Pexels or community-shared asset into a creator's OWN
    channel library/B-roll folder, so it's actually usable in a render — the
    public catalogue is browsable but was otherwise a dead end: nothing there
    fed the pipeline until a creator brought it into their own niche folder."""
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if payload.media_type not in {"photos", "videos"}:
        raise HTTPException(status_code=400, detail="Type de média invalide.")

    if payload.provider == "community":
        if not (payload.source_channel_id and payload.source_filename):
            raise HTTPException(status_code=400, detail="Ressource communautaire incomplète.")
        folder = db.query(CommunityLibraryFolder).filter(
            CommunityLibraryFolder.channel_id == payload.source_channel_id,
            CommunityLibraryFolder.status != "flagged",
        ).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Ressource communautaire introuvable.")
        source_dir = (STORAGE_PATH / "channels" / payload.source_channel_id / "library").resolve()
        source_path = (source_dir / payload.source_filename).resolve()
        if source_path.parent != source_dir or not source_path.is_file() or source_path.suffix.lower() not in ALLOWED_LIBRARY_EXTENSIONS:
            raise HTTPException(status_code=404, detail="Ressource communautaire introuvable.")
        contents = source_path.read_bytes()
        ext = source_path.suffix.lower()
    elif payload.provider == "pexels":
        if not payload.asset_url:
            raise HTTPException(status_code=400, detail="Ressource Pexels incomplète.")
        host = urlparse(payload.asset_url).hostname or ""
        if host not in _PEXELS_ASSET_HOSTS:
            raise HTTPException(status_code=400, detail="Source non autorisée.")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(payload.asset_url)
                resp.raise_for_status()
                contents = resp.content
        except httpx.HTTPError as exc:
            logger.warning("Public-library import fetch failed: %s", exc)
            raise HTTPException(status_code=502, detail="Téléchargement de la ressource impossible. Réessaie dans un instant.")
        guessed_ext = Path(urlparse(payload.asset_url).path).suffix.lower()
        ext = guessed_ext if payload.media_type == "photos" else (guessed_ext or ".mp4")
        if payload.media_type == "photos" and ext not in ALLOWED_LIBRARY_EXTENSIONS:
            ext = ".jpg"
        if payload.media_type == "videos" and ext not in ALLOWED_BROLL_EXTENSIONS:
            ext = ".mp4"
    else:
        raise HTTPException(status_code=400, detail="Fournisseur invalide.")

    if not contents:
        raise HTTPException(status_code=502, detail="Ressource vide, importation impossible.")

    if payload.media_type == "photos":
        if len(contents) > MAX_IMAGE_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail=f"Image trop volumineuse (max {MAX_IMAGE_UPLOAD_BYTES // (1024*1024)} Mo).")
        library_dir = STORAGE_PATH / "channels" / channel.id / "library"
        library_dir.mkdir(parents=True, exist_ok=True)
        existing = len(list(library_dir.glob("img_*")))
        _write_library_image(library_dir / f"img_{existing:04d}_{uuid.uuid4().hex[:8]}{ext}", ext, contents)
        style = dict(channel.image_style or {})
        style["library_image_count"] = len([f for f in library_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS])
        channel.image_style = style
    else:
        if len(contents) > MAX_BROLL_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail=f"Vidéo trop volumineuse (max {MAX_BROLL_UPLOAD_BYTES // (1024*1024)} Mo).")
        broll_dir = STORAGE_PATH / "channels" / channel.id / "broll"
        broll_dir.mkdir(parents=True, exist_ok=True)
        (broll_dir / f"{uuid.uuid4().hex[:8]}_public-library{ext}").write_bytes(contents)
        style = dict(channel.image_style or {})
        style["broll_path"] = f"channels/{channel.id}/broll"
        style["broll_count"] = len([f for f in broll_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_BROLL_EXTENSIONS])
        channel.image_style = style

    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.put("/{channel_id}")
def update_channel(channel_id: str, payload: ChannelUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    # Admin channel management (the admin dashboard's "Gérer le pipeline"
    # action) intentionally spans every creator's channel — lets an admin
    # pause/reconfigure a channel that's burning credits on unwanted auto
    # generations without needing the owner's permission or involvement.
    is_admin_edit = channel.user_id != current_user.id and current_user.is_admin
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    # Watermark removal is a lifetime unlock tied to having received real
    # credits at least once (purchase or admin grant) — without this,
    # anyone could just flip the toggle themselves, free-trial creators
    # included. Silently corrected rather than rejecting the whole update:
    # the render pipeline re-enforces this anyway (_channel_config_for_render
    # in queue_runner.py), so failing the entire channel save over one field
    # a client could have simply omitted would only be worse UX for no real
    # security benefit. Checked against the channel's real owner, not
    # whoever's actually making the request — an admin editing someone
    # else's channel has their own credit history, irrelevant here.
    if payload.effects_config is not None and not payload.effects_config.watermark_enabled:
        owner_for_credit_check = channel.user if is_admin_edit else current_user
        if not user_has_purchased_credits(db, owner_for_credit_check):
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
        image_style = payload.image_style.model_dump()
        # Permanent guard against a retired hardcoded default ("stoic
        # sculpture style, dark moody atmosphere") leaking onto a channel it
        # was never meant for. The style itself is legitimate — it's the
        # deliberate look for genuine Stoïcisme/Philosophie channels — the
        # bug was every NEW channel silently inheriting it regardless of
        # niche. The frontend no longer sends this itself, but a stale
        # cached page (an old browser tab that never reloaded the fixed
        # bundle) still could. So: only strip it when the channel's own
        # niche has nothing to do with philosophy/stoicism — a real
        # Stoïcisme channel deliberately keeping this style is untouched.
        niche_for_check = (payload.niche if payload.niche is not None else channel.niche) or ""
        style_prompt_lower = (image_style.get("style_prompt") or "").lower()
        niche_matches_stoic = bool(re.search(r"stoïc|stoic|philosophi", niche_for_check, re.IGNORECASE))
        if "stoic sculpture style" in style_prompt_lower and not niche_matches_stoic:
            image_style["style_prompt"] = ""
        channel.image_style = image_style
        # The wizard's general save can flip share_with_community on its own,
        # without going through a library upload — _sync_community_library_folder
        # was only ever called from the upload routes, so toggling "share"
        # here (the common path: sharing a library uploaded earlier) silently
        # updated the flag but never created the admin-visible curation row.
        _sync_community_library_folder(
            db, channel,
            bool(image_style.get("share_with_community")),
            int(image_style.get("library_image_count") or 0),
        )
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
    if payload.youtube_topic_sources is not None:
        channel.youtube_topic_sources = payload.youtube_topic_sources
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
    if payload.youtube_made_for_kids is not None:
        channel.youtube_made_for_kids = payload.youtube_made_for_kids
    for field in ("youtube_default_description", "youtube_category_id", "youtube_privacy_status", "youtube_license"):
        value = getattr(payload, field)
        if value is not None:
            setattr(channel, field, value)
    if payload.youtube_default_tags is not None:
        channel.youtube_default_tags = [str(tag).strip()[:100] for tag in payload.youtube_default_tags if str(tag).strip()][:30]
    for field in ("youtube_contains_synthetic_media", "youtube_notify_subscribers", "youtube_embeddable", "youtube_public_stats_viewable"):
        value = getattr(payload, field)
        if value is not None:
            setattr(channel, field, value)
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
    if payload.sfx_enabled is not None:
        channel.sfx_enabled = payload.sfx_enabled
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    from src.utils.billing import user_ai_script_enabled, user_can_render
    if not user_ai_script_enabled(db, current_user):
        raise HTTPException(
            status_code=403,
            detail="La génération automatique de script (IA) n'est pas incluse dans ton abonnement actuel. Passe à un palier supérieur pour l'utiliser.",
        )
    # Checked here, synchronously, instead of only inside the background
    # thread below: a quota/credit rejection there is only ever logged
    # server-side (generate_and_queue_auto_video_background swallows it into
    # a warning) — the frontend just polls for a new video, finds nothing
    # for 3 minutes, and gives up with no error shown at all. A creator
    # hitting their plan's video quota (or an empty credit balance) saw this
    # exactly as "the button stopped working" with zero explanation. Failing
    # fast here instead means the existing !res.ok toast in the frontend
    # actually fires.
    can_render, reason = user_can_render(db, current_user)
    if not can_render:
        raise HTTPException(status_code=402, detail=reason)

    # Do this before replying "C'est lancé". Previously this validation ran
    # only in the background worker; when a channel had no actual media in
    # its selected library/community source (or its AI visual option was not
    # available on the plan), the client briefly showed its optimistic card
    # and then removed it because no real Video row was ever created.
    # Returning the actionable validation message here makes the missing
    # source a configuration error the creator can fix, not a ghost launch.
    from src.api.routes.videos import validate_channel_visual_source
    validate_channel_visual_source(channel, db)

    from threading import Thread
    from src.worker.queue_runner import generate_and_queue_auto_video_background
    Thread(target=generate_and_queue_auto_video_background, args=(channel.id,), daemon=True).start()
    return {"status": "started"}

@router.post("/{channel_id}/logo")
async def upload_channel_logo(channel_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    # Marks this as a deliberate creator choice — _fill_logo_from_youtube_avatar
    # never overwrites a "manual" logo, even when the YouTube avatar changes later.
    branding["logo_source"] = "manual"
    branding.pop("youtube_avatar_synced_url", None)
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
        "size_percent": 10,
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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

# In-memory job store for AI music previews — Izivoice's /music call is a
# genuinely async task on their side (create + poll until "done") that
# commonly takes well past Cloudflare's fixed ~100s proxy timeout, which
# silently killed this request client-side with no error: the button stayed
# on "Génération…" forever, the creator had no idea whether it had worked,
# and — since the preview only flips music_preference.enabled to true on a
# *successful* response — the channel then saved with music silently
# disabled, so the rendered videos had no music at all despite the creator
# having "used" the AI generator. Same fix as voice cloning (see
# _clone_jobs above): return a job_id immediately, generate in the
# background, let the frontend poll.
_music_preview_jobs: Dict[str, Dict[str, Any]] = {}


def _run_music_preview_job(job_id: str, prompt: str, duration: float, tmp_path: Path):
    from src.pipeline.music import generate_music_izivoice
    try:
        generate_music_izivoice(prompt, duration, tmp_path)
        _music_preview_jobs[job_id] = {"status": "done", "path": str(tmp_path)}
    except Exception as e:
        _music_preview_jobs[job_id] = {"status": "error", "detail": f"Génération musicale impossible : {e}"}


@router.post("/preview-ai-music")
async def preview_ai_music(
    niche: str = Form(""),
    ai_prompt: Optional[str] = Form(None),
    script_excerpt: Optional[str] = Form(None),
    duration: float = Form(20.0),
    current_user: User = Depends(get_current_user),
):
    """Generates a short AI music preview so the client can listen to it in
    the wizard before saving the channel — same prompt path used at render
    time (Claude-written prompt, or the client's own override). Returns a
    job_id immediately; see /preview-ai-music/status/{job_id} for the result."""
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

    tmp_dir = STORAGE_PATH / "tmp" / "music-previews"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    tmp_path = tmp_dir / f"{job_id}.mp3"
    _music_preview_jobs[job_id] = {"status": "pending"}
    import threading
    threading.Thread(
        target=_run_music_preview_job,
        args=(job_id, prompt, max(5.0, min(duration, 30.0)), tmp_path),
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "pending"}


@router.get("/preview-ai-music/status/{job_id}")
def preview_ai_music_status(job_id: str, current_user: User = Depends(get_current_user)):
    job = _music_preview_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Tâche de génération introuvable ou expirée.")
    return {"status": job["status"], "detail": job.get("detail")}


@router.get("/preview-ai-music/file/{job_id}")
def preview_ai_music_file(job_id: str, current_user: User = Depends(get_current_user)):
    job = _music_preview_jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Aperçu musical introuvable ou pas encore prêt.")
    return FileResponse(job["path"], media_type="audio/mpeg", filename="preview.mp3")


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

    # Same async-job pattern as /preview-ai-music above — this call also
    # commonly runs past Cloudflare's ~100s proxy timeout if held open.
    tmp_dir = STORAGE_PATH / "tmp" / "music-previews"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    tmp_path = tmp_dir / f"{job_id}.mp3"
    _music_preview_jobs[job_id] = {"status": "pending"}
    import threading
    threading.Thread(
        target=_run_music_preview_job,
        args=(job_id, prompt, 20.0, tmp_path),
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "pending"}


MUSIC_STYLE_SUGGEST_INSTRUCTION = """You are a music supervisor helping someone with no music background set up a YouTube background-music channel. They need a "musical style" prompt that will drive an AI music generator for every future track on this channel.
{context_clause}
Respond with ONLY this JSON object, no other text:
{{
  "style_prompt": "a single dense, comma-separated music-generation prompt (no full sentences): genre, instrumentation, tempo/mood, and what kind of listening moment it fits (studying, sleeping, working, relaxing...) — concrete and specific, not vague adjectives alone",
  "title_examples": "3 example YouTube video titles for this style, one per line, in French, that sound like real videos in this niche (e.g. 'Lofi pour réviser toute la nuit')"
}}"""


@router.post("/music-style/suggest")
def suggest_music_style(payload: dict = None, current_user: User = Depends(get_current_user)):
    """Turns a vague or empty idea into a real, usable style_prompt + example
    titles for the Music Channel wizard — most creators setting one of these
    up have no music vocabulary ("lofi", "BPM", "ambient" mean nothing to
    them), so this is the same kind of guided assist as
    propose_thumbnail_concept, just for the one field this wizard actually
    can't function without."""
    hint = ((payload or {}).get("hint") or "").strip()[:500]
    if hint:
        context_clause = f'\nThe creator\'s own rough idea (correct/expand it, don\'t ignore it): "{hint}"\n'
    else:
        context_clause = "\nThey have no idea at all what they want — invent one broadly appealing, popular background-music channel concept (e.g. lofi study beats, deep sleep ambient, cozy piano, focus/productivity, nature sounds) with real commercial appeal on YouTube.\n"
    instruction = MUSIC_STYLE_SUGGEST_INSTRUCTION.format(context_clause=context_clause)
    try:
        from src.pipeline.ai_text import generate_text
        raw = generate_text(instruction, max_tokens=500, model="claude-sonnet-5", operation="music_style_suggest")
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"```$", "", text).strip()
        data = json.loads(text)
        if not data.get("style_prompt"):
            raise ValueError("missing style_prompt")
        return {"style_prompt": data["style_prompt"], "title_examples": data.get("title_examples") or ""}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Suggestion impossible : {e}")


ALLOWED_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
MAX_AUDIO_PREVIEW_BYTES = 30 * 1024 * 1024


@router.post("/preview-audio-mix")
async def preview_audio_mix(
    voice_sample: UploadFile = File(...),
    music_sample: Optional[UploadFile] = File(None),
    settings_json: str = Form(...),
    channel_id: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render a short preview with the exact same chain as final videos."""
    try:
        settings = MusicPreference.model_validate(json.loads(settings_json)).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Réglages audio invalides.") from exc

    voice_ext = Path(voice_sample.filename or "").suffix.lower()
    if voice_ext not in ALLOWED_MUSIC_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Extrait voix invalide (MP3, WAV, M4A ou OGG attendu).")
    voice_bytes = await voice_sample.read()
    if not voice_bytes or len(voice_bytes) > MAX_AUDIO_PREVIEW_BYTES:
        raise HTTPException(status_code=400, detail="Extrait voix vide ou trop volumineux (30 Mo maximum).")

    preview_dir = STORAGE_PATH / "tmp" / "studio-mix-previews" / uuid.uuid4().hex
    preview_dir.mkdir(parents=True, exist_ok=True)
    voice_path = preview_dir / f"voice{voice_ext}"
    voice_path.write_bytes(voice_bytes)
    music_path = None

    try:
        if music_sample and music_sample.filename:
            music_ext = Path(music_sample.filename).suffix.lower()
            if music_ext not in ALLOWED_MUSIC_EXTENSIONS:
                raise HTTPException(status_code=400, detail="Musique invalide (MP3, WAV, M4A ou OGG attendu).")
            music_bytes = await music_sample.read()
            if not music_bytes or len(music_bytes) > MAX_AUDIO_PREVIEW_BYTES:
                raise HTTPException(status_code=400, detail="Musique vide ou trop volumineuse (30 Mo maximum).")
            music_path = preview_dir / f"music{music_ext}"
            music_path.write_bytes(music_bytes)
        elif channel_id:
            channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
            if not channel:
                raise HTTPException(status_code=404, detail="Chaîne introuvable.")
            tracks = list((channel.music_preference or {}).get("tracks") or [])
            if tracks:
                candidate = (STORAGE_PATH / tracks[0]).resolve()
                allowed_dir = (STORAGE_PATH / "channels" / channel.id / "music").resolve()
                if candidate.exists() and candidate.is_relative_to(allowed_dir):
                    music_path = candidate
        if not music_path:
            raise HTTPException(status_code=400, detail="Ajoutez ou sélectionnez d’abord une musique pour la préécoute.")

        from src.pipeline.audio_mixer import mix_audio_tracks
        from src.utils.ffmpeg_runner import get_audio_duration, run_ffmpeg
        duration = get_audio_duration(voice_path)
        if duration <= 0:
            raise HTTPException(status_code=400, detail="L’extrait voix ne peut pas être lu.")
        # Keep previews quick while preserving the real processing character.
        if duration > 45:
            trimmed = preview_dir / "voice_trimmed.mp3"
            run_ffmpeg(["ffmpeg", "-y", "-i", str(voice_path), "-t", "45", "-c:a", "libmp3lame", "-b:a", "192k", str(trimmed)])
            voice_path = trimmed

        output_path = preview_dir / "studio-preview.mp3"
        mix_audio_tracks(
            voiceover_path=voice_path,
            music_path=music_path,
            output_audio_path=output_path,
            music_volume=settings.get("volume", 0.10),
            processing=settings,
        )
    except HTTPException:
        shutil.rmtree(preview_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(preview_dir, ignore_errors=True)
        logger.warning(f"Studio audio preview failed: {exc}")
        raise HTTPException(status_code=502, detail="Impossible de produire l’aperçu audio.") from exc

    return FileResponse(
        output_path,
        media_type="audio/mpeg",
        filename="apercu-mixage-kappgen.mp3",
        background=BackgroundTask(shutil.rmtree, preview_dir, ignore_errors=True),
    )

@router.post("/{channel_id}/music")
async def upload_channel_music(channel_id: str, files: List[UploadFile] = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Uploads one or more of the client's own background tracks. One is picked at
    random per render — this is the channel's own music, never third-party stock."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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

@router.get("/{channel_id}/sfx")
def list_channel_sound_effects(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    effects = (
        db.query(ChannelSoundEffect)
        .filter(ChannelSoundEffect.channel_id == channel_id)
        .order_by(ChannelSoundEffect.created_at.desc())
        .all()
    )
    return [e.to_dict() for e in effects]


@router.post("/{channel_id}/sfx")
async def upload_channel_sound_effects(
    channel_id: str,
    files: List[UploadFile] = File(...),
    labels: List[str] = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Uploads one or more short SFX clips with a creator-given label each
    (e.g. "whoosh transition", "notification ding") — the matching pass
    (src/pipeline/sound_effects.py) has no way to hear the clip, so this
    label is the only signal it has to decide when a given effect fits a
    moment in the transcript. `labels` must be the same length as `files`,
    in the same order — the frontend pairs each dropped file with its own
    label input before submitting."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if len(labels) != len(files):
        raise HTTPException(status_code=400, detail="Chaque fichier doit avoir une description.")

    sfx_dir = STORAGE_PATH / "channels" / channel.id / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)

    from src.utils.ffmpeg_runner import get_audio_duration

    saved = []
    for file, label in zip(files, labels):
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_MUSIC_EXTENSIONS:
            continue
        label = label.strip()[:200]
        if not label:
            continue
        stem = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(file.filename or "sfx").stem)[:60] or "sfx"
        dest_name = f"{uuid.uuid4().hex[:8]}_{stem}{ext}"
        dest_path = sfx_dir / dest_name
        dest_path.write_bytes(await file.read())
        try:
            duration = get_audio_duration(dest_path)
        except Exception:
            duration = None
        effect = ChannelSoundEffect(channel_id=channel.id, filename=dest_name, label=label, duration_seconds=duration)
        db.add(effect)
        saved.append(effect)

    if not saved:
        raise HTTPException(status_code=400, detail="Aucun fichier audio valide avec une description (mp3, wav, m4a, ogg).")

    db.commit()
    return [e.to_dict() for e in saved]


@router.delete("/{channel_id}/sfx/{sfx_id}")
def delete_channel_sound_effect(channel_id: str, sfx_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    effect = db.query(ChannelSoundEffect).filter(ChannelSoundEffect.id == sfx_id, ChannelSoundEffect.channel_id == channel_id).first()
    if not effect:
        raise HTTPException(status_code=404, detail="Effet sonore introuvable.")
    file_path = STORAGE_PATH / "channels" / channel.id / "sfx" / effect.filename
    file_path.unlink(missing_ok=True)
    db.delete(effect)
    db.commit()
    return {"status": "ok"}


ALLOWED_STYLE_REFERENCE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

@router.post("/analyze-style-image")
async def analyze_style_image(file: Optional[UploadFile] = File(None), files: Optional[List[UploadFile]] = File(None), current_user: User = Depends(get_current_user)):
    """Analyzes one or more visual references into a reusable, text-free scene style."""
    candidates = list(files or []) or ([file] if file else [])
    if not candidates:
        raise HTTPException(status_code=400, detail="Ajoute au moins une image.")
    images = []
    for candidate in candidates:
        ext = Path(candidate.filename or "").suffix.lower()
        media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(ext)
        if not media_type:
            raise HTTPException(status_code=400, detail=f"Format d'image non supporté pour {candidate.filename} (png, jpg, webp).")
        contents = await candidate.read()
        validate_uploaded_image(contents, ext, candidate.filename or "")
        images.append((contents, media_type))

    from src.pipeline.vision import analyze_reference_images
    try:
        style_prompt = analyze_reference_images(images)
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


def _analyze_thumbnail_style_background(channel_id: str, all_paths: List[str]) -> None:
    """Runs the actual (multi-provider, up to ~75s worst case) vision analysis
    off the request thread — see upload_channel_thumbnail_style below for why."""
    from src.db.session import SessionLocal
    from src.pipeline.vision import analyze_thumbnail_reference_profile
    db = SessionLocal()
    try:
        images = []
        for rel_path in all_paths:
            abs_path = STORAGE_PATH / rel_path
            ext = abs_path.suffix.lower()
            media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(ext)
            if abs_path.exists() and media_type:
                images.append((abs_path.read_bytes(), media_type))

        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        if not channel:
            return
        try:
            profile = analyze_thumbnail_reference_profile(images)
        except Exception as e:
            previous = dict(channel.thumbnail_style or {})
            previous["analyzing"] = False
            previous["analysis_error"] = f"Analyse des images impossible : {e}"
            channel.thumbnail_style = previous
            db.commit()
            return

        previous = dict(channel.thumbnail_style or {})
        previous.update({
            "reference_image_paths": all_paths,
            "style_prompt": profile["style_prompt"],
            "text_side": profile.get("text_side") or previous.get("text_side"),
            "analysis_summary": profile.get("analysis_summary"),
            "character_anchor": profile.get("character_anchor") or previous.get("character_anchor"),
            "typography_prompt": profile.get("typography_style") or previous.get("typography_prompt"),
            "analyzing": False,
            "analysis_error": None,
        })
        channel.thumbnail_style = previous
        image_style = dict(channel.image_style or {})
        image_style["generate_thumbnail_with_ai"] = True
        channel.image_style = image_style
        db.commit()
    finally:
        db.close()


@router.post("/{channel_id}/thumbnail-style")
async def upload_channel_thumbnail_style(channel_id: str, files: List[UploadFile] = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Saves one or more thumbnail-specific reference images for this channel
    and starts re-analyzing ALL of them together (existing + newly uploaded)
    into a single reusable style prompt — kept separate from image_style,
    since the thumbnail look is often deliberately different from the
    video's own body-image style.

    Only saves the files and starts the analysis, returning immediately —
    the vision call falls back across up to 3 providers (~75s worst case),
    and multiple large images take real time just to upload/decode on top of
    that, well past what Cloudflare's edge proxy holds a request open for;
    the browser previously saw the cut connection as a bare "Failed to
    fetch" once several reference images were added at once. The frontend
    polls /{channel_id}/thumbnail-style/status instead of awaiting this
    response."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    if (channel.thumbnail_style or {}).get("analyzing"):
        raise HTTPException(status_code=409, detail="Une analyse est déjà en cours pour cette chaîne.")

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
    previous = dict(channel.thumbnail_style or {})
    previous["analyzing"] = True
    previous["analysis_error"] = None
    channel.thumbnail_style = previous
    db.commit()
    db.refresh(channel)

    import threading
    threading.Thread(target=_analyze_thumbnail_style_background, args=(channel_id, all_paths), daemon=True).start()
    return channel.to_dict()


@router.get("/{channel_id}/thumbnail-style/status")
def get_thumbnail_style_status(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    style = channel.thumbnail_style or {}
    return {
        "analyzing": bool(style.get("analyzing")),
        "analysis_error": style.get("analysis_error"),
        "channel": channel.to_dict(),
    }


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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    preview_channel.thumbnail_style = {
        "style_prompt": concept["style_prompt"],
        "font_family": concept.get("font_family"),
        "accent_hex": concept.get("accent_hex"),
        "text_position": concept.get("text_position"),
        "text_side": concept.get("text_side"),
        "niche_examples": concept.get("niche_examples", []),
    }
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
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    style_prompt = (payload or {}).get("style_prompt")
    if not style_prompt:
        raise HTTPException(status_code=400, detail="style_prompt manquant.")

    existing = dict(channel.thumbnail_style or {})
    existing["style_prompt"] = style_prompt
    existing["concept_name"] = (payload or {}).get("concept_name")
    existing["text_style"] = (payload or {}).get("text_style")
    existing["font_family"] = (payload or {}).get("font_family")
    existing["accent_hex"] = (payload or {}).get("accent_hex")
    existing["text_position"] = (payload or {}).get("text_position")
    existing["text_side"] = (payload or {}).get("text_side")
    existing["niche_examples"] = (payload or {}).get("niche_examples") or []
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
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    channel_dir = STORAGE_PATH / "channels" / channel.id
    existing_paths = _thumbnail_reference_paths(channel.thumbnail_style or {})

    if image_path is None:
        for rel_path in existing_paths:
            file_path = STORAGE_PATH / rel_path
            if file_path.exists() and file_path.is_relative_to(channel_dir):
                file_path.unlink(missing_ok=True)
        channel.thumbnail_style = None
        image_style = dict(channel.image_style or {})
        image_style["generate_thumbnail_with_ai"] = False
        channel.image_style = image_style
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
        image_style = dict(channel.image_style or {})
        image_style["generate_thumbnail_with_ai"] = False
        channel.image_style = image_style
    else:
        images = []
        for rel_path in remaining_paths:
            abs_path = STORAGE_PATH / rel_path
            media_type = ALLOWED_STYLE_REFERENCE_EXTENSIONS.get(abs_path.suffix.lower())
            if abs_path.exists() and media_type:
                images.append((abs_path.read_bytes(), media_type))
        from src.pipeline.vision import analyze_thumbnail_reference_profile
        try:
            profile = analyze_thumbnail_reference_profile(images)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Analyse des images impossible : {e}")
        # Keep the rest of the identity (typography, character, text side) —
        # removing one reference re-derives the look, it doesn't reset the
        # channel's thumbnail identity to a bare style string.
        rebuilt = dict(channel.thumbnail_style or {})
        rebuilt.update({
            "reference_image_paths": remaining_paths,
            "style_prompt": profile["style_prompt"],
            "text_side": profile.get("text_side") or rebuilt.get("text_side"),
            "character_anchor": profile.get("character_anchor") or rebuilt.get("character_anchor"),
            "typography_prompt": profile.get("typography_style") or rebuilt.get("typography_prompt"),
            "analysis_summary": profile.get("analysis_summary") or rebuilt.get("analysis_summary"),
        })
        channel.thumbnail_style = rebuilt
        image_style = dict(channel.image_style or {})
        image_style["generate_thumbnail_with_ai"] = True
        channel.image_style = image_style

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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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


# --- Creator-facing library management --------------------------------------
# What a creator uploaded to the server (image folders, background music) and
# a way to actually delete it — distinct from the admin community-library
# tools above, this is scoped to the current user's own channels only, no
# admin rights needed. Video-attached audio (a script-upload's source file)
# is not covered here; it's tied to that video's own lifecycle and cleaned up
# when the video itself is deleted, not a standalone asset a creator manages.

@router.get("/library/overview")
def get_library_overview(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channels = db.query(Channel).filter(Channel.user_id == current_user.id).order_by(Channel.name.asc()).all()
    overview = []
    for channel in channels:
        library_dir = STORAGE_PATH / "channels" / channel.id / "library"
        image_count = 0
        if library_dir.is_dir():
            image_count = len([f for f in library_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS])
        tracks = list((channel.music_preference or {}).get("tracks") or [])
        branding = channel.branding or {}
        broll_dir = STORAGE_PATH / "channels" / channel.id / "broll"
        broll_count = len([f for f in broll_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_BROLL_EXTENSIONS]) if broll_dir.is_dir() else 0
        overview.append({
            "channel_id": channel.id,
            "channel_name": channel.name,
            "niche": channel.niche,
            "image_count": image_count,
            "music_track_count": len(tracks),
            # The library is a channel list too, so expose the same visual
            # identity used by the rest of the app instead of forcing the UI
            # to invent an initials-only placeholder.
            "avatar_path": branding.get("avatar_path"),
            "logo_path": branding.get("logo_path"),
            "youtube_channel_thumbnail_url": channel.youtube_channel_thumbnail_url,
            "broll_count": broll_count,
        })
    return {"channels": overview}


@router.get("/{channel_id}/library/images")
def list_my_channel_library_images(
    channel_id: str, offset: int = 0, limit: int = 60,
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    library_dir = STORAGE_PATH / "channels" / channel_id / "library"
    if not library_dir.is_dir():
        return {"filenames": [], "total": 0, "offset": 0, "has_more": False}
    all_filenames = sorted(
        (item.name for item in library_dir.iterdir() if item.is_file() and item.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS),
        reverse=True,
    )
    offset = max(0, offset)
    limit = max(1, min(limit, 120))
    filenames = all_filenames[offset:offset + limit]
    return {
        "filenames": filenames,
        "total": len(all_filenames),
        "offset": offset,
        "has_more": offset + len(filenames) < len(all_filenames),
    }


@router.get("/{channel_id}/library/images/{filename}")
def get_my_channel_library_image(channel_id: str, filename: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    library_dir = (STORAGE_PATH / "channels" / channel_id / "library").resolve()
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable.")
    return FileResponse(candidate)


@router.post("/{channel_id}/broll")
async def upload_channel_broll(channel_id: str, files: List[UploadFile] = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Store creator-provided B-roll clips. No AI generation or paid provider is involved."""
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    broll_dir = STORAGE_PATH / "channels" / channel.id / "broll"
    broll_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    rejected = 0
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        contents = await file.read()
        if ext not in ALLOWED_BROLL_EXTENSIONS or not contents or len(contents) > MAX_BROLL_UPLOAD_BYTES:
            rejected += 1
            continue
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(file.filename or "broll").stem)[:60] or "broll"
        (broll_dir / f"{uuid.uuid4().hex[:8]}_{stem}{ext}").write_bytes(contents)
        saved += 1
    if not saved:
        raise HTTPException(status_code=400, detail="Aucun clip vidéo valide (MP4, MOV, WebM ou M4V, 95 Mo max).")
    style = dict(channel.image_style or {})
    style["broll_path"] = f"channels/{channel.id}/broll"
    style["broll_count"] = len([f for f in broll_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_BROLL_EXTENSIONS])
    style["broll_rejected_count"] = rejected
    channel.image_style = style
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


MAX_BROLL_DIRECT_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 Go — the R2 direct-PUT path, for clips too big for the Cloudflare-proxied endpoint above.


class BrollDirectUploadStart(BaseModel):
    filename: str
    content_type: Optional[str] = "video/mp4"


@router.post("/{channel_id}/broll/direct-upload/start")
def start_channel_broll_direct_upload(channel_id: str, payload: BrollDirectUploadStart, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """For B-roll clips too large for our own API's Cloudflare-proxied request
    body cap (~100 Mo) — the browser PUTs the file straight to R2 using the
    URL returned here, never touching api.kappgen.com for the file bytes.
    Once uploaded, call /direct-upload/confirm to pull it onto local disk so
    the rest of the pipeline (which only knows local B-roll paths) needs no
    changes at all."""
    from src.utils import b2_storage
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    ext = Path(payload.filename or "").suffix.lower()
    if ext not in ALLOWED_BROLL_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format non supporté (MP4, MOV, WebM ou M4V).")
    if not b2_storage.is_b2_configured():
        raise HTTPException(status_code=503, detail="L'envoi direct de gros fichiers n'est pas disponible pour le moment.")
    object_key = f"staging/broll/{channel.id}/{uuid.uuid4().hex}{ext}"
    upload_url = b2_storage.presigned_put_url(object_key, content_type=payload.content_type or "video/mp4")
    if not upload_url:
        raise HTTPException(status_code=502, detail="Impossible de préparer l'envoi direct. Réessaie.")
    return {"upload_url": upload_url, "object_key": object_key}


class BrollDirectUploadConfirm(BaseModel):
    object_key: str
    filename: Optional[str] = None


@router.post("/{channel_id}/broll/direct-upload/confirm")
def confirm_channel_broll_direct_upload(channel_id: str, payload: BrollDirectUploadConfirm, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Called once the browser's direct PUT to R2 has finished. Downloads the
    object from R2 into the channel's normal local B-roll folder (a
    server-to-R2 transfer, not routed through Cloudflare's inbound proxy, so
    the size cap doesn't apply here either) then deletes the R2 staging copy
    — after this the clip is a completely ordinary local B-roll file, so
    nothing downstream (orchestrator, list/delete endpoints) needs to know
    it ever went through R2."""
    from src.utils import b2_storage
    from src.config import B2_PUBLIC_URL_BASE
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    object_key = (payload.object_key or "").strip()
    if not object_key.startswith(f"staging/broll/{channel.id}/"):
        raise HTTPException(status_code=400, detail="Référence d'envoi invalide.")
    ext = Path(object_key).suffix.lower()
    if ext not in ALLOWED_BROLL_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format non supporté.")
    broll_dir = STORAGE_PATH / "channels" / channel.id / "broll"
    broll_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(payload.filename or "broll").stem)[:60] or "broll"
    dest = broll_dir / f"{uuid.uuid4().hex[:8]}_{stem}{ext}"
    public_url = f"{B2_PUBLIC_URL_BASE}/{object_key}"
    total = 0
    try:
        with httpx.stream("GET", public_url, timeout=300.0) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_BROLL_DIRECT_UPLOAD_BYTES:
                        raise ValueError("too_large")
                    f.write(chunk)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        b2_storage.delete_video(object_key)
        detail = "Fichier trop volumineux (2 Go max)." if str(exc) == "too_large" else "Échec de la récupération du fichier envoyé. Réessaie."
        raise HTTPException(status_code=400 if str(exc) == "too_large" else 502, detail=detail)
    b2_storage.delete_video(object_key)
    style = dict(channel.image_style or {})
    style["broll_path"] = f"channels/{channel.id}/broll"
    style["broll_count"] = len([f for f in broll_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_BROLL_EXTENSIONS])
    channel.image_style = style
    db.commit()
    db.refresh(channel)
    return channel.to_dict()


@router.get("/{channel_id}/broll")
def list_channel_broll(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    directory = STORAGE_PATH / "channels" / channel.id / "broll"
    files = sorted([f.name for f in directory.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_BROLL_EXTENSIONS], reverse=True) if directory.is_dir() else []
    return {"filenames": files, "total": len(files)}


@router.get("/{channel_id}/broll/{filename}")
def get_channel_broll(channel_id: str, filename: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    directory = (STORAGE_PATH / "channels" / channel.id / "broll").resolve()
    candidate = (directory / filename).resolve()
    if candidate.parent != directory or not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_BROLL_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Clip introuvable.")
    return FileResponse(candidate)


@router.delete("/{channel_id}/broll/{filename}")
def delete_channel_broll(channel_id: str, filename: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id, Channel.user_id == current_user.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    directory = (STORAGE_PATH / "channels" / channel.id / "broll").resolve()
    candidate = (directory / filename).resolve()
    if candidate.parent != directory or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Clip introuvable.")
    candidate.unlink()
    style = dict(channel.image_style or {})
    style["broll_count"] = max(0, int(style.get("broll_count") or 1) - 1)
    channel.image_style = style
    db.commit()
    return {"deleted": True, "broll_count": style["broll_count"]}


@router.delete("/{channel_id}/library/images/{filename}")
def delete_my_channel_library_image(channel_id: str, filename: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from src.db.models import CommunityLibraryFolder, CommunityLibraryImagePlacement
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    library_dir = (STORAGE_PATH / "channels" / channel_id / "library").resolve()
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable.")
    candidate.unlink()
    db.query(CommunityLibraryImagePlacement).filter(
        CommunityLibraryImagePlacement.channel_id == channel_id,
        CommunityLibraryImagePlacement.filename == filename,
    ).delete(synchronize_session=False)
    image_style = dict(channel.image_style or {})
    new_count = max(0, int(image_style.get("library_image_count") or 0) - 1)
    image_style["library_image_count"] = new_count
    channel.image_style = image_style
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
    if folder:
        folder.image_count = new_count
    db.commit()
    return {"deleted": True, "image_count": new_count}


@router.delete("/{channel_id}/library/images")
def delete_all_my_channel_library_images(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Wipes the whole folder in one call — the "efface tout ce que j'ai
    uploadé" case, instead of forcing a click-per-image loop client-side."""
    from src.db.models import CommunityLibraryFolder, CommunityLibraryImagePlacement
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    if channel.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    library_dir = STORAGE_PATH / "channels" / channel_id / "library"
    deleted = 0
    if library_dir.is_dir():
        for item in list(library_dir.iterdir()):
            if item.is_file() and item.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS:
                item.unlink()
                deleted += 1
    db.query(CommunityLibraryImagePlacement).filter(CommunityLibraryImagePlacement.channel_id == channel_id).delete(synchronize_session=False)
    image_style = dict(channel.image_style or {})
    image_style["library_image_count"] = 0
    channel.image_style = image_style
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
    if folder:
        folder.image_count = 0
    db.commit()
    return {"deleted": deleted, "image_count": 0}


@router.get("/{channel_id}/youtube/auth-url")
def get_youtube_auth_url(channel_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
    if channel.user_id != current_user.id and not current_user.is_admin:
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
