import os
import re
import json
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Tuple
import httpx
from src.config import IZIVOICE_API_KEY
from src.utils.logger import logger
from src.utils.ffmpeg_runner import get_audio_duration, run_ffmpeg

def clean_script_text(script: str) -> str:
    """Removes extra whitespace and cleans script text."""
    return re.sub(r'\s+', ' ', script).strip()

def generate_mock_voiceover(script_text: str, output_audio_path: Path) -> Tuple[Path, Dict[str, Any]]:
    """
    Fallback mock generator when no Izivoice key is available.
    Generates spoken audio (via macOS `say` or FFmpeg synthetic speech tone) and alignment metadata.
    """
    words = clean_script_text(script_text).split()
    if not words:
        words = ["Bienvenue", "sur", "cette", "vidéo"]
        
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
        # Estimate duration: ~2.5 words per second (150 WPM)
        estimated_duration = max(3.0, len(words) / 2.5)
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
        total_duration = max(3.0, len(words) / 2.5)

    # Generate synthetic word alignment timing
    word_duration = total_duration / len(words)
    word_timings = []
    for i, word in enumerate(words):
        start = i * word_duration
        end = (i + 1) * word_duration
        word_timings.append({
            "word": word,
            "start": round(start, 2),
            "end": round(end, 2)
        })

    mock_transcript_json = {
        "text": script_text,
        "duration": total_duration,
        "words": word_timings
    }
    
    logger.info(f"Generated mock voiceover audio ({total_duration:.2f}s) and timing JSON.")
    return output_audio_path, mock_transcript_json

def generate_voiceover(script_text: str, output_audio_path: Path) -> Tuple[Path, Dict[str, Any]]:
    """
    Generates voiceover TTS audio file and subtitle alignment metadata via Izivoice API or fallback.
    """
    script_text = clean_script_text(script_text)
    
    if not IZIVOICE_API_KEY:
        logger.info("IZIVOICE_API_KEY not set. Using local TTS fallback.")
        return generate_mock_voiceover(script_text, output_audio_path)
        
    try:
        logger.info("Requesting voiceover from Izivoice API...")
        response = httpx.post(
            "https://api.izivoice.com/v1/tts",
            headers={"Authorization": f"Bearer {IZIVOICE_API_KEY}"},
            json={"text": script_text, "format": "mp3", "alignments": True},
            timeout=60.0
        )
        response.raise_for_status()
        data = response.json()
        
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Download audio content if URL returned or byte content
        if "audio_url" in data:
            audio_resp = httpx.get(data["audio_url"])
            output_audio_path.write_bytes(audio_resp.content)
        elif "audio_bytes" in data:
            import base64
            output_audio_path.write_bytes(base64.b64decode(data["audio_bytes"]))
        else:
            raise ValueError("Unexpected Izivoice response format")
            
        transcript_info = {
            "text": script_text,
            "duration": get_audio_duration(output_audio_path),
            "words": data.get("words", [])
        }
        return output_audio_path, transcript_info
        
    except Exception as e:
        logger.warning(f"Izivoice API call failed ({e}). Falling back to mock generator.")
        return generate_mock_voiceover(script_text, output_audio_path)
