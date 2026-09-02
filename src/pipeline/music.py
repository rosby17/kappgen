import json
import random
import time
import httpx
from pathlib import Path
from typing import Optional, Dict, Any, List
from src.config import ASSETS_PATH, STORAGE_PATH, IZIVOICE_API_KEY, IZIVOICE_BASE_URL
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg

TASK_POLL_INTERVAL_SECONDS = 3.0
TASK_POLL_TIMEOUT_SECONDS = 90


def _izivoice_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {IZIVOICE_API_KEY}"}


def _poll_izivoice_task(task_id: str, client: httpx.Client) -> Dict[str, Any]:
    elapsed = 0.0
    while elapsed < TASK_POLL_TIMEOUT_SECONDS:
        try:
            resp = client.get(f"{IZIVOICE_BASE_URL}/tasks/{task_id}", headers=_izivoice_headers(), timeout=30.0)
            if resp.status_code >= 500:
                time.sleep(TASK_POLL_INTERVAL_SECONDS)
                elapsed += TASK_POLL_INTERVAL_SECONDS
                continue
            resp.raise_for_status()
            task = resp.json()
        except httpx.TransportError:
            time.sleep(TASK_POLL_INTERVAL_SECONDS)
            elapsed += TASK_POLL_INTERVAL_SECONDS
            continue
        status = task.get("status")
        if status == "done":
            return task
        if status in ("error", "failed"):
            raise RuntimeError(f"Izivoice music task {task_id} failed: {task.get('error_message') or task}")
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
        elapsed += TASK_POLL_INTERVAL_SECONDS
    raise TimeoutError(f"Izivoice music task {task_id} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def generate_music_izivoice(prompt: str, duration: float, output_path: Path) -> Path:
    """
    Generates a background music track via Izivoice's music-generation endpoint.

    Confirmed against https://www.izivoice.app/api-docs: POST /music takes a
    `prompt` string (no documented duration param — the API decides the
    length), returns {success, task_id}, polled via GET /tasks/{task_id}
    until status == "done", at which point metadata.audio_url holds the track.
    """
    with httpx.Client() as client:
        resp = client.post(
            f"{IZIVOICE_BASE_URL}/music",
            headers=_izivoice_headers(),
            json={"prompt": prompt[:2000]},
            timeout=30.0
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success") or not data.get("task_id"):
            raise ValueError(f"Unexpected Izivoice music-generate response: {data}")

        task = _poll_izivoice_task(data["task_id"], client)
        audio_url = (task.get("metadata") or {}).get("audio_url")
        if not audio_url:
            raise ValueError(f"Izivoice music task {data['task_id']} completed with no audio_url: {task}")

        audio_resp = client.get(audio_url, timeout=60.0)
        audio_resp.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio_resp.content)
    return output_path


def _generate_synthetic_fallback_track(duration: float) -> Path:
    """Last-resort track when there's no user-uploaded music and AI generation
    is unavailable/fails: a soft low drone, not a copyrighted stock track."""
    music_dir = ASSETS_PATH / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    output_path = music_dir / "ambient_fallback.mp3"
    if not output_path.exists():
        logger.info(f"Generating synthetic fallback ambient track ({duration:.1f}s)...")
        filter_str = f"sine=frequency=110:duration={max(duration, 5.0):.1f},volume=0.2"
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", filter_str, "-c:a", "libmp3lame", str(output_path)]
        run_ffmpeg(cmd)
    return output_path


def get_background_music_track(
    music_pref: Dict[str, Any],
    duration: float,
    channel_id: Optional[str] = None,
    niche: Optional[str] = None,
    script_text: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Path:
    """
    Resolves the background music track for a render:
      - mode "library": picks a random track from the channel's own uploaded
        set (music_pref["tracks"], storage-relative paths) — the user's own
        music, never third-party stock tracks. Free — nothing is billed.
      - mode "ai_generate": generates a track with Izivoice, using a prompt
        Claude derives from the channel niche and this video's script (or an
        explicit music_pref["ai_prompt"] override, which skips the Claude step).
        Billed IZIVOICE_MUSIC_CREDITS, same as the wizard's own preview
        (channels.py's /preview-ai-music) — but only on an actual cache miss,
        since a cache hit never calls Izivoice at all.
      - anything else / on failure: a synthetic ambient drone, generated
        locally, so a render never blocks on missing music.
    """
    mode = music_pref.get("mode", "library")

    if mode == "library":
        tracks: List[str] = music_pref.get("tracks") or []
        candidates = [STORAGE_PATH / t for t in tracks if (STORAGE_PATH / t).exists()]
        if candidates:
            return random.choice(candidates)
        logger.info("Music mode is 'library' but no uploaded tracks are available; using fallback tone.")
        return _generate_synthetic_fallback_track(duration)

    if mode == "ai_generate":
        if not IZIVOICE_API_KEY:
            logger.info("Music mode is 'ai_generate' but IZIVOICE_API_KEY is not set; using fallback tone.")
            return _generate_synthetic_fallback_track(duration)

        prompt = music_pref.get("ai_prompt")
        if not prompt:
            try:
                from src.pipeline.vision import generate_music_prompt
                prompt = generate_music_prompt(niche or "general", script_text or "")
            except Exception as e:
                logger.warning(f"Claude music-prompt generation failed ({e}); using a plain template prompt.")
                prompt = f"Instrumental background music for a {niche or 'general'} themed video, subtle and non-distracting"
        try:
            cache_dir = ASSETS_PATH / "music" / "ai_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            output_path = cache_dir / f"{abs(hash((prompt, round(duration)))) }.mp3"
            if output_path.exists():
                return output_path
            if user_id:
                from src.utils.billing import debit_izivoice_usage_by_user_id, IZIVOICE_MUSIC_CREDITS
                if not debit_izivoice_usage_by_user_id(user_id, IZIVOICE_MUSIC_CREDITS, "ai_music_generation"):
                    logger.warning(f"Insufficient KappGen credit balance for AI music generation (user {user_id}); using fallback tone instead.")
                    return _generate_synthetic_fallback_track(duration)
            return generate_music_izivoice(prompt, duration, output_path)
        except Exception as e:
            logger.warning(f"Izivoice music generation failed ({e}). Falling back to synthetic tone.")
            return _generate_synthetic_fallback_track(duration)

    return _generate_synthetic_fallback_track(duration)
