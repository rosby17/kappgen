from pathlib import Path
from typing import Optional
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg, get_audio_duration

def mix_audio_tracks(
    voiceover_path: Path,
    music_path: Optional[Path],
    output_audio_path: Path,
    music_volume: float = 0.15
) -> Path:
    """
    Mixes voiceover audio track with background music.
    Controls music relative volume and loops background music to fit voiceover length.
    """
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)
    vo_duration = get_audio_duration(voiceover_path)
    
    if not music_path or not music_path.exists() or music_volume <= 0:
        # If no music, copy voiceover directly or convert to target format
        cmd = [
            "ffmpeg", "-y",
            "-i", str(voiceover_path),
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(output_audio_path)
        ]
        run_ffmpeg(cmd)
        return output_audio_path
        
    # FFmpeg complex filter mixing voiceover (stream 0) and looping music (stream 1)
    # amix filter with volume control
    filter_complex = (
        f"[1:a]volume={music_volume:.2f},aloop=loop=-1:size=2e+09[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-i", str(voiceover_path),
        "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(output_audio_path)
    ]
    
    run_ffmpeg(cmd)
    logger.info(f"Mixed voiceover with background music ({music_volume*100:.0f}% volume) -> {output_audio_path}")
    return output_audio_path
