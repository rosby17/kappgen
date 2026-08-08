import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from src.utils.logger import logger

class FFmpegError(Exception):
    pass

def run_ffmpeg(command: List[str], check: bool = True) -> subprocess.CompletedProcess:
    """
    Executes an FFmpeg command with proper error handling and logging.
    """
    cmd_str = " ".join(command)
    logger.info(f"Executing FFmpeg command: {cmd_str}")
    
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    if check and process.returncode != 0:
        logger.error(f"FFmpeg failed with code {process.returncode}:\n{process.stderr}")
        raise FFmpegError(f"FFmpeg command failed: {process.stderr}")
        
    return process

def get_audio_duration(file_path: Path) -> float:
    """
    Returns the duration of an audio/video file in seconds using ffprobe.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode != 0:
        logger.warning(f"Could not read duration for {file_path}: {process.stderr}")
        return 0.0
    try:
        return float(process.stdout.strip())
    except ValueError:
        return 0.0

def get_media_info(file_path: Path) -> Dict[str, Any]:
    """
    Extracts detailed media info using ffprobe JSON output.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path)
    ]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode == 0:
        try:
            return json.loads(process.stdout)
        except Exception:
            pass
    return {}

def validate_audio_file(file_path: Path) -> Dict[str, Any]:
    """Rejects renamed/corrupt files before they enter the render queue."""
    info = get_media_info(file_path)
    audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if not audio_streams or duration <= 0:
        raise ValueError("Le fichier envoyé ne contient pas de piste audio valide ou il est corrompu.")
    return info
