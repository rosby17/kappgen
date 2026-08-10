import os
from pathlib import Path
from src.utils.ffmpeg_runner import run_ffmpeg


def extracted_audio_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}_audio.m4a")


def ensure_extracted_audio(source_path: Path) -> Path:
    """
    Returns the voiceover+music audio track of a rendered video, pulling it out
    of the final MP4 and caching it next to the original. Used by the "reuse
    audio" flow so a new video can be generated from an existing render's
    soundtrack without re-running TTS.
    """
    cached_path = extracted_audio_path(source_path)
    if cached_path.exists():
        return cached_path

    temp_path = cached_path.with_name(f".{cached_path.stem}.part.m4a")
    cmd = [
        "ffmpeg", "-y", "-i", str(source_path),
        "-vn", "-c:a", "copy",
        str(temp_path)
    ]
    try:
        run_ffmpeg(cmd)
        os.replace(temp_path, cached_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return cached_path
