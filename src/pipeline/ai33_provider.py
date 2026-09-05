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
    resp.raise_for_status()
    voices = resp.json().get("voices") or []
    if not voices:
        # Retry without the language filter as a last resort rather than
        # failing outright — better a non-French auto-pick than no voice at
        # all when explicitly none is configured.
        resp = client.get(
            f"{AI33PRO_BASE_URL}/v1/shared-voices",
            headers=_headers(api_key), params={"page": 0, "page_size": 5}, timeout=30.0,
        )
        resp.raise_for_status()
        voices = resp.json().get("voices") or []
    if not voices:
        raise RuntimeError("No voice_id configured and ai33.pro /v1/shared-voices returned no voices to auto-select.")
    selected_id = voices[0]["voice_id"]
    logger.info(f"Auto-selected ai33.pro voice_id={selected_id} ({voices[0].get('name')})")
    return selected_id


def submit_tts(
    client: httpx.Client,
    text: str,
    voice_id: str,
    voice_settings: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
) -> str:
    """POST /v3/text-to-speech (FormData, not JSON) — returns task_id. Poll
    with poll_task(); result's metadata.audio_url is populated the same way
    Izivoice's own wrapper relays it."""
    settings = voice_settings or {}
    form = {
        "text": text,
        "voice_id": voice_id,
        "speed": str(settings.get("speed", 0.845)),
        "with_transcript": "false",
        "stability": str(settings.get("stability", 0.8)),
        "similarity_boost": str(settings.get("similarity_boost", 0.9)),
        "style": str(settings.get("style", 0.0)),
        "use_speaker_boost": str(settings.get("use_speaker_boost", True)).lower(),
    }
    resp = _post_with_retry(
        client, f"{AI33PRO_BASE_URL}/v3/text-to-speech",
        headers=_headers(api_key), data=form, timeout=30.0,
    )
    resp.raise_for_status()
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


def await_stt_webhook_result(task_id: str, timeout: float = STT_WEBHOOK_WAIT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Waits for src/api/routes/webhooks.py's ai33 STT webhook to have
    recorded a terminal result for `task_id` in the Ai33TaskResult table —
    converts ai33.pro's unreliable direct polling into reliable polling
    against KappGen's own database instead. Returns the task's `metadata`
    dict (or raises on 'error'/timeout), same shape callers already expect
    from a polled task's metadata field."""
    from src.db.session import SessionLocal
    from src.db.models import Ai33TaskResult

    elapsed = 0.0
    interval = 2.0
    while elapsed < timeout:
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
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"ai33.pro STT webhook for task {task_id} did not arrive within {timeout}s")


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
