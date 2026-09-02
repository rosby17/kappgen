from pathlib import Path
from array import array
import math
import subprocess
from typing import List, Dict, Any, Optional
from src.utils.ffmpeg_runner import run_ffmpeg, logger


def analyze_scene_audio_energy(audio_path: Path, segments: List[Dict[str, float]], sample_rate: int = 2000) -> List[float]:
    """Return a robust 0..1 energy score for each scene from the narration.

    Decoding once to low-rate mono PCM is cheap even for long videos and is
    much more faithful than guessing intensity from word count. Scores are
    normalized within the video, so a softly narrated meditation still gets
    useful relative dynamics without being edited like an action trailer.
    """
    if not segments:
        return []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"],
            capture_output=True,
            check=True,
        )
        samples = array("h")
        samples.frombytes(proc.stdout)
        raw = []
        for seg in segments:
            start = max(0, int(seg["start"] * sample_rate))
            end = min(len(samples), max(start + 1, int(seg["end"] * sample_rate)))
            chunk = samples[start:end]
            if not chunk:
                raw.append(0.0)
                continue
            # RMS expresses sustained intensity; peak adds a small accent for
            # emphatic words without letting a single click dominate the cut.
            rms = math.sqrt(sum(float(v) * v for v in chunk) / len(chunk))
            peak = max(abs(v) for v in chunk)
            raw.append(rms * 0.85 + peak * 0.15)
        ordered = sorted(raw)
        low = ordered[max(0, round((len(ordered) - 1) * 0.1))]
        high = ordered[max(0, round((len(ordered) - 1) * 0.9))]
        span = max(high - low, 1.0)
        return [max(0.0, min(1.0, (value - low) / span)) for value in raw]
    except Exception as exc:
        logger.warning(f"Audio energy analysis failed; using neutral cinematic pacing: {exc}")
        return [0.5] * len(segments)

def build_image_clip(
    image_path: Path,
    output_clip_path: Path,
    duration: float,
    zoom_min_pct: float = 1.0,
    zoom_max_pct: float = 1.12,
    fps: int = 30,
    energy: float = 0.5,
    scene_index: int = 0,
    editing_profile: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Creates a 1920x1080 video clip from a static image applying a Ken Burns (zoompan) motion effect.
    Alternates between zoom-in, zoom-out, and panning.
    """
    total_frames = int(duration * fps)
    if total_frames < 1:
        total_frames = 1
        
    # Movement follows the narration: restrained on calm passages and fuller
    # on energetic ones. Direction follows a stable cinematic sequence rather
    # than random choices that can accidentally repeat or fight the soundtrack.
    default_motion_types = [
        "zoom_in", "zoom_out", "pan_right", "pan_left",
        "zoom_in_pan_right", "zoom_in_pan_left",
        "zoom_out_pan_right", "zoom_out_pan_left",
    ]
    energy = max(0.0, min(1.0, energy))
    profile = editing_profile or {}
    motion_types = profile.get("motions") or default_motion_types
    motion = motion_types[scene_index % len(motion_types)]
    configured_delta = max(0.0, zoom_max_pct - zoom_min_pct)
    motion_scale = max(0.25, min(1.0, float(profile.get("motion_scale", 1.0))))
    zoom_delta = configured_delta * motion_scale * (0.35 + 0.65 * energy)
    dynamic_zoom_max = zoom_min_pct + zoom_delta

    zoom_in_expr = f"min(pzoom+{zoom_delta/total_frames:.6f},{dynamic_zoom_max})"
    zoom_out_expr = f"max({dynamic_zoom_max}-(on/{total_frames})*{zoom_delta:.6f},{zoom_min_pct})"
    pan_right_expr = "(on/{0})*(iw-iw/zoom)".format(total_frames)
    pan_left_expr = "(1-(on/{0}))*(iw-iw/zoom)".format(total_frames)
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"

    if motion == "zoom_in":
        zoom_expr, x_expr, y_expr = zoom_in_expr, center_x, center_y
    elif motion == "zoom_out":
        zoom_expr, x_expr, y_expr = zoom_out_expr, center_x, center_y
    elif motion == "pan_right":
        zoom_expr, x_expr, y_expr = f"{dynamic_zoom_max}", pan_right_expr, center_y
    elif motion == "pan_left":
        zoom_expr, x_expr, y_expr = f"{dynamic_zoom_max}", pan_left_expr, center_y
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


def build_video_clip(
    video_path: Path,
    output_clip_path: Path,
    duration: float,
    fps: int = 30,
) -> Path:
    """Prepare a creator-provided B-roll clip for one narration scene.

    The source is looped when shorter than the scene and center-cropped to the
    same 1920x1080 canvas as image scenes. Audio from the B-roll is discarded;
    the narration/mix remains the single authoritative audio track.
    """
    filter_graph = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps={}:format=yuv420p".format(fps)
    cmd = [
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video_path),
        "-t", f"{duration:.3f}", "-vf", filter_graph,
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        str(output_clip_path),
    ]
    output_clip_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(cmd)
    return output_clip_path
