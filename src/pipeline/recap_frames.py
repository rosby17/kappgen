"""Still-frame selection for the Recap pipeline (recap_editor.py).

Deliberately extracts single JPEG frames only — never a video clip of the
source. Two independent limits keep the result a curated set of stills
rather than a near-continuous flip-book that would reconstruct the source
video: a hard floor on the spacing between captures (MIN_SECONDS_BETWEEN_FRAMES)
and a ceiling on the total count (MAX_FRAMES_PER_RECAP).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

MIN_SECONDS_BETWEEN_FRAMES = 15.0
MAX_FRAMES_PER_RECAP = 40
# How close a scene-change timestamp must be to a target beat to be used
# instead of the raw proportional timestamp — keeps captures from landing
# mid-motion-blur on a cut that's actually seconds away.
SNAP_WINDOW_SECONDS = 3.0


def detect_scene_changes(source_path: Path, threshold: float = 0.35) -> List[float]:
    """Timestamps (seconds) of likely shot changes, via ffmpeg's scene-detection
    filter. Best-effort: an empty list here just means every target beat below
    falls back to its raw proportional timestamp instead of snapping to a cut."""
    cmd = [
        "ffmpeg", "-i", str(source_path),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception:
        return []
    timestamps: List[float] = []
    for line in proc.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            chunk = line.split("pts_time:", 1)[1].split(" ", 1)[0]
            timestamps.append(float(chunk))
        except (IndexError, ValueError):
            continue
    return timestamps


def choose_frame_timestamps(source_duration: float, narration_duration: float, seconds_per_beat: float = 6.0) -> List[float]:
    """One timestamp per narration "beat" (~seconds_per_beat of narration),
    mapped proportionally onto the source's own timeline, then snapped onto
    the nearest real scene change if one is close by. Capped by both count
    (MAX_FRAMES_PER_RECAP) and minimum spacing (MIN_SECONDS_BETWEEN_FRAMES) —
    whichever produces fewer frames wins, so a long source video never turns
    into a near-continuous sequence of stills."""
    if source_duration <= 0 or narration_duration <= 0:
        return []
    beat_count = max(1, round(narration_duration / seconds_per_beat))
    spacing_cap = max(1, int(source_duration // MIN_SECONDS_BETWEEN_FRAMES))
    frame_count = min(beat_count, spacing_cap, MAX_FRAMES_PER_RECAP)

    return [(i + 0.5) / frame_count * source_duration for i in range(frame_count)]


def snap_to_scene_changes(targets: List[float], scene_changes: List[float]) -> List[float]:
    if not scene_changes:
        return targets
    snapped = []
    for t in targets:
        nearest = min(scene_changes, key=lambda s: abs(s - t))
        snapped.append(nearest if abs(nearest - t) <= SNAP_WINDOW_SECONDS else t)
    return snapped


def extract_frames(source_path: Path, timestamps: List[float], output_dir: Path) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: List[Path] = []
    for i, t in enumerate(timestamps):
        out = output_dir / f"frame_{i:03d}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(source_path), "-frames:v", "1", "-q:v", "2", str(out)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            if out.exists() and out.stat().st_size > 0:
                frames.append(out)
        except Exception:
            continue
    return frames
