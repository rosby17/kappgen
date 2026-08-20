import re
import time
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import httpx
from src.config import IZIVOICE_API_KEY, IZIVOICE_BASE_URL, IZIVOICE_VOICE_ID
from src.utils.logger import logger
from src.utils.ffmpeg_runner import get_audio_duration, run_ffmpeg

TASK_POLL_INTERVAL_SECONDS = 2.5
TASK_POLL_TIMEOUT_SECONDS = 600  # per task (TTS call or one STT chunk)
STT_CHUNK_SECONDS = 300  # 5 min/chunk — smaller chunks are less likely to trip
                          # Izivoice's STT into a 500 (observed on 10-min chunks)

_cached_voice_id: Optional[str] = None


def clean_script_text(script: str) -> str:
    """Removes extra whitespace and cleans script text."""
    return re.sub(r'\s+', ' ', script).strip()


def synthetic_word_timings(text: str, duration: float) -> List[Dict[str, Any]]:
    """Evenly distributes words of `text` across `duration` seconds. Used whenever
    a provider gives us audio/duration but no real per-word alignment."""
    words = clean_script_text(text).split()
    if not words:
        words = ["Audio"]
    duration = max(duration, 0.5)
    word_duration = duration / len(words)
    timings = []
    for i, word in enumerate(words):
        timings.append({
            "word": word,
            "start": round(i * word_duration, 2),
            "end": round((i + 1) * word_duration, 2)
        })
    return timings


def _izivoice_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key or IZIVOICE_API_KEY}"}


def _post_with_retry(client: httpx.Client, url: str, max_retries: int = 5, **kwargs) -> httpx.Response:
    """POSTs with exponential backoff retry on 429/5xx (Izivoice rate-limits aggressively
    when several videos render concurrently, since each one fires its own TTS/STT calls)."""
    delay = 3.0
    for attempt in range(max_retries + 1):
        resp = client.post(url, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                resp.raise_for_status()
            logger.warning(f"Izivoice request to {url} returned {resp.status_code}, retrying in {delay:.0f}s...")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        return resp
    return resp


def _poll_task(task_id: str, client: httpx.Client, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Polls GET /tasks/{task_id} until status is 'done' or 'error' (or timeout).
    Transient 5xx/429 responses (observed intermittently from Izivoice) are
    retried instead of aborting the whole transcription — the underlying task
    is usually still processing fine server-side."""
    elapsed = 0.0
    while elapsed < TASK_POLL_TIMEOUT_SECONDS:
        try:
            resp = client.get(f"{IZIVOICE_BASE_URL}/tasks/{task_id}", headers=_izivoice_headers(api_key), timeout=30.0)
            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning(f"Izivoice task poll for {task_id} returned {resp.status_code}, retrying...")
                time.sleep(TASK_POLL_INTERVAL_SECONDS)
                elapsed += TASK_POLL_INTERVAL_SECONDS
                continue
            resp.raise_for_status()
            task = resp.json()
        except httpx.TransportError as e:
            logger.warning(f"Izivoice task poll for {task_id} transport error ({e}), retrying...")
            time.sleep(TASK_POLL_INTERVAL_SECONDS)
            elapsed += TASK_POLL_INTERVAL_SECONDS
            continue
        status = task.get("status")
        if status == "done":
            return task
        if status == "error":
            raise RuntimeError(f"Izivoice task {task_id} failed: {task.get('error') or task}")
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
        elapsed += TASK_POLL_INTERVAL_SECONDS
    raise TimeoutError(f"Izivoice task {task_id} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def _get_default_voice_id(client: httpx.Client, api_key: Optional[str] = None) -> str:
    """Returns the configured voice_id, or auto-picks the first available voice and caches it."""
    global _cached_voice_id
    if IZIVOICE_VOICE_ID:
        return IZIVOICE_VOICE_ID
    # The global cache belongs only to NicheCut's shared account. Reusing it
    # for BYOK accounts could select a voice that does not exist for that user.
    is_personal_key = bool(api_key and api_key != IZIVOICE_API_KEY)
    if _cached_voice_id and not is_personal_key:
        return _cached_voice_id

    resp = client.get(
        f"{IZIVOICE_BASE_URL}/voices",
        headers=_izivoice_headers(api_key),
        params={"page": 1, "page_size": 1, "language": "fr"},
        timeout=30.0
    )
    resp.raise_for_status()
    data = resp.json()
    voices = (data.get("data") or {}).get("voices") or []
    if not voices:
        # Retry without language filter as a fallback
        resp = client.get(
            f"{IZIVOICE_BASE_URL}/voices",
            headers=_izivoice_headers(api_key),
            params={"page": 1, "page_size": 1},
            timeout=30.0
        )
        resp.raise_for_status()
        voices = ((resp.json().get("data") or {}).get("voices")) or []

    if not voices:
        raise RuntimeError("No voice_id configured and Izivoice /voices returned no voices to auto-select.")

    selected_id = voices[0]["voice_id"]
    if not is_personal_key:
        _cached_voice_id = selected_id
    logger.info(f"Auto-selected Izivoice voice_id={selected_id} ({voices[0].get('name')})")
    return selected_id


def generate_mock_voiceover(script_text: str, output_audio_path: Path) -> Tuple[Path, Dict[str, Any]]:
    """
    Fallback mock generator when no Izivoice key is available.
    Generates spoken audio (via macOS `say` or FFmpeg synthetic speech tone) and alignment metadata.
    """
    script_text = clean_script_text(script_text) or "Bienvenue sur cette vidéo"
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)

    # Try using macOS builtin `say` tool for realistic spoken test audio if available
    mac_say = subprocess.run(["which", "say"], capture_output=True, text=True)
    if mac_say.returncode == 0:
        aiff_path = output_audio_path.with_suffix(".aiff")
        subprocess.run(["say", "-v", "Thomas", "-o", str(aiff_path), script_text])
        if aiff_path.exists():
            # Convert AIFF to MP3/WAV using ffmpeg
            run_ffmpeg(["ffmpeg", "-y", "-i", str(aiff_path), str(output_audio_path)])
            if aiff_path.exists():
                aiff_path.unlink()

    # If `say` failed or unavailable, fallback to FFmpeg synthetic tone audio
    if not output_audio_path.exists():
        estimated_duration = max(3.0, len(script_text.split()) / 2.5)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=220:duration={estimated_duration:.2f}",
            "-c:a", "libmp3lame",
            str(output_audio_path)
        ]
        run_ffmpeg(cmd)

    total_duration = get_audio_duration(output_audio_path)
    if total_duration <= 0:
        total_duration = max(3.0, len(script_text.split()) / 2.5)

    word_timings = synthetic_word_timings(script_text, total_duration)

    mock_transcript_json = {
        "text": script_text,
        "duration": total_duration,
        "words": word_timings
    }

    logger.info(f"Generated mock voiceover audio ({total_duration:.2f}s) and timing JSON.")
    return output_audio_path, mock_transcript_json


def _extract_words_from_stt_metadata(metadata: Dict[str, Any], client: httpx.Client) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    Parses a completed speech-to-text task's `metadata` field. Izivoice's STT
    task metadata does NOT include the transcript inline — it only provides a
    `json_url` (and `srt_url`) pointing to the actual result, which is a JSON
    array of language segments, each with a `text` and a `words` list (entries
    typed "word" or "spacing" — only "word" entries carry real timing).
    """
    json_url = metadata.get("json_url")
    if not json_url:
        return metadata.get("text") or metadata.get("transcript") or "", None

    resp = client.get(json_url, timeout=30.0)
    resp.raise_for_status()
    raw_body = resp.text

    try:
        segments = resp.json()
    except ValueError:
        # Izivoice has been observed serving this json_url genuinely truncated
        # at the source (confirmed: reproduces identically across HTTP/1.1,
        # HTTP/2, and with compression disabled — not a decoding bug on our
        # end). The "text" field of each segment appears early in the
        # document, well before the (much longer) per-word timing array where
        # the cut happens, so it's usually still intact even when the overall
        # JSON is broken. Recover it with a regex rather than falling back to
        # showing the video's title as the subtitle.
        recovered = re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_body)
        # First match is usually the top-level language segment's full text;
        # skip 1-2 char entries which are just word/spacing fragments.
        full_texts = [t for t in recovered if len(t) > 3]
        # resp.text is already correctly-decoded unicode (httpx handles the
        # charset) — do NOT re-decode it as unicode_escape, that mangles
        # accented characters (was producing "chaÃ®ne" instead of "chaîne").
        text = " ".join(full_texts[:1]).strip() if full_texts else ""
        text = text.replace('\\"', '"').replace("\\\\", "\\")

        # Also recover REAL per-word timing for whichever word objects made it
        # through intact before the cut (truncation happens at some point deep
        # in the "words" array, so most of it is usually still there). Without
        # this, the caller falls back to synthetic even-split timing, which
        # visibly drifts out of sync with the actual voiceover on longer clips.
        word_objs = re.findall(
            r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"start"\s*:\s*([\d.]+)\s*,\s*"end"\s*:\s*([\d.]+)\s*,\s*"type"\s*:\s*"word"',
            raw_body,
        )
        words = [
            {"word": w.replace('\\"', '"').strip(), "start": float(s), "end": float(e)}
            for w, s, e in word_objs if w.strip()
        ]

        logger.warning(
            f"Izivoice STT json_url returned truncated/invalid JSON; recovered plain text via regex "
            f"({'ok' if text else 'failed'}) and {len(words)} word timing(s)."
        )
        return text, (words or None)

    if not isinstance(segments, list):
        return "", None

    words = []
    full_text_parts = []
    for seg in segments:
        seg_text = seg.get("text") or ""
        if seg_text:
            full_text_parts.append(seg_text)
        for w in seg.get("words") or []:
            if w.get("type") != "word":
                continue
            word = w.get("text")
            start = w.get("start")
            end = w.get("end")
            if word is not None and start is not None and end is not None and word.strip():
                words.append({"word": word.strip(), "start": float(start), "end": float(end)})

    return " ".join(full_text_parts).strip(), (words or None)


def _split_audio_for_stt(audio_path: Path, chunk_dir: Path) -> List[Path]:
    """Splits long audio into <= STT_CHUNK_SECONDS chunks to respect Izivoice's 50MB file limit."""
    total_duration = get_audio_duration(audio_path)
    if total_duration <= STT_CHUNK_SECONDS:
        return [audio_path]

    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunk_dir / "chunk_%03d.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(STT_CHUNK_SECONDS),
        "-c", "copy", str(pattern)
    ]
    run_ffmpeg(cmd)
    return sorted(chunk_dir.glob("chunk_*.mp3"))


def transcribe_audio_izivoice(audio_path: Path, fallback_text: str = "", api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Transcribes an audio file via Izivoice's speech-to-text, chunking long files
    (10min+/3h videos) to stay under the API's per-request size limit, and stitching
    word-level timing back together with per-chunk offsets.
    """
    total_duration = get_audio_duration(audio_path)
    chunk_dir = audio_path.parent / f"{audio_path.stem}_stt_chunks"
    chunks = _split_audio_for_stt(audio_path, chunk_dir)

    all_words: List[Dict[str, Any]] = []
    all_text_parts: List[str] = []
    offset = 0.0
    failed_chunks = 0

    try:
        with httpx.Client() as client:
            for chunk_path in chunks:
                chunk_duration = get_audio_duration(chunk_path)
                logger.info(f"Transcribing chunk {chunk_path.name} ({chunk_duration:.1f}s, offset={offset:.1f}s)...")

                try:
                    with open(chunk_path, "rb") as f:
                        resp = _post_with_retry(
                            client,
                            f"{IZIVOICE_BASE_URL}/speech-to-text",
                            headers=_izivoice_headers(api_key),
                            files={"file": (chunk_path.name, f, "audio/mpeg")},
                            timeout=60.0
                        )
                    resp.raise_for_status()
                    task_id = resp.json()["task_id"]
                    task = _poll_task(task_id, client, api_key)
                    metadata = task.get("metadata", {}) or {}
                    chunk_text, chunk_words = _extract_words_from_stt_metadata(metadata, client)
                except Exception as chunk_err:
                    # One chunk failing (Izivoice has returned 500s on some
                    # requests) must not throw away transcript already
                    # recovered from the OTHER chunks — previously any single
                    # failure fell back to showing the video's filename/title
                    # as "subtitles" for the entire video. Skip just this
                    # segment (no subtitle line during its span) instead.
                    failed_chunks += 1
                    logger.warning(f"Transcription failed for chunk {chunk_path.name} ({chunk_err}); leaving this segment without subtitles rather than discarding the rest of the transcript.")
                    offset += chunk_duration
                    continue

                all_text_parts.append(chunk_text)

                if chunk_words:
                    for w in chunk_words:
                        all_words.append({
                            "word": w["word"],
                            "start": round(w["start"] + offset, 2),
                            "end": round(w["end"] + offset, 2)
                        })
                elif chunk_text:
                    for w in synthetic_word_timings(chunk_text, chunk_duration):
                        all_words.append({
                            "word": w["word"],
                            "start": round(w["start"] + offset, 2),
                            "end": round(w["end"] + offset, 2)
                        })

                offset += chunk_duration

        if failed_chunks == len(chunks):
            raise RuntimeError(f"All {len(chunks)} transcription chunk(s) failed.")
        if failed_chunks:
            logger.warning(f"{failed_chunks}/{len(chunks)} transcription chunks failed and were skipped; the rest of the transcript is real.")
    finally:
        if chunk_dir.exists() and chunk_dir != audio_path.parent:
            for f in chunk_dir.glob("*"):
                f.unlink(missing_ok=True)
            chunk_dir.rmdir()

    full_text = " ".join(p for p in all_text_parts if p).strip() or fallback_text
    if not all_words:
        all_words = synthetic_word_timings(full_text, total_duration)

    return {"text": full_text, "duration": total_duration, "words": all_words}


def generate_transcript_for_audio(audio_path: Path, fallback_text: str = "", api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Public entrypoint used by the orchestrator for pre-recorded/uploaded audio:
    real transcription via Izivoice speech-to-text when configured, else a
    synthetic even-split alignment over the fallback title/text.
    """
    if not (api_key or IZIVOICE_API_KEY):
        logger.info("IZIVOICE_API_KEY not set. Using synthetic subtitle timing for uploaded audio.")
        duration = get_audio_duration(audio_path)
        return {
            "text": fallback_text or "Audio préenregistré",
            "duration": duration,
            "words": synthetic_word_timings(fallback_text or "Audio préenregistré", duration)
        }

    try:
        return transcribe_audio_izivoice(audio_path, fallback_text=fallback_text, api_key=api_key)
    except Exception as e:
        logger.warning(f"Izivoice speech-to-text failed ({e}). Falling back to synthetic subtitle timing.")
        duration = get_audio_duration(audio_path)
        return {
            "text": fallback_text or "Audio préenregistré",
            "duration": duration,
            "words": synthetic_word_timings(fallback_text or "Audio préenregistré", duration)
        }


def generate_voiceover(script_text: str, output_audio_path: Path, voice_id: Optional[str] = None, api_key: Optional[str] = None, voice_settings: Optional[Dict[str, Any]] = None) -> Tuple[Path, Dict[str, Any]]:
    """
    Generates voiceover TTS audio via the Izivoice API (or local fallback when no key is set),
    then derives word-level subtitle timing via Izivoice speech-to-text on the resulting audio
    (Izivoice's /text-to-speech does not return alignment data itself).
    """
    script_text = clean_script_text(script_text)

    effective_key = api_key or IZIVOICE_API_KEY
    if not effective_key:
        logger.info("IZIVOICE_API_KEY not set. Using local TTS fallback.")
        return generate_mock_voiceover(script_text, output_audio_path)

    try:
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client() as client:
            voice_id = voice_id or _get_default_voice_id(client, effective_key)

            logger.info("Requesting voiceover from Izivoice /text-to-speech...")
            resp = _post_with_retry(
                client,
                f"{IZIVOICE_BASE_URL}/text-to-speech",
                headers=_izivoice_headers(effective_key),
                data={
                    "text": script_text,
                    "voice_id": voice_id,
                    "speed": str((voice_settings or {}).get("speed", 0.845)),
                    "with_transcript": "false",
                    "stability": str((voice_settings or {}).get("stability", 0.8)),
                    "similarity_boost": str((voice_settings or {}).get("similarity_boost", 0.9)),
                    "style": str((voice_settings or {}).get("style", 0.0)),
                },
                timeout=30.0
            )
            resp.raise_for_status()
            task_id = resp.json()["task_id"]

            task = _poll_task(task_id, client, effective_key)
            audio_url = (task.get("metadata") or {}).get("audio_url")
            if not audio_url:
                raise ValueError(f"Unexpected Izivoice text-to-speech response: {task}")

            audio_resp = client.get(audio_url, timeout=60.0)
            audio_resp.raise_for_status()
            output_audio_path.write_bytes(audio_resp.content)

        transcript_info = transcribe_audio_izivoice(output_audio_path, fallback_text=script_text, api_key=effective_key)
        return output_audio_path, transcript_info

    except Exception as e:
        logger.warning(f"Izivoice API call failed ({e}). Falling back to local TTS mock generator.")
        return generate_mock_voiceover(script_text, output_audio_path)
