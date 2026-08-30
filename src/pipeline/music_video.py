"""Render pipeline for "Vidéo Musicale" channels (Channel.content_type ==
"music") — a fundamentally lighter pipeline than the narration one: no
script, no voiceover, no subtitles. The content IS the music itself.

High level:
  1. Generate the audio — one track looped to fill the target duration, or
     several tracks concatenated back-to-back ("compilation") for videos
     longer than a single AI-generated track.
  2. Generate 0-N illustrative background images (free Hugging Face path,
     same as the narration pipeline's per-scene images — never a paid call).
  3. Build a background video from those images (Ken Burns per image,
     cross-faded between them — reusing clip_builder/assembler's existing
     helpers) or a plain animated gradient if the creator chose 0 images.
  4. Overlay an audio-reactive waveform on top and mux with the final audio.

Billing is a single flat charge once the video is actually ready (see
MUSIC_VIDEO_CREDITS in src/utils/billing.py), not per intermediate step —
different from the narration pipeline's per-feature debits, matching what
the creator asked for when this product was scoped.
"""
import random
from pathlib import Path
from typing import List, Optional, Tuple

from src.pipeline.clip_builder import build_image_clip
from src.pipeline.music import generate_music_izivoice, _generate_synthetic_fallback_track
from src.pipeline.ai_text import generate_text
from src.utils.ffmpeg_runner import run_ffmpeg, get_audio_duration
from src.utils.logger import logger
from src.config import ASSETS_PATH

WIDTH, HEIGHT = 1920, 1080
XFADE_DURATION = 1.5  # seconds of overlap between consecutive background images
# Same asset + look as the narration pipeline's watermark (see WATERMARK_PATH
# in assembler.py) — kept as a separate constant since this module doesn't
# import assembler.py at all, not because the asset itself differs.
WATERMARK_PATH = ASSETS_PATH / "branding" / "watermark.png"


def pick_music_video_title(style_prompt: str, title_examples: Optional[str], recent_titles: List[str]) -> str:
    """Picks a title for a new music video — prefers the creator's own example
    titles (rotating through them, skipping ones already used) since those
    are exactly what the creator said they want, and only asks Claude to
    invent a new one in that style when the example list runs out."""
    examples = [line.strip() for line in (title_examples or "").splitlines() if line.strip()]
    unused = [t for t in examples if t not in recent_titles]
    if unused:
        return random.choice(unused)
    if examples:
        # Every example has been used at least once already — cycle back
        # rather than block production on Claude being available.
        return random.choice(examples)
    try:
        avoid = "\n".join(f"- {t}" for t in recent_titles[:20]) or "(none yet)"
        instruction = f"""Invent ONE short, appealing YouTube title for a music video with this style: {style_prompt}
Already-used titles on this channel (don't repeat): {avoid}
Respond with ONLY the title text, nothing else."""
        return generate_text(instruction, max_tokens=60, operation="music_video_title").strip().strip('"')
    except Exception as e:
        logger.warning(f"Music video title generation failed, using a generic fallback: {e}")
        return "Musique originale"


def _generate_audio_track(style_prompt: str, index: int, output_dir: Path) -> Path:
    output_path = output_dir / f"track_{index}.mp3"
    try:
        return generate_music_izivoice(style_prompt, 180.0, output_path)
    except Exception as e:
        logger.warning(f"AI music generation failed for track {index} ({e}); using a synthetic fallback tone.")
        return _generate_synthetic_fallback_track(180.0)


def build_audio(
    style_prompt: str,
    edit_mode: str,
    target_duration_seconds: float,
    output_dir: Path,
) -> Tuple[Path, int]:
    """Returns (final_audio_path, tracks_generated) — tracks_generated feeds
    the single end-of-render credit charge (see MUSIC_VIDEO_CREDITS)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tracks_generated = 0

    if edit_mode == "compilation":
        # Generate tracks until their combined length covers the target,
        # then concat + trim to the exact duration — a track's real length
        # is only known after generating it (Izivoice doesn't take a
        # duration param), so this can't be precomputed up front.
        segments: List[Path] = []
        total = 0.0
        while total < target_duration_seconds and len(segments) < 20:  # hard cap: never loop forever on a bad estimate
            track = _generate_audio_track(style_prompt, len(segments) + 1, output_dir)
            tracks_generated += 1
            segments.append(track)
            total += get_audio_duration(track)

        concat_list = output_dir / "concat_list.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")
        concatenated = output_dir / "compilation.mp3"
        run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(concatenated)])
        final_audio = output_dir / "final_audio.mp3"
        run_ffmpeg(["ffmpeg", "-y", "-i", str(concatenated), "-t", f"{target_duration_seconds:.3f}", "-c", "copy", str(final_audio)])
        return final_audio, tracks_generated

    # "loop" (default): one track, repeated to fill the target duration.
    track = _generate_audio_track(style_prompt, 1, output_dir)
    tracks_generated = 1
    final_audio = output_dir / "final_audio.mp3"
    run_ffmpeg([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(track),
        "-t", f"{target_duration_seconds:.3f}", "-c", "copy", str(final_audio),
    ])
    return final_audio, tracks_generated


def build_background_video(image_paths: List[Path], target_duration_seconds: float, output_path: Path) -> Path:
    """0 images: a slow-drifting flat gradient (no AI cost, still not a
    static frozen frame). 1+ images: a Ken Burns clip per image (reusing
    clip_builder's existing per-scene helper), cross-faded together to fill
    the target duration — same visual language the narration pipeline
    already uses for its own scene images."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not image_paths:
        run_ffmpeg([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"gradients=s={WIDTH}x{HEIGHT}:speed=0.005:x0=0:y0=0:x1={WIDTH}:y1={HEIGHT}:c0=0x0a1420:c1=0x0f2a3d",
            "-t", f"{target_duration_seconds:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(output_path),
        ])
        return output_path

    if len(image_paths) == 1:
        return build_image_clip(image_paths[0], output_path, target_duration_seconds)

    # Each image gets an equal slice, minus the overlap eaten by the xfade
    # transitions between consecutive clips.
    per_image = target_duration_seconds / len(image_paths) + XFADE_DURATION * (len(image_paths) - 1) / len(image_paths)
    tmp_dir = output_path.parent / "bg_clips"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    clips = [build_image_clip(p, tmp_dir / f"clip_{i}.mp4", per_image) for i, p in enumerate(image_paths)]

    inputs: List[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    filter_parts = []
    prev_label = "0:v"
    cumulative = per_image
    for i in range(1, len(clips)):
        out_label = f"v{i}" if i < len(clips) - 1 else "vout"
        offset = cumulative - XFADE_DURATION
        filter_parts.append(f"[{prev_label}][{i}:v]xfade=transition=fade:duration={XFADE_DURATION}:offset={offset:.3f}[{out_label}]")
        prev_label = out_label
        cumulative += per_image - XFADE_DURATION

    run_ffmpeg([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-t", f"{target_duration_seconds:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(output_path),
    ])
    return output_path


def compose_final_video(background_video: Path, final_audio: Path, output_path: Path, watermark_enabled: bool = True) -> Path:
    """Overlays an audio-reactive waveform strip over the background video
    and muxes in the final audio track — the one visual element that makes
    a plain looping background feel like a real "music video" instead of a
    static image with sound playing behind it. Also burns in the free-tier
    KappGen watermark unless the creator has ever paid for credits — same
    entitlement rule and look (scale=900:-1, 0.22 opacity, dead center) as
    the narration pipeline's own watermark (assembler.py); this pipeline
    never shared that code path, so without this a music video would have
    rendered watermark-free regardless of whether the creator ever paid."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    has_watermark = watermark_enabled and WATERMARK_PATH.exists()
    filter_complex = (
        f"[1:a]showwaves=s={WIDTH}x260:mode=cline:colors=0x00c2ff|0x38d0ff:scale=sqrt,format=yuva420p,"
        f"colorchannelmixer=aa=0.85[wave];"
        f"[0:v][wave]overlay=(W-w)/2:H-h-70:shortest=1[vbase]"
    )
    inputs = ["-i", str(background_video), "-i", str(final_audio)]
    if has_watermark:
        inputs += ["-i", str(WATERMARK_PATH)]
        filter_complex += (
            ";[2:v]scale=900:-1,format=rgba,colorchannelmixer=aa=0.22[wm];"
            "[vbase][wm]overlay=(W-w)/2:(H-h)/2[vout]"
        )
    else:
        filter_complex += ";[vbase]copy[vout]"
    run_ffmpeg([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ])
    return output_path


def render_music_video(
    style_prompt: str,
    edit_mode: str,
    image_count: int,
    target_duration_minutes: float,
    niche: str,
    output_dir: Path,
    progress_callback=None,
    watermark_enabled: bool = True,
) -> Tuple[Path, int]:
    """Main entry point, called from the worker (see queue_runner.py). Returns
    (output_mp4, tracks_generated) — tracks_generated feeds the single
    end-of-render credit charge."""
    def progress(stage: str, percent: int):
        if progress_callback:
            progress_callback(stage, percent)

    target_duration_seconds = max(30.0, target_duration_minutes * 60.0)
    audio_dir = output_dir / "source" / "audio"

    progress("Génération de la musique", 15)
    final_audio, tracks_generated = build_audio(style_prompt, edit_mode, target_duration_seconds, audio_dir)

    progress("Génération des images", 45)
    image_paths: List[Path] = []
    if image_count > 0:
        from src.pipeline.images import _generate_with_huggingface_flux
        import httpx
        images_dir = output_dir / "source" / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        prompt = f"{style_prompt}, in the visual context of {niche}" if niche else style_prompt
        with httpx.Client(limits=httpx.Limits(max_connections=4)) as client:
            for i in range(image_count):
                img_path = images_dir / f"bg_{i+1}.png"
                try:
                    _generate_with_huggingface_flux(prompt, img_path, client, size="1280x720", operation="music_video_background")
                    image_paths.append(img_path)
                except Exception as e:
                    logger.warning(f"Music video background image {i+1} failed (free tier only, no paid fallback): {e}")

    progress("Montage de la vidéo", 70)
    background_video = build_background_video(image_paths, target_duration_seconds, output_dir / "background.mp4")

    progress("Assemblage final", 90)
    output_mp4 = compose_final_video(background_video, final_audio, output_dir / "output.mp4", watermark_enabled=watermark_enabled)

    progress("Vidéo prête", 100)
    return output_mp4, tracks_generated
