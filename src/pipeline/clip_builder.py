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
        
    # Randomized Ken Burns motion: pure zoom, pure pan, and diagonal zoom+pan
    # combos, so consecutive scenes rarely feel like the same move repeated.
    motion_types = [
        "zoom_in", "zoom_out", "pan_right", "pan_left",
        "zoom_in_pan_right", "zoom_in_pan_left",
        "zoom_out_pan_right", "zoom_out_pan_left",
    ]
    motion = random.choice(motion_types)
    zoom_delta = zoom_max_pct - zoom_min_pct

    zoom_in_expr = f"min(pzoom+{zoom_delta/total_frames:.6f},{zoom_max_pct})"
    zoom_out_expr = f"max({zoom_max_pct}-(on/{total_frames})*{zoom_delta:.6f},{zoom_min_pct})"
    pan_right_expr = "(on/{0})*(iw-iw/zoom)".format(total_frames)
    pan_left_expr = "(1-(on/{0}))*(iw-iw/zoom)".format(total_frames)
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"

    if motion == "zoom_in":
        zoom_expr, x_expr, y_expr = zoom_in_expr, center_x, center_y
    elif motion == "zoom_out":
        zoom_expr, x_expr, y_expr = zoom_out_expr, center_x, center_y
    elif motion == "pan_right":
        zoom_expr, x_expr, y_expr = f"{zoom_max_pct}", pan_right_expr, center_y
    elif motion == "pan_left":
        zoom_expr, x_expr, y_expr = f"{zoom_max_pct}", pan_left_expr, center_y
    elif motion == "zoom_in_pan_right":
        zoom_expr, x_expr, y_expr = zoom_in_expr, pan_right_expr, center_y
    elif motion == "zoom_in_pan_left":
        zoom_expr, x_expr, y_expr = zoom_in_expr, pan_left_expr, center_y
    elif motion == "zoom_out_pan_right":
        zoom_expr, x_expr, y_expr = zoom_out_expr, pan_right_expr, center_y
    else: # zoom_out_pan_left
        zoom_expr, x_expr, y_expr = zoom_out_expr, pan_left_expr, center_y
        
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
