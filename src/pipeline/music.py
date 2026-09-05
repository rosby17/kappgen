import json
import random
import time
import httpx
from pathlib import Path
from typing import Optional, Dict, Any, List
from src.config import ASSETS_PATH, STORAGE_PATH, IZIVOICE_API_KEY, IZIVOICE_BASE_URL, AI33PRO_API_KEY
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg


def _configured_music_providers() -> List[str]:
    """Admin-ordered music providers (src/utils/app_settings.py), filtered to
    whichever actually have a key configured — same pattern as voiceover.py's
    _configured_providers(). "ai33pro" is ai33.pro directly
    (src/pipeline/ai33_provider.py): Izivoice's own /music route is itself a
    thin passthrough to that same upstream endpoint, so this bypasses
    Izivoice's account/quota entirely rather than changing what's generated."""
    from src.utils.app_settings import music_provider_order
    keys = {"izivoice": IZIVOICE_API_KEY, "ai33pro": AI33PRO_API_KEY}
    providers = [p for p in music_provider_order() if keys.get(p)]
    return providers or (["izivoice"] if IZIVOICE_API_KEY else [])

TASK_POLL_INTERVAL_SECONDS = 3.0
# A real Izivoice music generation commonly runs well past 90s (the same
# reason the wizard's own preview endpoint had to move to an async job
# pattern — see channels.py's /preview-ai-music). That constraint was about
# Cloudflare's ~100s proxy timeout on an HTTP request; this poll runs
# entirely inside the background render worker with no HTTP request behind
# it, so there's no reason to cap it that low here too. A 90s timeout was
# silently discarding the real track and falling back to
# _generate_synthetic_fallback_track's plain 110Hz drone on almost every
# music-channel render — "the montage finishes but it's just noise, no
# actual music" was this fallback firing nearly every time, not a bug in
# the render itself.
TASK_POLL_TIMEOUT_SECONDS = 600


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


def _music_via_izivoice(
    client: httpx.Client, prompt: str, output_path: Path,
    lyrics: Optional[str] = None, title: Optional[str] = None,
    tags: Optional[str] = None, vocal_gender: Optional[str] = None,
) -> Path:
    """
    Generates a music track via Izivoice's music-generation endpoint.

    POST /music now requires create_mode ("simple" or "custom") — a field it
    silently started rejecting requests without (400 "Invalid creation
    mode"), breaking this call entirely until this was updated. "simple"
    mode (default, no lyrics given) takes gpt_description_prompt and
    make_instrumental=True — right for background music behind narration,
    which must never compete with vocals. Passing `lyrics` switches to
    "custom" mode (title/lyrics/tags/vocal_gender) for a real song with
    vocals — the Vidéo Musicale product, where the content IS the song. No
    documented duration param either way — the API decides the length.
    Returns {success, task_id}, polled via GET /tasks/{task_id} until status
    == "done", at which point metadata.audio_url holds the track.
    """
    if lyrics:
        payload = {"create_mode": "custom", "title": title or "", "lyrics": lyrics}
        if tags:
            payload["tags"] = tags
        if vocal_gender:
            payload["vocal_gender"] = vocal_gender
    else:
        payload = {
            "create_mode": "simple",
            "gpt_description_prompt": prompt[:2000],
            "make_instrumental": True,
        }
    resp = client.post(
        f"{IZIVOICE_BASE_URL}/music",
        headers=_izivoice_headers(),
        json=payload,
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


def _music_via_ai33(
    client: httpx.Client, prompt: str, output_path: Path,
    lyrics: Optional[str] = None, title: Optional[str] = None,
    tags: Optional[str] = None, vocal_gender: Optional[str] = None,
) -> Path:
    """Same shape as _music_via_izivoice, ai33.pro directly (see
    src/pipeline/ai33_provider.py) — bypasses Izivoice's own account/quota
    entirely rather than changing what's generated (Izivoice's own /music
    route is itself a thin passthrough to this same upstream endpoint)."""
    from src.pipeline import ai33_provider
    task_id = ai33_provider.submit_music_generation(
        client, prompt, make_instrumental=(not lyrics), api_key=AI33PRO_API_KEY,
        lyrics=lyrics, title=title, tags=tags, vocal_gender=vocal_gender,
    )
    task = ai33_provider.poll_task(task_id, client, AI33PRO_API_KEY)
    audio_url = (task.get("metadata") or {}).get("audio_url")
    if not audio_url:
        raise ValueError(f"ai33.pro music task {task_id} completed with no audio_url: {task}")

    audio_resp = client.get(audio_url, timeout=60.0)
    audio_resp.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio_resp.content)
    return output_path


def generate_music_izivoice(
    prompt: str, duration: float, output_path: Path,
    lyrics: Optional[str] = None, title: Optional[str] = None,
    tags: Optional[str] = None, vocal_gender: Optional[str] = None,
) -> Path:
    """Public entrypoint (kept its historical name — every call site expects
    it) — tries the admin-ordered music providers (see
    _configured_music_providers()) in turn, falling through to the next one
    on failure. `duration` is unused by every provider so far (neither
    Izivoice nor ai33.pro accepts a target length; the API decides), kept in
    the signature only for call-site compatibility.

    `lyrics` is None for every existing caller (narration background music,
    always instrumental) — only music_video.py's Vidéo Musicale pipeline
    passes real lyrics, to generate an actual song with vocals instead of an
    instrumental bed."""
    providers = _configured_music_providers()
    if not providers:
        raise RuntimeError("No music provider configured (IZIVOICE_API_KEY / AI33PRO_API_KEY both missing).")

    last_error: Optional[Exception] = None
    with httpx.Client() as client:
        for provider in providers:
            try:
                if provider == "ai33pro":
                    return _music_via_ai33(client, prompt, output_path, lyrics=lyrics, title=title, tags=tags, vocal_gender=vocal_gender)
                return _music_via_izivoice(client, prompt, output_path, lyrics=lyrics, title=title, tags=tags, vocal_gender=vocal_gender)
            except Exception as e:
                last_error = e
                logger.warning(f"{provider} music generation failed ({e}); trying next configured provider if any.")
                continue
    raise last_error or RuntimeError("Music generation failed on every configured provider.")


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
    video_id: Optional[str] = None,
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
                if not debit_izivoice_usage_by_user_id(user_id, IZIVOICE_MUSIC_CREDITS, "ai_music_generation", video_id=video_id):
                    logger.warning(f"Insufficient KappGen credit balance for AI music generation (user {user_id}); using fallback tone instead.")
                    return _generate_synthetic_fallback_track(duration)
            try:
                return generate_music_izivoice(prompt, duration, output_path)
            except Exception:
                # One immediate retry before giving up — Izivoice's /music
                # endpoint has been seen returning a transient 500 that
                # succeeds on a second try.
                return generate_music_izivoice(prompt, duration, output_path)
        except Exception as e:
            logger.warning(f"Izivoice music generation failed ({e}). Falling back to synthetic tone.")
            if user_id:
                # Already charged as if generation succeeded — it didn't, so
                # the free fallback tone must not be paid for.
                from src.utils.billing import refund_izivoice_usage_by_user_id, IZIVOICE_MUSIC_CREDITS
                refund_izivoice_usage_by_user_id(user_id, IZIVOICE_MUSIC_CREDITS, "ai_music_generation", video_id=video_id)
            return _generate_synthetic_fallback_track(duration)

    return _generate_synthetic_fallback_track(duration)
