import os
import random
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg

# Gentle dissolves only — wipes/slides/circle-opens read as abrupt "cuts with
# a gimmick" rather than a smooth blend between scenes.
XFADE_TRANSITIONS = ["fade", "fadeblack", "dissolve"]
XFADE_DURATION = 1.2  # seconds of overlap between consecutive clips — soft, unhurried blend

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
    branding_config: Optional[Dict[str, Any]] = None,
    clip_durations: Optional[List[float]] = None,
    subtitle_style: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Joins motion clips (crossfading between them when durations are known so
    scene changes feel dynamic rather than hard-cut), applies color grading/
    grain, burns subtitles, places a square logo (top-left) and channel-name
    watermark text in the subtitle font (top-right), and multiplexes audio.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    effects = effects_config or {}
    branding = branding_config or {}
    sub_style = subtitle_style or {}

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
    has_subtitles_filter = check_ffmpeg_filter("subtitles")
    if has_subtitles_filter:
        ass_path_escaped = str(subtitle_ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        video_filters.append(f"subtitles=filename='{ass_path_escaped}'")
    else:
        logger.info("FFmpeg missing libass 'subtitles' filter; continuing video assembly without direct ASS filter burn.")

    vf_string = ",".join(video_filters) if video_filters else "null"

    # Square logo, top-left corner (if configured)
    logo_path_str = branding.get("logo_path")
    has_logo = bool(logo_path_str and Path(logo_path_str).exists())

    # Channel-name watermark, top-right corner, styled with the channel's own
    # subtitle font/color so it visually matches the rest of the video
    # instead of a generic default. Written to a textfile so ffmpeg's drawtext
    # doesn't choke on colons/quotes/apostrophes in the channel name.
    watermark_text = str(branding.get("channel_name_text") or "").strip()
    has_watermark = bool(watermark_text)
    watermark_txt_path = None
    if has_watermark:
        watermark_txt_path = temp_dir / "watermark.txt"
        watermark_txt_path.write_text(watermark_text, encoding="utf-8")
        watermark_font = sub_style.get("font", "Arial")
        # ffmpeg's color parser accepts "#RRGGBB" directly — the same web hex
        # value already used for the subtitle style, no conversion needed.
        watermark_color = sub_style.get("color") or "#FFFFFF"
        watermark_outline = sub_style.get("outline_color") or "#000000"
        wm_txt_escaped = str(watermark_txt_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        watermark_filter = (
            f"drawtext=font='{watermark_font}':textfile='{wm_txt_escaped}':fontsize=26:"
            f"fontcolor={watermark_color}:borderw=1.5:bordercolor={watermark_outline}:"
            f"x=w-tw-40:y=52"
        )

    # Crossfade chain needs each clip's real duration to compute cumulative
    # xfade offsets, and opens every clip as a simultaneous ffmpeg input to
    # build the filter graph — on a memory-constrained shared box that OOM-
    # kills the process for long videos with many scenes (confirmed in
    # production: a 40+ clip render was killed by the Linux OOM killer).
    # Cap it to short/medium videos; long ones fall back to a plain hard-cut
    # concat, which streams clips sequentially instead of holding them all
    # open in memory at once.
    MAX_CLIPS_FOR_XFADE = 15
    use_xfade = (
        clip_durations is not None
        and len(clip_durations) == len(clip_paths)
        and 2 <= len(clip_paths) <= MAX_CLIPS_FOR_XFADE
        and all(d > XFADE_DURATION * 2.2 for d in clip_durations)
    )

    concat_list_file = temp_dir / "clips_concat.txt"
    cmd = ["ffmpeg", "-y"]

    if use_xfade:
        for clip in clip_paths:
            cmd.extend(["-i", str(clip.resolve())])
        n = len(clip_paths)
        chain = []
        cumulative = clip_durations[0]
        prev_label = "0:v"
        for i in range(1, n):
            transition = random.choice(XFADE_TRANSITIONS)
            offset = cumulative - XFADE_DURATION
            out_label = f"vx{i}" if i < n - 1 else "v_joined"
            chain.append(
                f"[{prev_label}][{i}:v]xfade=transition={transition}:duration={XFADE_DURATION}:offset={offset:.3f}[{out_label}]"
            )
            cumulative += clip_durations[i] - XFADE_DURATION
            prev_label = out_label
        video_chain = ";".join(chain)
    else:
        with open(concat_list_file, "w", encoding="utf-8") as f:
            for clip in clip_paths:
                clean_path = str(clip.resolve()).replace("'", "'\\''")
                f.write(f"file '{clean_path}'\n")
        cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_list_file)])

    if has_logo:
        cmd.extend(["-i", str(Path(logo_path_str).resolve())])

    cmd.extend(["-i", str(audio_path.resolve())])
    audio_input_index = (len(clip_paths) if use_xfade else 1) + (1 if has_logo else 0)

    filter_parts = [video_chain] if use_xfade else []
    base_label = "v_joined" if use_xfade else "0:v"
    if vf_string != "null":
        filter_parts.append(f"[{base_label}]{vf_string}[v_base]")
        base_label = "v_base"

    if has_logo:
        logo_index = len(clip_paths) if use_xfade else 1
        # Force a clean square crop regardless of the source image's aspect ratio.
        filter_parts.append(f"[{logo_index}:v]scale=100:100:force_original_aspect_ratio=increase,crop=100:100[logo]")
        filter_parts.append(f"[{base_label}][logo]overlay=40:40[v_logo]")
        base_label = "v_logo"

    if has_watermark:
        filter_parts.append(f"[{base_label}]{watermark_filter}[v_wm]")
        base_label = "v_wm"

    if filter_parts:
        cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", f"[{base_label}]", "-map", f"{audio_input_index}:a"])
    else:
        cmd.extend(["-vf", "null", "-map", "0:v", "-map", "1:a"])

    # Encode to a temp file first and atomically rename into place only on success.
    # Prevents a killed/crashed process (e.g. server restart mid-render) from leaving
    # a truncated MP4 (missing moov atom) sitting at output_path with status "done".
    temp_output_path = output_path.with_name(f".{output_path.stem}.part.mp4")

    cmd.extend([
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(temp_output_path)
    ])

    logger.info("Assembling final MP4 video with FFmpeg...")
    try:
        run_ffmpeg(cmd)
        os.replace(temp_output_path, output_path)
    finally:
        if temp_output_path.exists():
            temp_output_path.unlink()

    if concat_list_file.exists():
        concat_list_file.unlink(missing_ok=True)

    logger.info(f"Final video successfully generated: {output_path}")
    return output_path
