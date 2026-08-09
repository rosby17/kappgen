import os
from pathlib import Path
from src.utils.ffmpeg_runner import run_ffmpeg
from src.utils.logger import logger

SD_RESOLUTION = "854:480"


def sd_variant_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}_sd.mp4")


def ensure_sd_variant(source_path: Path) -> Path:
    """
    Returns the SD (480p) version of a rendered video, downscaling and caching
    it next to the original if it doesn't exist yet. Called proactively right
    after a render finishes (so it's usually already sitting on disk by the
    time someone clicks download) and defensively from the download endpoint
    itself as a fallback if it isn't there yet.
    """
    cached_path = sd_variant_path(source_path)
    if cached_path.exists():
        return cached_path

    temp_path = cached_path.with_name(f".{cached_path.stem}.part.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(source_path),
        "-vf", f"scale={SD_RESOLUTION}:flags=lanczos",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart",
        str(temp_path)
    ]
    try:
        run_ffmpeg(cmd)
        os.replace(temp_path, cached_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return cached_path


def try_ensure_sd_variant(source_path: Path) -> None:
    """Best-effort SD pre-generation — a failure here must never fail the render itself."""
    try:
        ensure_sd_variant(source_path)
    except Exception as e:
        logger.warning(f"Non-fatal: failed to pre-generate SD variant for {source_path}: {e}")
