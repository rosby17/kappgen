"""Direct client for ai33.pro — the actual upstream TTS/STT/voice/image
provider that Izivoice (a separate business, also owned by the operator)
resells. KappGen's automated volume was consuming Izivoice's own account
quota; this lets an admin route KappGen's calls straight to ai33.pro
instead, via src/utils/app_settings.py's voiceover_provider_order() (voice)
and thumbnail_provider_order() (images).

Protocol reverse-engineered from Izivoice's own source (which calls ai33.pro
this same way for its own product) — see the "Connexion directe à ai33.pro"
plan for the exact file/line references. Differs from Izivoice's own wrapper
API in three ways: header is `xi-api-key` (not `Authorization: Bearer`),
bodies are FormData (not JSON), and the task-status path is singular
`/v1/task/{id}` (not Izivoice's own `/tasks/{id}`).

The task metadata shape once a job is "done" (`audio_url`, or `json_url`/
`srt_url` for STT, or `result_images` for image generation) is the same raw
shape Izivoice's own UI reads directly off `task.metadata` — so voiceover.py's
existing `_extract_words_from_stt_metadata` works unchanged against it, and
image generation reads `metadata.result_images` the same way Izivoice's own
public /api/images docs describe (docs/image-api.fr.md in the izivoice repo)
— only the submission/polling transport differs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.config import AI33PRO_API_KEY, AI33PRO_BASE_URL, BACKEND_BASE_URL
from src.utils.logger import logger

TASK_POLL_INTERVAL_SECONDS = 2.5
TASK_POLL_TIMEOUT_SECONDS = 600
STT_WEBHOOK_WAIT_TIMEOUT_SECONDS = 900  # STT can take a while on long chunks


def _headers(api_key: Optional[str] = None) -> Dict[str, str]:
    return {"xi-api-key": api_key or AI33PRO_API_KEY}


def _post_with_retry(client: httpx.Client, url: str, max_retries: int = 5, **kwargs) -> httpx.Response:
    delay = 3.0
    for attempt in range(max_retries + 1):
        resp = client.post(url, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                resp.raise_for_status()
            logger.warning(f"ai33.pro request to {url} returned {resp.status_code}, retrying in {delay:.0f}s...")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        return resp
    return resp


def poll_task(task_id: str, client: httpx.Client, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Polls GET /v1/task/{id} until status is 'done'/'error' (or timeout).
    Only used for TTS — ai33.pro's own STT polling is documented (in
    Izivoice's source) as unreliable, so STT goes through the webhook instead
    (see await_stt_webhook_result below)."""
    elapsed = 0.0
    while elapsed < TASK_POLL_TIMEOUT_SECONDS:
        try:
            resp = client.get(f"{AI33PRO_BASE_URL}/v1/task/{task_id}", headers=_headers(api_key), timeout=30.0)
            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning(f"ai33.pro task poll for {task_id} returned {resp.status_code}, retrying...")
                time.sleep(TASK_POLL_INTERVAL_SECONDS)
                elapsed += TASK_POLL_INTERVAL_SECONDS
                continue
            resp.raise_for_status()
            task = resp.json()
        except httpx.TransportError as e:
            logger.warning(f"ai33.pro task poll for {task_id} transport error ({e}), retrying...")
            time.sleep(TASK_POLL_INTERVAL_SECONDS)
            elapsed += TASK_POLL_INTERVAL_SECONDS
            continue
        status = task.get("status")
        if status == "done":
            return task
        if status == "error":
            raise RuntimeError(f"ai33.pro task {task_id} failed: {task.get('error') or task}")
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
        elapsed += TASK_POLL_INTERVAL_SECONDS
    raise TimeoutError(f"ai33.pro task {task_id} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def _raise_for_status_with_detail(resp: httpx.Response, endpoint_name: str = "ai33.pro") -> None:
    if resp.is_error:
        detail = ""
        try:
            err_data = resp.json()
            detail = err_data.get("message") or err_data.get("detail") or err_data.get("error") or str(err_data)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{endpoint_name} returned HTTP {resp.status_code}: {detail or resp.reason_phrase}")


def default_voice_id(client: httpx.Client, api_key: Optional[str] = None) -> str:
    """GET /v1/shared-voices — response is NOT wrapped in {"data": {...}} the
    way Izivoice's own /voices is; it's {"total_count", "voices": [...]}
    directly (confirmed against a live call). Filtered to French — this is a
    16 000+ voice global catalogue with no language bias, unlike Izivoice's
    own /voices which defaults its account to French content."""
    resp = client.get(
        f"{AI33PRO_BASE_URL}/v1/shared-voices",
        # page_size=1 is silently empty (voices=[] despite total_count>0 and
        # has_more=true) — an ai33.pro quirk confirmed live; 5 is the smallest
        # tested value that actually returns rows.
        headers=_headers(api_key),
        params={"page": 0, "page_size": 5, "language": "fr"},
        timeout=30.0,
    )
    _raise_for_status_with_detail(resp, "ai33.pro GET /v1/shared-voices")
    voices = resp.json().get("voices") or []
    if not voices:
        # Retry without the language filter as a last resort rather than
        # failing outright — better a non-French auto-pick than no voice at
        # all when explicitly none is configured.
        resp = client.get(
            f"{AI33PRO_BASE_URL}/v1/shared-voices",
            headers=_headers(api_key), params={"page": 0, "page_size": 5}, timeout=30.0,
        )
        _raise_for_status_with_detail(resp, "ai33.pro GET /v1/shared-voices")
        voices = resp.json().get("voices") or []
    if not voices:
        raise RuntimeError("No voice_id configured and ai33.pro /v1/shared-voices returned no voices to auto-select.")
    selected_id = voices[0]["voice_id"]
    logger.info(f"Auto-selected ai33.pro voice_id={selected_id} ({voices[0].get('name')})")
    return selected_id


def list_user_voices(client: httpx.Client, api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """GET /v3/voices?provider=clone — returns the user's own cloned voices on ai33.pro.
    The returned voice_id values are already prefixed (clone_<id>) as required by v3 TTS."""
    try:
        resp = client.get(
            f"{AI33PRO_BASE_URL}/v3/voices",
            headers=_headers(api_key),
            params={"provider": "clone", "page_size": 100},
            timeout=30.0,
        )
        if resp.status_code == 200:
            return (resp.json().get("data") or [])
        return []
    except Exception as exc:
        logger.warning(f"ai33.pro /v3/voices fetch failed: {exc}")
        return []


def submit_voice_clone(
    client: httpx.Client,
    name: str,
    audio_bytes: bytes,
    filename: str = "voice-sample.flac",
    api_key: Optional[str] = None,
) -> str:
    """POST /v3/text-to-speech/voice-clone — instant voice cloning on ai33.pro.
    Returns the full prefixed voice_id (clone_<id>) ready for use directly
    in submit_tts() without any further transformation.
    Fields per the v3 docs: voice_name (required), audio_file (required, max 10 MB)."""
    media_type = "audio/flac" if filename.endswith(".flac") else "audio/mpeg"
    resp = _post_with_retry(
        client,
        f"{AI33PRO_BASE_URL}/v3/text-to-speech/voice-clone",
        headers=_headers(api_key),
        data={"voice_name": name},
        files={"audio_file": (filename, audio_bytes, media_type)},
        timeout=120.0,
    )
    _raise_for_status_with_detail(resp, "ai33.pro POST /v3/text-to-speech/voice-clone")
    res_data = resp.json()
    raw_id = (res_data.get("data") or {}).get("voice_id") or res_data.get("voice_id")
    if not raw_id:
        raise RuntimeError(f"ai33.pro voice clone returned no voice_id: {res_data}")
    # v3 TTS requires cloned voices to be addressed as clone_<id>
    prefixed = str(raw_id) if str(raw_id).startswith("clone_") else f"clone_{raw_id}"
    logger.info(f"ai33.pro voice cloned successfully: {prefixed}")
    return prefixed


def delete_voice(client: httpx.Client, voice_id: str, api_key: Optional[str] = None) -> bool:
    """DELETE /v3/text-to-speech/voice-clone/{id} — deletes a cloned voice on ai33.pro.
    Strips the clone_ prefix if present since the endpoint takes the raw numeric id."""
    raw_id = voice_id.removeprefix("clone_")
    try:
        resp = client.delete(
            f"{AI33PRO_BASE_URL}/v3/text-to-speech/voice-clone/{raw_id}",
            headers=_headers(api_key),
            timeout=30.0,
        )
        return resp.status_code in (200, 204)
    except Exception as exc:
        logger.warning(f"ai33.pro delete voice {voice_id} failed: {exc}")
        return False


def submit_tts(
    client: httpx.Client,
    text: str,
    voice_id: str,
    voice_settings: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
) -> str:
    """POST /v3/text-to-speech (FormData) — returns task_id. Poll with poll_task();
    result's metadata.audio_url is the generated audio.

    Per the v3 API docs, voice_id MUST carry a provider prefix:
    elevenlabs_, minimax_, clone_, edge_, kokoro_, vbee_, fishaudio_.
    Cloned voices (from submit_voice_clone) are already stored as clone_<id>.
    Shared/catalog voices from /v1/shared-voices are returned without prefix;
    those are ElevenLabs voices so we prepend elevenlabs_ automatically."""
    # Ensure voice_id has the required v3 provider prefix
    KNOWN_PREFIXES = ("elevenlabs_", "minimax_", "clone_", "edge_", "kokoro_", "vbee_", "fishaudio_")
    if not any(voice_id.startswith(p) for p in KNOWN_PREFIXES):
        voice_id = f"elevenlabs_{voice_id}"
        logger.debug(f"ai33.pro v3 TTS: auto-prefixed voice_id to {voice_id}")
    settings = voice_settings or {}
    form = {
        "text": text,
        "voice_id": voice_id,
        "speed": str(settings.get("speed", 1.0)),
        "with_transcript": "false",
    }
    resp = _post_with_retry(
        client, f"{AI33PRO_BASE_URL}/v3/text-to-speech",
        headers=_headers(api_key), data=form, timeout=30.0,
    )
    _raise_for_status_with_detail(resp, "ai33.pro POST /v3/text-to-speech")
    return resp.json()["task_id"]


def submit_stt_with_webhook(
    client: httpx.Client,
    audio_path: Path,
    api_key: Optional[str] = None,
    tag_audio_events: bool = True,
) -> str:
    """POST /v1/task/speech-to-text with receive_url set to KappGen's own
    webhook (src/api/routes/webhooks.py) — direct polling of this endpoint is
    documented (in Izivoice's own source, which hit this in production) as
    unreliable ("server_busy" on most keys), so the result is delivered by
    webhook instead of polled here. Returns task_id; pair with
    await_stt_webhook_result()."""
    with open(audio_path, "rb") as f:
        resp = _post_with_retry(
            client, f"{AI33PRO_BASE_URL}/v1/task/speech-to-text",
            headers=_headers(api_key),
            files={"file": (audio_path.name, f, "audio/mpeg")},
            data={
                "tag_audio_events": "true" if tag_audio_events else "false",
                "receive_url": f"{BACKEND_BASE_URL}/api/webhooks/ai33/speech-to-text",
            },
            timeout=60.0,
        )
    resp.raise_for_status()
    return resp.json()["task_id"]


def await_stt_webhook_result(task_id: str, timeout: float = STT_WEBHOOK_WAIT_TIMEOUT_SECONDS, *, client: Optional[httpx.Client] = None, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Waits for src/api/routes/webhooks.py's ai33 STT webhook to have
    recorded a terminal result for `task_id` in the Ai33TaskResult table —
    converts ai33.pro's unreliable direct polling into reliable polling
    against KappGen's own database instead. Returns the task's `metadata`
    dict (or raises on 'error'/timeout), same shape callers already expect
    from a polled task's metadata field."""
    from src.db.session import SessionLocal
    from src.db.models import Ai33TaskResult

    deadline = time.monotonic() + timeout
    next_probe = time.monotonic() + 15.0
    interval = 2.0
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            row = db.query(Ai33TaskResult).filter(Ai33TaskResult.task_id == task_id).first()
            if row:
                if row.status == "done":
                    return (row.payload or {}).get("metadata") or {}
                if row.status in ("error", "failed"):
                    raise RuntimeError(f"ai33.pro STT task {task_id} failed: {row.payload}")
        finally:
            db.close()
        # Webhooks remain primary. A missed callback must not hide an
        # already completed task; transient polling errors never abort STT.
        if client is not None and time.monotonic() >= next_probe:
            task = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = client.get(
                    f"{AI33PRO_BASE_URL}/v1/task/{task_id}",
                    headers=_headers(api_key), timeout=min(10.0, remaining),
                )
                response.raise_for_status()
                task = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("STT status probe unavailable for %s: %s", task_id, exc)
            if isinstance(task, dict):
                status = str(task.get("status") or "").lower()
                if status == "done" and task.get("metadata"):
                    return task["metadata"]
                if status in ("error", "failed"):
                    raise RuntimeError(f"ai33.pro STT task {task_id} failed: {task.get('error') or task}")
            next_probe = time.monotonic() + 15.0
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(f"ai33.pro STT webhook for task {task_id} did not arrive within {timeout}s")


def submit_music_generation(
    client: httpx.Client,
    prompt: str,
    make_instrumental: bool = True,
    api_key: Optional[str] = None,
    lyrics: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[str] = None,
    vocal_gender: Optional[str] = None,
) -> str:
    """POST /v1s/task/music-generation (JSON, not FormData — confirmed
    against Izivoice's own source, src/app/api/music/route.ts in the
    izivoice repo: that route is a thin passthrough straight to this same
    ai33.pro path).

    "simple" create_mode (the default) generates from a text description
    only, always instrumental — right for background music behind
    narration. Passing `lyrics` switches to "custom" mode instead (title/
    lyrics/tags/vocal_gender, confirmed against the same izivoice route:
    "custom" requires lyrics or tags) — a real song with vocals, for the
    Vidéo Musicale product where the content IS the song. Poll with
    poll_task(); a "done" task's metadata.audio_url is the generated track,
    same shape Izivoice's own wrapper returns."""
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
            "make_instrumental": make_instrumental,
        }
    resp = _post_with_retry(
        client, f"{AI33PRO_BASE_URL}/v1s/task/music-generation",
        headers={**_headers(api_key), "Content-Type": "application/json"},
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("task_id"):
        raise RuntimeError(f"Unexpected ai33.pro music-generation response: {data}")
    return data["task_id"]


def submit_image_generation(
    client: httpx.Client,
    prompt: str,
    model_id: str = "gpt-image-2",
    generations_count: int = 1,
    model_parameters: Optional[Dict[str, Any]] = None,
    reference_image_paths: Optional[List[Path]] = None,
    api_key: Optional[str] = None,
) -> str:
    """POST /v1i/task/generate-image (FormData) — returns task_id. Poll with
    poll_task(); a "done" task's metadata.result_images[0].imageUrl is the
    generated image, same shape Izivoice's own public docs describe for its
    /api/images wrapper around this exact endpoint (docs/image-api.fr.md,
    izivoice repo — confirmed against src/app/api/images/generate/route.ts
    there: same base path `/v1i/task/generate-image`, same `xi-api-key`
    header, same field names, Izivoice just forwards the multipart body
    verbatim).

    Reference images ride in `assets`, referenced from the prompt via
    `@img1`, `@img2`, ... in file order — required by ai33.pro's own
    validation (Izivoice's route rejects a request whose @imgN references
    don't exactly match the asset count), unlike Izivoice's own
    best-effort `reference_images` field name used elsewhere in this
    codebase's direct-Izivoice path (images.py's _submit_izivoice_image_task,
    a guess since Izivoice doesn't publish that internal schema)."""
    params = dict(model_parameters or {"aspect_ratio": "16:9", "resolution": "2K"})
    existing_paths = [p for p in (reference_image_paths or []) if p.exists()][:10]
    # ai33.pro (via Izivoice's own validation, which forwards these fields
    # verbatim) rejects the request unless the prompt's @imgN references
    # exactly match the asset count — append them rather than trust every
    # caller to remember the exact token syntax.
    final_prompt = prompt
    if existing_paths and not any(f"@img{i + 1}" in prompt for i in range(len(existing_paths))):
        refs = " ".join(f"@img{i + 1}" for i in range(len(existing_paths)))
        final_prompt = f"{prompt}, using {refs} as style/character reference"
    open_files = []
    parts: List[tuple] = [
        ("prompt", (None, final_prompt)),
        ("model_id", (None, model_id)),
        ("generations_count", (None, str(generations_count))),
        ("model_parameters", (None, json.dumps(params))),
    ]
    try:
        for path in existing_paths:
            fh = open(path, "rb")
            open_files.append(fh)
            media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            parts.append(("assets", (path.name, fh, media_type)))
        resp = _post_with_retry(
            client, f"{AI33PRO_BASE_URL}/v1i/task/generate-image",
            headers=_headers(api_key), files=parts, timeout=30.0,
        )
    finally:
        for fh in open_files:
            fh.close()
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("task_id"):
        raise RuntimeError(f"Unexpected ai33.pro generate-image response: {data}")
    return data["task_id"]
