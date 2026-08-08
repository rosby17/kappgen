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
STT_CHUNK_SECONDS = 600  # 10 min/chunk keeps files well under Izivoice's 50MB STT limit

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


def _izivoice_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {IZIVOICE_API_KEY}"}


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


def _poll_task(task_id: str, client: httpx.Client) -> Dict[str, Any]:
    """Polls GET /tasks/{task_id} until status is 'done' or 'error' (or timeout)."""
    elapsed = 0.0
    while elapsed < TASK_POLL_TIMEOUT_SECONDS:
        resp = client.get(f"{IZIVOICE_BASE_URL}/tasks/{task_id}", headers=_izivoice_headers(), timeout=30.0)
        resp.raise_for_status()
        task = resp.json()
        status = task.get("status")
        if status == "done":
            return task
        if status == "error":
            raise RuntimeError(f"Izivoice task {task_id} failed: {task.get('error') or task}")
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
        elapsed += TASK_POLL_INTERVAL_SECONDS
    raise TimeoutError(f"Izivoice task {task_id} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def _get_default_voice_id(client: httpx.Client) -> str:
    """Returns the configured voice_id, or auto-picks the first available voice and caches it."""
    global _cached_voice_id
    if IZIVOICE_VOICE_ID:
        return IZIVOICE_VOICE_ID
    if _cached_voice_id:
        return _cached_voice_id

    resp = client.get(
        f"{IZIVOICE_BASE_URL}/voices",
        headers=_izivoice_headers(),
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
            headers=_izivoice_headers(),
            params={"page": 1, "page_size": 1},
            timeout=30.0
        )
        resp.raise_for_status()
        voices = ((resp.json().get("data") or {}).get("voices")) or []

    if not voices:
        raise RuntimeError("No voice_id configured and Izivoice /voices returned no voices to auto-select.")

    _cached_voice_id = voices[0]["voice_id"]
    logger.info(f"Auto-selected Izivoice voice_id={_cached_voice_id} ({voices[0].get('name')})")
    return _cached_voice_id


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


def _extract_words_from_stt_metadata(metadata: Dict[str, Any]) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    Best-effort parsing of a completed speech-to-text task's `metadata` field.
    Izivoice's docs don't publish the exact completed shape, so this tries a few
    reasonable layouts and falls back gracefully when only plain text is available.
    """
    text = metadata.get("text") or metadata.get("transcript") or ""

    raw_words = metadata.get("words")
    if isinstance(raw_words, list) and raw_words:
        words = []
        for w in raw_words:
            word = w.get("word") or w.get("text")
            start = w.get("start", w.get("start_time"))
            end = w.get("end", w.get("end_time"))
            if word is not None and start is not None and end is not None:
                words.append({"word": word, "start": float(start), "end": float(end)})
        if words:
            return text, words

    segments = metadata.get("segments")
    if isinstance(segments, list) and segments:
        words = []
        full_text_parts = []
        for seg in segments:
            seg_words = seg.get("words")
            if isinstance(seg_words, list) and seg_words:
                for w in seg_words:
                    word = w.get("word") or w.get("text")
                    start = w.get("start", w.get("start_time"))
                    end = w.get("end", w.get("end_time"))
                    if word is not None and start is not None and end is not None:
                        words.append({"word": word, "start": float(start), "end": float(end)})
            else:
                seg_text = seg.get("text", "")
                seg_start = seg.get("start", seg.get("start_time", 0.0))
                seg_end = seg.get("end", seg.get("end_time", seg_start))
                full_text_parts.append(seg_text)
                if seg_text and seg_end > seg_start:
                    for w in synthetic_word_timings(seg_text, seg_end - seg_start):
                        words.append({
                            "word": w["word"],
                            "start": round(seg_start + w["start"], 2),
                            "end": round(seg_start + w["end"], 2)
                        })
        if not text:
            text = " ".join(full_text_parts)
        if words:
            return text, words

    return text, None


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


def transcribe_audio_izivoice(audio_path: Path, fallback_text: str = "") -> Dict[str, Any]:
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

    try:
        with httpx.Client() as client:
            for chunk_path in chunks:
                chunk_duration = get_audio_duration(chunk_path)
                logger.info(f"Transcribing chunk {chunk_path.name} ({chunk_duration:.1f}s, offset={offset:.1f}s)...")

                with open(chunk_path, "rb") as f:
                    resp = _post_with_retry(
                        client,
                        f"{IZIVOICE_BASE_URL}/speech-to-text",
                        headers=_izivoice_headers(),
                        files={"file": (chunk_path.name, f, "audio/mpeg")},
                        timeout=60.0
                    )
                resp.raise_for_status()
                task_id = resp.json()["task_id"]
                task = _poll_task(task_id, client)
                metadata = task.get("metadata", {}) or {}

                chunk_text, chunk_words = _extract_words_from_stt_metadata(metadata)
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
    finally:
        if chunk_dir.exists() and chunk_dir != audio_path.parent:
            for f in chunk_dir.glob("*"):
                f.unlink(missing_ok=True)
            chunk_dir.rmdir()

    full_text = " ".join(p for p in all_text_parts if p).strip() or fallback_text
    if not all_words:
        all_words = synthetic_word_timings(full_text, total_duration)

    return {"text": full_text, "duration": total_duration, "words": all_words}


def generate_transcript_for_audio(audio_path: Path, fallback_text: str = "") -> Dict[str, Any]:
    """
    Public entrypoint used by the orchestrator for pre-recorded/uploaded audio:
    real transcription via Izivoice speech-to-text when configured, else a
    synthetic even-split alignment over the fallback title/text.
    """
    if not IZIVOICE_API_KEY:
        logger.info("IZIVOICE_API_KEY not set. Using synthetic subtitle timing for uploaded audio.")
        duration = get_audio_duration(audio_path)
        return {
            "text": fallback_text or "Audio préenregistré",
            "duration": duration,
            "words": synthetic_word_timings(fallback_text or "Audio préenregistré", duration)
        }

    try:
        return transcribe_audio_izivoice(audio_path, fallback_text=fallback_text)
    except Exception as e:
        logger.warning(f"Izivoice speech-to-text failed ({e}). Falling back to synthetic subtitle timing.")
        duration = get_audio_duration(audio_path)
        return {
            "text": fallback_text or "Audio préenregistré",
            "duration": duration,
            "words": synthetic_word_timings(fallback_text or "Audio préenregistré", duration)
        }


def generate_voiceover(script_text: str, output_audio_path: Path) -> Tuple[Path, Dict[str, Any]]:
    """
    Generates voiceover TTS audio via the Izivoice API (or local fallback when no key is set),
    then derives word-level subtitle timing via Izivoice speech-to-text on the resulting audio
    (Izivoice's /text-to-speech does not return alignment data itself).
    """
    script_text = clean_script_text(script_text)

    if not IZIVOICE_API_KEY:
        logger.info("IZIVOICE_API_KEY not set. Using local TTS fallback.")
        return generate_mock_voiceover(script_text, output_audio_path)

    try:
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client() as client:
            voice_id = _get_default_voice_id(client)

            logger.info("Requesting voiceover from Izivoice /text-to-speech...")
            resp = _post_with_retry(
                client,
                f"{IZIVOICE_BASE_URL}/text-to-speech",
                headers=_izivoice_headers(),
                json={"text": script_text, "voice_id": voice_id},
                timeout=30.0
            )
            resp.raise_for_status()
            task_id = resp.json()["task_id"]

            task = _poll_task(task_id, client)
            audio_url = (task.get("metadata") or {}).get("audio_url")
            if not audio_url:
                raise ValueError(f"Unexpected Izivoice text-to-speech response: {task}")

            audio_resp = client.get(audio_url, timeout=60.0)
            audio_resp.raise_for_status()
            output_audio_path.write_bytes(audio_resp.content)

        transcript_info = transcribe_audio_izivoice(output_audio_path, fallback_text=script_text)
        return output_audio_path, transcript_info

    except Exception as e:
        logger.warning(f"Izivoice API call failed ({e}). Falling back to local TTS mock generator.")
        return generate_mock_voiceover(script_text, output_audio_path)
