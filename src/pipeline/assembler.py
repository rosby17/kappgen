import os
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg

def check_ffmpeg_filter(filter_name: str) -> bool:
    """Checks if a specific filter is supported by system ffmpeg."""
    res = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True)
    return filter_name in res.stdout

def assemble_final_video(
    clip_paths: List[Path],
    audio_path: Path,
    subtitle_ass_path: Path,
    output_path: Path,
    effects_config: Optional[Dict[str, Any]] = None,
    branding_config: Optional[Dict[str, Any]] = None
) -> Path:
    """
    Concatenates motion clips, applies color grading/grain, burns subtitles, places logo, and multiplexes audio.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # Write concat list file
    concat_list_file = temp_dir / "clips_concat.txt"
    with open(concat_list_file, "w", encoding="utf-8") as f:
        for clip in clip_paths:
            clean_path = str(clip.resolve()).replace("'", "'\\''")
            f.write(f"file '{clean_path}'\n")
            
    effects = effects_config or {}
    branding = branding_config or {}
    
    video_filters = []
    
    # Color grading
    color_mode = effects.get("color_grade", "warm")
    if color_mode == "warm":
        video_filters.append("eq=gamma=1.05:saturation=1.15")
    elif color_mode == "vintage":
        video_filters.append("colorbalance=rs=0.1:gs=-0.05:bs=-0.1,eq=saturation=0.85")
    elif color_mode == "dramatic":
        video_filters.append("eq=contrast=1.2:saturation=0.9")

    # Film grain
    if effects.get("grain", True):
        video_filters.append("noise=alls=8:allf=t+u")
        
    # Check if FFmpeg build has libass 'subtitles' filter
    if check_ffmpeg_filter("subtitles"):
        ass_path_escaped = str(subtitle_ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        video_filters.append(f"subtitles=filename='{ass_path_escaped}'")
    else:
        logger.info("FFmpeg missing libass 'subtitles' filter; continuing video assembly without direct ASS filter burn.")

    vf_string = ",".join(video_filters) if video_filters else "null"
    
    # Handles Logo overlay if branding logo_path is present
    logo_path_str = branding.get("logo_path")
    has_logo = False
    if logo_path_str and Path(logo_path_str).exists():
        has_logo = True

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list_file)
    ]
    
    if has_logo:
        cmd.extend(["-i", str(Path(logo_path_str).resolve())])
        
    cmd.extend(["-i", str(audio_path.resolve())])

    if has_logo:
        filter_complex = f"[0:v]{vf_string}[v_base];[1:v]scale=120:-1[logo];[v_base][logo]overlay=W-w-40:40[outv]"
        cmd.extend(["-filter_complex", filter_complex, "-map", "[outv]", "-map", "2:a"])
    else:
        cmd.extend(["-vf", vf_string, "-map", "0:v", "-map", "1:a"])

    cmd.extend([
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(output_path)
    ])
    
    logger.info("Assembling final MP4 video with FFmpeg...")
    run_ffmpeg(cmd)
    
    if concat_list_file.exists():
        concat_list_file.unlink()
        
    logger.info(f"Final video successfully generated: {output_path}")
    return output_path
