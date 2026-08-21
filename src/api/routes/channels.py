from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
import random
import re
import shutil
import threading
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
from src.utils.auth import get_current_user
from src.utils.billing import user_has_active_subscription
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
    """Izivoice's real catalog holds 11 000+ voices — far too many to ever load
    in full into a picker. Without a search term we fetch a first, generously
    sized page (~1000) so the picker isn't limited to a handful of voices;
    with a search term we forward it straight to Izivoice (which searches its
    whole catalog server-side) and only fetch a couple of pages, since the
    match set is already narrow.

    Passing `page` switches to single-page mode: the picker uses this to load
    further chunks past the initial ~1000 ("Charger plus de voix") instead of
    eagerly fetching the whole catalog up front."""
    api_key = izivoice_key_for_user(current_user)
    if not api_key:
        raise HTTPException(status_code=503, detail="Le catalogue de voix n'est pas configuré.")
    # Izivoice's `page` is 0-indexed — page=1 silently returns the *second*
    # page (empty for most accounts), which is why the picker only ever
    # showed the 4 hardcoded fallback voices instead of the real catalog.
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
        # Izivoice's `has_more` flag has been unreliable (see note above about
        # page indexing) — a full page back is treated as "there might be
        # more" regardless of what the flag says, so the picker never stops
        # short of the real end of the catalog. Only a short/empty page is
        # trusted as the actual end.
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
            # Same unreliable-flag issue as the single-page branch above: a
            # full page is treated as "keep going" even if the provider's
            # has_more says otherwise, so we don't cut the catalog short.
            has_more = bool(data.get("has_more")) or len(batch) >= 100
            if not batch:
                break
    return {"voices": all_voices, "has_more": has_more, "next_page": max_pages}

# In-memory job store for voice cloning — Izivoice's /clone call can run past
# Cloudflare's fixed ~100s proxy timeout (same class of issue already hit
# with large image-folder uploads, see save_valid_library_images below), which
# silently killed the request client-side with no error and the button stuck
# forever on "Clonage…". The endpoint now returns immediately with a job_id,
# does the actual (slow) Izivoice call in a background thread, and the
# frontend polls /voice/clone/status/{job_id} until it's done.
_clone_jobs: Dict[str, Dict[str, Any]] = {}

# Izivoice's own guidance for their "server couldn't process this audio"
# error: a short, clean sample clones just as well as a long one and avoids
# their engine choking on longer/complex clips — enforced automatically
# instead of relying on the creator to manually trim their file.
CLONE_MAX_SECONDS = 30

# Short, neutral sentence read back in the newly cloned voice right after
# cloning, since Izivoice's /clone itself never returns a sample to preview.
VOICE_PREVIEW_TEXT = "Bonjour, voici un aperçu de cette voix clonée sur KappGen."


def _transcode_to_clean_audio(contents: bytes, filename: str) -> bytes:
    """Re-encodes the sample to FLAC (lossless, mono, 24kHz) before it ever
    reaches Izivoice. Some uploads (mobile-app exports, browser recordings)
    have a technically-playable but non-standard container/header that
    Izivoice's cloning engine can fail to read the duration of — even with
    their own removeNoise cleanup applied server-side (see Izivoice's own
    src/app/api/clone/route.ts, which hits the same issue and works around it
    by transcoding first — notably to FLAC too, in their denoise path). Doing
    that ourselves guarantees a clean file no matter what Izivoice does on
    their end. FLAC (not raw PCM WAV) specifically to stay under Izivoice's
    4MB upload cap: an uncompressed 44.1kHz WAV blows past 4MB on anything
    longer than ~45s, silently turning a fixed "bad header" into a new
    "file too large" failure for any real voice sample. Returns the original
    bytes unchanged if ffmpeg can't decode the input at all (already-invalid
    audio, caught later by Izivoice's own validation instead).

    Also hard-caps the sample to CLONE_MAX_SECONDS: Izivoice's own error for
    long/complex clips says as much ("Privilégiez un extrait pur de 30
    secondes maximum") — voice cloning doesn't benefit from a longer sample
    past that anyway, so trimming automatically is strictly better than
    surfacing their 500 and asking the creator to re-cut the file by hand."""
    import tempfile
    from src.utils.ffmpeg_runner import run_ffmpeg, FFmpegError
    suffix = Path(filename or "audio").suffix or ".bin"
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / f"in{suffix}"
        dst_path = Path(tmp) / "out.flac"
        src_path.write_bytes(contents)
        try:
            run_ffmpeg(["ffmpeg", "-y", "-i", str(src_path), "-t", str(CLONE_MAX_SECONDS), "-ar", "24000", "-ac", "1", "-c:a", "flac", str(dst_path)])
            return dst_path.read_bytes()
        except (FFmpegError, OSError) as exc:
            logger.warning(f"Voice-clone pre-transcode failed, sending original file as-is: {exc}")
            return contents


def _run_clone_job(job_id: str, api_key: str, filename: str, content_type: str, contents: bytes, name: str):
    try:
        clean_audio = _transcode_to_clean_audio(contents, filename)
        if len(clean_audio) > 4 * 1024 * 1024:
            _clone_jobs[job_id] = {
                "status": "error",
                "detail": "Cet échantillon est trop long une fois nettoyé (limite Izivoice : 4 Mo). Utilisez un extrait plus court (~30-45 s).",
            }
            return
        response = httpx.post(
            f"{IZIVOICE_BASE_URL}/clone",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("voice-sample.flac", clean_audio, "audio/flac")},
            # Back to removeNoise=true (server-side noise cleanup) now that
            # Izivoice has fixed the "Failed to parse duration" bug on that
            # path too — it was temporarily forced to "false" to route
            # around it (see git history for the full story).
            data={"name": name, "removeNoise": "true", "optimizeAccent": "true"},
            timeout=280,
        )
        response.raise_for_status()
        data = response.json()
        voice_id = data.get("voice_id") or ((data.get("data") or {}).get("voice_id"))
        if not voice_id:
            _clone_jobs[job_id] = {"status": "error", "detail": "Izivoice n'a retourné aucun identifiant de voix."}
            return

        # Izivoice's /clone deliberately returns preview_url: null ("no longer
        # generate a preview here to speed up the process") — without this,
        # a freshly cloned voice has no way to be previewed anywhere in the
        # app (catalog voices all have a pre-made sample; this one wouldn't).
        # Best-effort: a cloned voice with no preview is still usable, just
        # not previewable, so this never fails the clone itself.
        preview_url = None
        try:
            preview_path = STORAGE_PATH / "voice_previews" / f"{voice_id}.mp3"
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            from src.pipeline.voiceover import generate_voiceover
            generate_voiceover(VOICE_PREVIEW_TEXT, preview_path, voice_id=voice_id, api_key=api_key)
            # API_BASE on the frontend already ends in /api — a leading /api
            # here would double up into /api/api/... (see the same note on
            # the scene-image route in videos.py, which hit this exact bug).
            preview_url = f"/channels/voice/{voice_id}/preview"
        except Exception as exc:
            logger.warning(f"Voice-clone preview generation failed for {voice_id}: {exc}")

        _clone_jobs[job_id] = {"status": "done", "voice_id": voice_id, "name": name, "preview_url": preview_url}
    except httpx.HTTPStatusError as exc:
        # Surface whatever Izivoice actually said instead of just the status
        # code — a bare "(500)" gives no way to tell a bad audio file apart
        # from a misconfigured request on our side.
        try:
            upstream_detail = exc.response.json()
            upstream_detail = upstream_detail.get("message") or upstream_detail.get("detail") or upstream_detail.get("error") or exc.response.text
        except Exception:
            upstream_detail = exc.response.text
        logger.error(f"Izivoice /clone failed ({exc.response.status_code}): {upstream_detail}")
        # Known upstream quirk (acknowledged in Izivoice's own code): their
        # cloning engine sometimes can't read the duration of an audio file
        # whose container/header is non-standard, even though the file plays
        # fine everywhere else — re-exporting it (e.g. to a clean WAV/MP3)
        # reliably fixes it, so point the creator at that instead of a raw
        # upstream error they can't act on.
        if "failed to parse duration" in str(upstream_detail).lower():
            _clone_jobs[job_id] = {
                "status": "error",
                "detail": "Izivoice n'a pas réussi à lire ce fichier audio (en-tête non standard, même s'il joue normalement ailleurs). Réexportez-le en MP3 ou WAV propre (ex. via Audacity ou QuickTime) puis réessayez.",
            }
        else:
            _clone_jobs[job_id] = {"status": "error", "detail": f"Izivoice a refusé le clonage ({exc.response.status_code}) : {upstream_detail or 'raison inconnue'}"}
    except Exception as exc:
        logger.error(f"Izivoice /clone crashed: {exc}")
        _clone_jobs[job_id] = {"status": "error", "detail": f"Le clonage a échoué : {exc}"}


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
    _clone_jobs[job_id] = {"status": "pending"}
    threading.Thread(
        target=_run_clone_job,
        args=(job_id, api_key, audio.filename, audio.content_type, contents, name.strip()),
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "pending"}


@router.get("/voice/clone/status/{job_id}")
def clone_voice_status(job_id: str, current_user: User = Depends(get_current_user)):
    job = _clone_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Tâche de clonage introuvable ou expirée.")
    return job


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
    channel = Channel(
        user_id=current_user.id,
        name=payload.name,
        description=payload.description,
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

@router.put("/{channel_id}")
def update_channel(channel_id: str, payload: ChannelUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    # Watermark removal is a paid feature — without this, any user could
    # just flip the toggle themselves regardless of subscription status.
    if payload.effects_config is not None and not payload.effects_config.watermark_enabled:
        if not user_has_active_subscription(db, current_user):
            raise HTTPException(status_code=403, detail="Un abonnement actif est requis pour retirer le filigrane KappGen.")

    if payload.name is not None:
        channel.name = payload.name
    if payload.description is not None:
        channel.description = payload.description
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
        channel.publish_mode = payload.publish_mode
    if payload.publish_time_mode is not None:
        channel.publish_time_mode = payload.publish_time_mode
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
    if channel.automation_mode != "auto":
        raise HTTPException(status_code=409, detail="Cette chaîne n'est pas en mode automatique.")

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
    channel.image_style = image_style
    db.commit()
    db.refresh(channel)
    return channel.to_dict()

@router.post("/{channel_id}/library-images")
async def upload_channel_library_images(
    channel_id: str,
    files: List[UploadFile] = File(...),
    append: bool = Form(False),
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
    channel.image_style = image_style

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
            # A manually-uploaded logo otherwise always wins over the YouTube
            # avatar in the UI (an explicit creator choice) — but connecting
            # is itself an explicit "sync my real identity" action, so any
            # earlier placeholder logo gets cleared to let the real photo
            # show through. Uploading a new one afterward still overrides it.
            branding = dict(channel.branding or {})
            if branding.get("logo_path"):
                branding["logo_path"] = None
                channel.branding = branding
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
    db.delete(channel)
    db.commit()
    return {"message": "Channel deleted successfully"}
