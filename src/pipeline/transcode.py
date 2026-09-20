import os
from pathlib import Path

from src.utils.ffmpeg_runner import run_ffmpeg
from src.utils.logger import logger


# Download exports are separate from the master render. SD is optimized for
# sending/archiving, HD for ordinary web publishing, and the untouched master
# remains the Full HD download.
EXPORT_PRESETS = {
    "sd": {
        "suffix": "sd", "resolution": "854:480",
        "crf": "31", "maxrate": "800k", "bufsize": "1600k",
        "preset": "slow", "audio_bitrate": "64k",
    },
    "hd": {
        "suffix": "hd", "resolution": "1280:720",
        "crf": "25", "maxrate": "1800k", "bufsize": "3600k",
        "preset": "slow", "audio_bitrate": "112k",
    },
}
SD_RESOLUTION = EXPORT_PRESETS["sd"]["resolution"]


def export_variant_path(source_path: Path, quality: str) -> Path:
    preset = EXPORT_PRESETS[quality]
    return source_path.with_name(f"{source_path.stem}_{preset['suffix']}.mp4")


def sd_variant_path(source_path: Path) -> Path:
    return export_variant_path(source_path, "sd")


def ensure_export_variant(source_path: Path, quality: str, destination: Path | None = None) -> Path:
    """Create and cache a real compressed SD/HD export, never a disguised master."""
    if quality not in EXPORT_PRESETS:
        raise ValueError(f"Unsupported export quality: {quality}")
    preset = EXPORT_PRESETS[quality]
    cached_path = destination or export_variant_path(source_path, quality)
    if cached_path.exists() and cached_path.stat().st_size > 1024:
        return cached_path

    cached_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cached_path.with_name(f".{cached_path.stem}.part.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(source_path),
        "-vf", f"scale={preset['resolution']}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,fps=30",
        "-c:v", "libx264", "-preset", preset["preset"], "-crf", preset["crf"],
        "-maxrate", preset["maxrate"], "-bufsize", preset["bufsize"],
        "-c:a", "aac", "-b:a", preset["audio_bitrate"], "-ac", "2",
        "-movflags", "+faststart", str(temp_path),
    ]
    try:
        run_ffmpeg(cmd)
        os.replace(temp_path, cached_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return cached_path


def ensure_sd_variant(source_path: Path) -> Path:
    return ensure_export_variant(source_path, "sd")


def try_ensure_sd_variant(source_path: Path) -> None:
    """Best-effort SD pre-generation — a failure here must never fail the render itself."""
    try:
        ensure_sd_variant(source_path)
    except Exception as e:
        logger.warning(f"Non-fatal: failed to pre-generate SD variant for {source_path}: {e}")
