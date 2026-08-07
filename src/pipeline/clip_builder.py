from pathlib import Path
import random
from src.utils.ffmpeg_runner import run_ffmpeg, logger

def build_image_clip(
    image_path: Path,
    output_clip_path: Path,
    duration: float,
    zoom_min_pct: float = 1.0,
    zoom_max_pct: float = 1.12,
    fps: int = 30
) -> Path:
    """
    Creates a 1920x1080 video clip from a static image applying a Ken Burns (zoompan) motion effect.
    Alternates between zoom-in, zoom-out, and panning.
    """
    total_frames = int(duration * fps)
    if total_frames < 1:
        total_frames = 1
        
    # Alternate motion modes (zoom-in, zoom-out, pan left-to-right, pan right-to-left)
    motion_types = ["zoom_in", "zoom_out", "pan_right", "pan_left"]
    motion = random.choice(motion_types)
    
    # Calculate zoom expression for FFmpeg zoompan filter
    # z: current zoom level, d: duration in frames, x/y: pan coordinates
    if motion == "zoom_in":
        zoom_expr = f"min(pzoom+{(zoom_max_pct - zoom_min_pct)/total_frames:.6f},{zoom_max_pct})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        zoom_expr = f"max({zoom_max_pct}-(on/{total_frames})*{(zoom_max_pct - zoom_min_pct):.6f},{zoom_min_pct})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        zoom_expr = f"{zoom_max_pct}"
        x_expr = f"(on/{total_frames})*(iw-iw/zoom)"
        y_expr = "ih/2-(ih/zoom/2)"
    else: # pan_left
        zoom_expr = f"{zoom_max_pct}"
        x_expr = f"(1-(on/{total_frames}))*(iw-iw/zoom)"
        y_expr = "ih/2-(ih/zoom/2)"
        
    filter_graph = (
        f"scale=1920:1080:force_original_aspect_ratio=increase,"
        f"crop=1920:1080,"
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d={total_frames}:s=1920x1080:fps={fps},"
        f"format=yuv420p"
    )
    
    cmd = [
        "ffmpeg",
        "-y",
        "-loop", "1",
        "-i", str(image_path),
        "-vf", filter_graph,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "22",
        str(output_clip_path)
    ]
    
    output_clip_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(cmd)
    return output_clip_path
