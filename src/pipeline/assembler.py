import math
import os
import random
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg
from src.config import STORAGE_PATH, ASSETS_PATH

WATERMARK_PATH = ASSETS_PATH / "branding" / "watermark.png"

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
    grain, burns subtitles, places a square logo (top-right), and multiplexes
    audio.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    effects = effects_config or {}
    branding = branding_config or {}
    sub_style = subtitle_style or {}

    video_filters = []

    # Color grading + overlay effects are both gated behind effects_config.enabled —
    # a client can turn the whole "effects" layer off without losing their tuned
    # color grade / intensity settings underneath (they just don't apply for now).
    effects_enabled = effects.get("enabled", True)

    # Every grade below is a real, tested ffmpeg filter chain (eq/colorbalance/
    # colorchannelmixer/hue) — no placeholder options that don't actually change
    # the render.
    color_mode = effects.get("color_grade", "warm") if effects_enabled else "none"
    if color_mode == "warm":
        video_filters.append("eq=gamma=1.05:saturation=1.15")
    elif color_mode == "vintage":
        video_filters.append("colorbalance=rs=0.1:gs=-0.05:bs=-0.1,eq=saturation=0.85")
    elif color_mode == "dramatic":
        video_filters.append("eq=contrast=1.2:saturation=0.9")
    elif color_mode == "cool":
        video_filters.append("colorbalance=rs=-0.08:gs=0.02:bs=0.15,eq=saturation=1.05")
    elif color_mode == "noir":
        video_filters.append("hue=s=0,eq=contrast=1.25:brightness=-0.02")
    elif color_mode == "sepia":
        video_filters.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131:0")
    elif color_mode == "vibrant":
        video_filters.append("eq=saturation=1.5:contrast=1.1")
    elif color_mode == "faded":
        video_filters.append("eq=contrast=0.82:brightness=0.05:saturation=0.85")
    elif color_mode == "cinematic":
        # Teal shadows / orange highlights — the classic blockbuster grade.
        video_filters.append("colorbalance=rs=0.15:bs=-0.15:rh=-0.05:bh=0.1,eq=contrast=1.1:saturation=1.05")

    # Overlay effects — textures layered on top of the whole video, distinct from
    # the color grade above. A channel can combine any number of them at once.
    # "overlay_effects" is the current field (a list); falls back to the old
    # single-choice "overlay_effect" string, and further back to the original
    # boolean "grain" flag, for channels saved before either existed.
    # grain_intensity/vignette_intensity (0-100, default 50) scale how strong
    # each one is.
    overlay_effects = effects.get("overlay_effects")
    if overlay_effects is None:
        legacy = effects.get("overlay_effect")
        if legacy is None:
            legacy = "grain" if effects.get("grain", True) else "none"
        overlay_effects = {
            "none": [], "grain": ["grain"], "white_noise": ["white_noise"],
            "vignette": ["vignette"], "grain_vignette": ["grain", "vignette"],
        }.get(legacy, [])
    if not effects_enabled:
        overlay_effects = []

    grain_frac = max(0, min(100, effects.get("grain_intensity", 50))) / 100
    vignette_frac = max(0, min(100, effects.get("vignette_intensity", 50))) / 100

    # alls ranges chosen so 50% lands near the old fixed defaults (8 / 22)
    grain_alls = round(2 + grain_frac * 28)
    white_noise_alls = round(4 + grain_frac * 46)
    # ffmpeg's vignette "angle": smaller = stronger. PI/4 was the old fixed
    # default (~50%); sweep from a barely-there PI/2.2 up to a heavy PI/9.
    vignette_angle = (math.pi / 2.2) - (math.pi / 2.2 - math.pi / 9) * vignette_frac

    # grain and white_noise both drive ffmpeg's "noise" filter — applying both
    # would just double up the same texture, so grain wins if both are picked.
    if "grain" in overlay_effects:
        video_filters.append(f"noise=alls={grain_alls}:allf=t+u")
    elif "white_noise" in overlay_effects:
        video_filters.append(f"noise=alls={white_noise_alls}:allf=t+u")
    if "vignette" in overlay_effects:
        video_filters.append(f"vignette={vignette_angle:.4f}")

    # Extra textures — each a real, independent, linearly-chainable ffmpeg
    # filter (no fake/no-op options): fixed-strength since these are meant as
    # quick stylistic toggles rather than sliders like grain/vignette above.
    if "chromatic_aberration" in overlay_effects:
        # Shifts the red/blue channels apart horizontally — the classic lens/VHS fringe look.
        video_filters.append("rgbashift=rh=3:bh=-3")
    if "old_film" in overlay_effects:
        # Composite: heavy grain + tight vignette + desaturation, like scanned archival footage.
        video_filters.append("noise=alls=22:allf=t+u,vignette=0.35,eq=saturation=0.7:contrast=1.05")
    if "flicker" in overlay_effects:
        # Subtle time-varying brightness pulse — projector/old-film flicker.
        video_filters.append("eq=eval=frame:brightness='0.035*sin(2*PI*t*3)'")
    if "soft_focus" in overlay_effects:
        # Gentle blur only (no blend) — a cheap, real "dreamy" filmic softness.
        video_filters.append("gblur=sigma=1.4")
    if "sharpen" in overlay_effects:
        # Crisper "HD clarity" look — the opposite end of the texture spectrum from grain.
        video_filters.append("unsharp=5:5:0.8:5:5:0.0")

    # Check if FFmpeg build has libass 'subtitles' filter, and whether the client
    # wants subtitles burned in at all (subtitle_style.enabled, default True).
    subtitles_enabled = sub_style.get("enabled", True)
    has_subtitles_filter = subtitles_enabled and check_ffmpeg_filter("subtitles")
    if has_subtitles_filter:
        ass_path_escaped = str(subtitle_ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        video_filters.append(f"subtitles=filename='{ass_path_escaped}'")
    elif subtitles_enabled:
        logger.info("FFmpeg missing libass 'subtitles' filter; continuing video assembly without direct ASS filter burn.")

    vf_string = ",".join(video_filters) if video_filters else "null"

    # Square logo, top-right corner (if configured and not disabled). logo_path
    # is stored storage-relative ("channels/<id>/logo.png") — it must be resolved
    # against STORAGE_PATH, not treated as relative to the process's cwd (which
    # silently made has_logo False for every channel, however the logo was set).
    logo_path_str = branding.get("logo_path")
    logo_full_path = (STORAGE_PATH / logo_path_str) if logo_path_str else None
    has_logo = bool(branding.get("logo_enabled", True) and logo_full_path and logo_full_path.exists())

    # Free-tier NicheCut watermark. The official horizontal logo is deliberately
    # large and centered: a corner mark can be removed with a trivial crop or
    # covered by another logo. Paid plans disable it through watermark_enabled.
    has_watermark = bool(effects.get("watermark_enabled", True) and WATERMARK_PATH.exists())

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
    # Without this, the concat demuxer can leave PTS discontinuities between
    # clips that make ffmpeg write a wrong (too-short) duration into the
    # output's moov atom — browsers then report the wrong video.duration,
    # which breaks seeking/skip controls near the end of the real content.
    cmd = ["ffmpeg", "-y", "-fflags", "+genpts"]

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
        cmd.extend(["-i", str(logo_full_path.resolve())])
    if has_watermark:
        cmd.extend(["-i", str(WATERMARK_PATH.resolve())])

    cmd.extend(["-i", str(audio_path.resolve())])
    audio_input_index = (len(clip_paths) if use_xfade else 1) + (1 if has_logo else 0) + (1 if has_watermark else 0)

    filter_parts = [video_chain] if use_xfade else []
    base_label = "v_joined" if use_xfade else "0:v"
    if vf_string != "null":
        filter_parts.append(f"[{base_label}]{vf_string}[v_base]")
        base_label = "v_base"

    if has_logo:
        logo_index = len(clip_paths) if use_xfade else 1
        # Force a clean square crop regardless of the source image's aspect ratio.
        filter_parts.append(f"[{logo_index}:v]scale=100:100:force_original_aspect_ratio=increase,crop=100:100[logo]")
        filter_parts.append(f"[{base_label}][logo]overlay=W-w-40:40[v_logo]")
        base_label = "v_logo"

    if has_watermark:
        watermark_index = (len(clip_paths) if use_xfade else 1) + (1 if has_logo else 0)
        # Roughly 47% of a 1920px frame. Opacity raised from 0.14 to 0.22 so it
        # actually reads as sitting on top of the subtitles instead of getting
        # visually lost behind their bold, high-contrast text.
        filter_parts.append(f"[{watermark_index}:v]scale=900:-1,format=rgba,colorchannelmixer=aa=0.22[wm]")
        filter_parts.append(f"[{base_label}][wm]overlay=(W-w)/2:(H-h)/2[v_wm]")
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
        # Capped average bitrate instead of plain CRF: CRF's output size is
        # content-dependent and unpredictable — the grain/noise overlay filter
        # in particular adds high-frequency detail that's expensive to encode,
        # which is how a 30min render ended up at 11.6GB under CRF alone.
        # 4200k video + 128k audio ≈ 4.33 Mbps → a 30min video lands around
        # ~975MB, comfortably under a 1GB budget regardless of content.
        "-b:v", "4200k",
        "-maxrate", "4600k",
        "-bufsize", "9200k",
        "-c:a", "aac",
        "-b:a", "128k",
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
