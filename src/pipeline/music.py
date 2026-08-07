from pathlib import Path
from typing import Optional
from src.config import ASSETS_PATH
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg

def get_background_music_track(style: str = "ambient", duration: float = 60.0, output_path: Optional[Path] = None) -> Path:
    """
    Returns a path to a background music file.
    If no pre-existing track exists, creates a subtle ambient music track using FFmpeg audio synth.
    """
    music_dir = ASSETS_PATH / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    
    existing = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.wav"))
    if existing:
        return existing[0]
        
    if output_path is None:
        output_path = music_dir / f"ambient_track_{style}.mp3"
        
    if not output_path.exists():
        logger.info(f"Generating synthetic background ambient track ({duration:.1f}s)...")
        # Generates a soft harmonic chord tone (440Hz + 554Hz) with low volume
        filter_str = f"sine=frequency=110:duration={duration:.1f},volume=0.2"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", filter_str,
            "-c:a", "libmp3lame",
            str(output_path)
        ]
        run_ffmpeg(cmd)
        
    return output_path
