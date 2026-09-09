"""Voice-cloning job processing, run by the worker (not the API process).

Cloning used to run as a background thread inside the API request process,
tracked in an in-memory dict. That thread died silently every time the API
container got redeployed — which happens far more often than the worker
(routine API/frontend-only changes never touch it, see entrypoint.sh's ROLE
split) — leaving the creator's "Clonage…" button stuck forever with no way
to ever resolve, even though nothing was actually wrong with their audio or
Izivoice's side of things. Moving the real work here, driven by the worker
polling VoiceCloneJob rows in the database, means the job survives an API
redeploy exactly like a queued video render already does.
"""
from pathlib import Path
from typing import Optional

import httpx

from src.config import AI33PRO_API_KEY, IZIVOICE_API_KEY, IZIVOICE_BASE_URL, STORAGE_PATH
from src.db.models import VoiceCloneJob
from src.utils.logger import logger

# Izivoice's own guidance for their "server couldn't process this audio"
# error: a short, clean sample clones just as well as a long one and avoids
# their engine choking on longer/complex clips — enforced automatically
# instead of relying on the creator to manually trim their file.
CLONE_MAX_SECONDS = 30

# Short, neutral sentence read back in the newly cloned voice right after
# cloning, since Izivoice's /clone itself never returns a sample to preview.
VOICE_PREVIEW_TEXT = "Bonjour, voici un aperçu de cette voix clonée sur KappGen."

# Uploaded samples awaiting processing — shared storage, since the API
# container (which saves the file) and the worker (which processes it) are
# separate containers that only share this volume, not memory.
CLONE_UPLOADS_DIR = STORAGE_PATH / "voice_clone_uploads"


def transcode_to_clean_audio(contents: bytes, filename: str) -> bytes:
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
            run_ffmpeg(
                ["ffmpeg", "-y", "-i", str(src_path), "-t", str(CLONE_MAX_SECONDS), "-ar", "24000", "-ac", "1", "-c:a", "flac", str(dst_path)],
                timeout=60,
            )
            return dst_path.read_bytes()
        except (FFmpegError, OSError) as exc:
            logger.warning(f"Voice-clone pre-transcode failed, sending original file as-is: {exc}")
            return contents


def process_voice_clone_job(db, job: VoiceCloneJob, api_key: Optional[str] = None) -> None:
    """Runs one claimed clone job (already flipped to "processing" and
    committed by the caller) to completion (or failure), updating the row in
    place. Always leaves the job in a terminal status ("done" or "error").
    Uses ai33.pro directly if ai33pro is configured / prioritized in
    voiceover_provider_order(), otherwise uses Izivoice."""
    from src.utils.app_settings import voiceover_provider_order
    from src.pipeline import ai33_provider

    audio_path = STORAGE_PATH / job.audio_path
    try:
        contents = audio_path.read_bytes()
    except OSError as exc:
        job.status = "error"
        job.error_message = f"Échantillon audio introuvable : {exc}"
        db.commit()
        return

    try:
        clean_audio = transcode_to_clean_audio(contents, job.audio_path)
        if len(clean_audio) > 8 * 1024 * 1024:
            job.status = "error"
            job.error_message = "Cet échantillon est trop volumineux une fois nettoyé (> 8 Mo). Utilisez un extrait plus court (~20-45 s)."
            db.commit()
            return

        primary_provider = "ai33pro" if AI33PRO_API_KEY else "izivoice"
        voice_id = None

        if primary_provider == "ai33pro" and AI33PRO_API_KEY:
            logger.info(f"Cloning voice '{job.name}' directly on ai33.pro...")
            with httpx.Client() as client:
                voice_id = ai33_provider.submit_voice_clone(
                    client=client,
                    name=job.name,
                    audio_bytes=clean_audio,
                    filename="voice-sample.flac",
                    description="Clonée via KappGen",
                    api_key=AI33PRO_API_KEY,
                )
        else:
            effective_key = api_key or IZIVOICE_API_KEY
            if not effective_key:
                raise RuntimeError("Aucune clé API configurée pour le clonage de voix (ni ai33.pro, ni Izivoice).")
            logger.info(f"Cloning voice '{job.name}' via Izivoice...")
            response = httpx.post(
                f"{IZIVOICE_BASE_URL}/clone",
                headers={"Authorization": f"Bearer {effective_key}"},
                files={"file": ("voice-sample.flac", clean_audio, "audio/flac")},
                data={"name": job.name, "removeNoise": "true", "optimizeAccent": "true"},
                timeout=280,
            )
            response.raise_for_status()
            data = response.json()
            voice_id = data.get("voice_id") or ((data.get("data") or {}).get("voice_id"))

        if not voice_id:
            job.status = "error"
            job.error_message = f"{primary_provider} n'a retourné aucun identifiant de voix."
            db.commit()
            return

        preview_url = None
        try:
            preview_path = STORAGE_PATH / "voice_previews" / f"{voice_id}.mp3"
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            from src.pipeline.voiceover import generate_voiceover
            generate_voiceover(VOICE_PREVIEW_TEXT, preview_path, voice_id=voice_id, api_key=api_key)
            preview_url = f"/channels/voice/{voice_id}/preview"
        except Exception as exc:
            logger.warning(f"Voice-clone preview generation failed for {voice_id}: {exc}")

        job.status = "done"
        job.voice_id = voice_id
        job.preview_url = preview_url
        db.commit()
        logger.info(f"Voice clone successful for job {job.id}: voice_id={voice_id}")
    except httpx.HTTPStatusError as exc:
        try:
            upstream_detail = exc.response.json()
            upstream_detail = upstream_detail.get("message") or upstream_detail.get("detail") or upstream_detail.get("error") or exc.response.text
        except Exception:
            upstream_detail = exc.response.text
        logger.error(f"Voice clone failed ({exc.response.status_code}): {upstream_detail}")
        job.status = "error"
        job.error_message = f"Le fournisseur a refusé le clonage ({exc.response.status_code}) : {upstream_detail or 'erreur inconnue'}"
        db.commit()
    except Exception as exc:
        logger.error(f"Voice clone crashed: {exc}")
        job.status = "error"
        job.error_message = f"Le clonage a échoué : {exc}"
        db.commit()
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except OSError:
            pass
