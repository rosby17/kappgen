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
from src.config import ASSETS_PATH, STORAGE_PATH

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


def _generate_audio_track(style_prompt: str, index: int, output_dir: Path, user_id: str | None = None, video_id: str | None = None) -> Path:
    output_path = output_dir / f"track_{index}.mp3"
    if user_id:
        from src.utils.billing import debit_izivoice_usage_by_user_id, IZIVOICE_MUSIC_CREDITS
        if not debit_izivoice_usage_by_user_id(user_id, IZIVOICE_MUSIC_CREDITS, "music_video_generation", video_id=video_id):
            raise RuntimeError("Solde de crédits KappGen insuffisant pour générer la musique.")
    # One retry before giving up: Izivoice's /music endpoint has been seen
    # returning a transient 500 that succeeds on an immediate second try —
    # worth it before falling all the way back to the synthetic drone,
    # which a creator correctly does not experience as "the music they
    # asked for" (production report: a whole 10-minute music video that
    # was just this looped tone, from a single failed attempt).
    last_exc = None
    for attempt in range(2):
        try:
            return generate_music_izivoice(style_prompt, 180.0, output_path)
        except Exception as e:
            last_exc = e
            logger.warning(f"AI music generation failed for track {index}, attempt {attempt + 1}/2 ({e}).")
    logger.warning(f"AI music generation failed for track {index} after retry ({last_exc}); using a synthetic fallback tone.")
    if user_id:
        # The creator was already charged as if AI generation succeeded —
        # it didn't, so the free fallback tone must not be paid for.
        from src.utils.billing import refund_izivoice_usage_by_user_id, IZIVOICE_MUSIC_CREDITS
        refund_izivoice_usage_by_user_id(user_id, IZIVOICE_MUSIC_CREDITS, "music_video_generation", video_id=video_id)
    return _generate_synthetic_fallback_track(180.0)


def build_audio(
    style_prompt: str,
    edit_mode: str,
    target_duration_seconds: float,
    output_dir: Path,
    user_id: str | None = None,
    video_id: str | None = None,
    music_source_mode: str = "ai_generate",
    own_tracks: Optional[List[str]] = None,
) -> Tuple[Path, int]:
    """Returns (final_audio_path, tracks_generated) — tracks_generated feeds
    the single end-of-render credit charge (see MUSIC_VIDEO_CREDITS)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tracks_generated = 0

    # "Importer mes propres pistes": the creator's own uploaded files
    # (channel.music_preference.tracks, storage-relative — same field a
    # faceless channel's background music reuses) stand in for every AI
    # generation call below. No credits debited, no Izivoice wait at all —
    # this is the whole point of the option.
    if music_source_mode == "library":
        candidates = [STORAGE_PATH / t for t in (own_tracks or []) if (STORAGE_PATH / t).exists()]
        if candidates:
            if edit_mode == "compilation" and len(candidates) > 1:
                segments: List[Path] = []
                total = 0.0
                shuffled = candidates[:]
                random.shuffle(shuffled)
                i = 0
                while total < target_duration_seconds and len(segments) < 40:
                    track = shuffled[i % len(shuffled)]
                    segments.append(track)
                    total += get_audio_duration(track)
                    i += 1
                concat_list = output_dir / "concat_list.txt"
                concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")
                concatenated = output_dir / "compilation.mp3"
                run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c:a", "libmp3lame", str(concatenated)])
                final_audio = output_dir / "final_audio.mp3"
                run_ffmpeg(["ffmpeg", "-y", "-i", str(concatenated), "-t", f"{target_duration_seconds:.3f}", "-c", "copy", str(final_audio)])
                return final_audio, 0
            track = random.choice(candidates)
            final_audio = output_dir / "final_audio.mp3"
            run_ffmpeg([
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(track),
                "-t", f"{target_duration_seconds:.3f}", "-c:a", "libmp3lame", str(final_audio),
            ])
            return final_audio, 0
        logger.warning("Music source mode is 'library' but no tracks are uploaded; falling back to AI generation.")

    if edit_mode == "compilation":
        # Generate tracks until their combined length covers the target,
        # then concat + trim to the exact duration — a track's real length
        # is only known after generating it (Izivoice doesn't take a
        # duration param), so this can't be precomputed up front.
        segments: List[Path] = []
        total = 0.0
        while total < target_duration_seconds and len(segments) < 20:  # hard cap: never loop forever on a bad estimate
            track = _generate_audio_track(style_prompt, len(segments) + 1, output_dir, user_id=user_id, video_id=video_id)
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
    track = _generate_audio_track(style_prompt, 1, output_dir, user_id=user_id, video_id=video_id)
    tracks_generated = 1
    final_audio = output_dir / "final_audio.mp3"
    run_ffmpeg([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(track),
        "-t", f"{target_duration_seconds:.3f}", "-c", "copy", str(final_audio),
    ])
    return final_audio, tracks_generated


def build_background_video(
    image_paths: List[Path],
    target_duration_seconds: float,
    output_path: Path,
    zoom_min_pct: float = 1.0,
    zoom_max_pct: float = 1.12,
) -> Path:
    """0 images: a slow-drifting flat gradient (no AI cost, still not a
    static frozen frame). 1+ images: a Ken Burns clip per image (reusing
    clip_builder's existing per-scene helper, same zoom_min_pct/zoom_max_pct
    the narration pipeline's Effets step controls), cross-faded together to
    fill the target duration — same visual language the narration pipeline
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
        return build_image_clip(image_paths[0], output_path, target_duration_seconds, zoom_min_pct=zoom_min_pct, zoom_max_pct=zoom_max_pct)

    # Each image gets an equal slice, minus the overlap eaten by the xfade
    # transitions between consecutive clips.
    per_image = target_duration_seconds / len(image_paths) + XFADE_DURATION * (len(image_paths) - 1) / len(image_paths)
    tmp_dir = output_path.parent / "bg_clips"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    clips = [build_image_clip(p, tmp_dir / f"clip_{i}.mp4", per_image, zoom_min_pct=zoom_min_pct, zoom_max_pct=zoom_max_pct) for i, p in enumerate(image_paths)]

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


def _escape_drawtext(text: str) -> str:
    """Minimal escaping for ffmpeg's drawtext filter argument — backslash and
    single-quote would otherwise break out of the filtergraph's own quoting,
    colon would be read as the next key=value separator. Real newlines
    become the literal two-character `\\n` drawtext itself renders as a
    line break."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return escaped.replace("\n", "\\n")


def _effects_video_filter(effects_config: Optional[dict]) -> Optional[str]:
    """Same two effects the narration pipeline's Effets step exposes (grain,
    vignette) — a deliberately small subset of assembler.py's full effect
    system, since that one is written inline against variables the narration
    render loop computes for itself and isn't a standalone importable
    helper. Returns None when neither is enabled (no-op filter)."""
    if not effects_config:
        return None
    enabled = effects_config.get("overlay_effects") or []
    parts = []
    if "grain" in enabled:
        intensity = int(effects_config.get("grain_intensity") or 50)
        parts.append(f"noise=alls={max(1, round(intensity * 0.3)):d}:allf=t+u")
    if "vignette" in enabled:
        intensity = int(effects_config.get("vignette_intensity") or 50)
        parts.append(f"vignette=PI/{max(3, round(8 - intensity / 20)):d}")
    return ",".join(parts) if parts else None


def _subtitle_drawtext_filter(subtitle_style: Optional[dict], subtitle_text: str) -> Optional[str]:
    """Burns a static title/lyrics text across the whole video — no per-word
    timing (there's no narration transcript to time it against, unlike the
    faceless pipeline's karaoke-style ASS subtitles), just the styled text
    shown throughout. subtitle_style uses the same field names as the
    faceless wizard's subtitle_style, trimmed to what a static display
    needs (see MusicChannelWizard's own comment on the same shape)."""
    text = (subtitle_text or "").strip()
    if not subtitle_style or not subtitle_style.get("enabled") or not text:
        return None
    size = int(subtitle_style.get("size") or 44)
    color = subtitle_style.get("base_color") or "#FFFFFF"
    outline_color = subtitle_style.get("outline_color") or "#000000"
    outline_width = int(subtitle_style.get("outline_width") or 0)
    position = subtitle_style.get("position") or "bottom"
    box_color = subtitle_style.get("box_color") or "transparent"
    font = subtitle_style.get("font") or "Inter"
    y_expr = {"top": "80", "center": "(h-text_h)/2", "middle": "(h-text_h)/2"}.get(position, "h-text_h-80")
    parts = [
        f"drawtext=font='{font}'",
        f"text='{_escape_drawtext(text)}'",
        f"fontsize={size}",
        f"fontcolor={color}",
        "x=(w-text_w)/2",
        f"y={y_expr}",
        "line_spacing=6",
    ]
    if outline_width > 0:
        parts.append(f"bordercolor={outline_color}")
        parts.append(f"borderw={outline_width}")
    if box_color and box_color != "transparent":
        parts.append(f"box=1:boxcolor={box_color}@0.6:boxborderw=14")
    return ":".join(parts)


def compose_final_video(
    background_video: Path,
    final_audio: Path,
    output_path: Path,
    watermark_enabled: bool = True,
    effects_config: Optional[dict] = None,
    subtitle_style: Optional[dict] = None,
    subtitle_text: str = "",
) -> Path:
    """Overlays an audio-reactive waveform strip over the background video
    and muxes in the final audio track — the one visual element that makes
    a plain looping background feel like a real "music video" instead of a
    static image with sound playing behind it. Also burns in the free-tier
    KappGen watermark unless the creator has ever paid for credits — same
    entitlement rule and look (scale=900:-1, 0.22 opacity, dead center) as
    the narration pipeline's own watermark (assembler.py); this pipeline
    never shared that code path, so without this a music video would have
    rendered watermark-free regardless of whether the creator ever paid.
    Optionally also applies the Effets step's grain/vignette and burns in a
    static title/lyrics text from the Sous-titres step."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    has_watermark = watermark_enabled and WATERMARK_PATH.exists()
    effects_filter = _effects_video_filter(effects_config)
    base_label = "0:v" if not effects_filter else "vfx"
    filter_complex = f"[0:v]{effects_filter}[vfx];" if effects_filter else ""
    # A full-width, saturated cyan waveform read as an ugly bar slapped across
    # the whole frame — a slim, soft, centered strip (roughly half the frame
    # width) reads as an actual design element instead of a debug overlay.
    waveform_width = WIDTH // 2
    filter_complex += (
        f"[1:a]showwaves=s={waveform_width}x160:mode=cline:colors=0xffffff:scale=sqrt,format=yuva420p,"
        f"colorchannelmixer=aa=0.55[wave];"
        f"[{base_label}][wave]overlay=(W-w)/2:H-h-90:shortest=1[vbase]"
    )
    inputs = ["-i", str(background_video), "-i", str(final_audio)]
    next_label = "vbase"
    if has_watermark:
        inputs += ["-i", str(WATERMARK_PATH)]
        filter_complex += (
            f";[2:v]scale=900:-1,format=rgba,colorchannelmixer=aa=0.22[wm];"
            f"[{next_label}][wm]overlay=(W-w)/2:(H-h)/2[vwm]"
        )
        next_label = "vwm"
    subtitle_filter = _subtitle_drawtext_filter(subtitle_style, subtitle_text)
    if subtitle_filter:
        filter_complex += f";[{next_label}]{subtitle_filter}[vout]"
    else:
        filter_complex += f";[{next_label}]copy[vout]"
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
    user_id: str | None = None,
    video_id: str | None = None,
    music_source_mode: str = "ai_generate",
    own_tracks: Optional[List[str]] = None,
    image_style: Optional[dict] = None,
    effects_config: Optional[dict] = None,
    subtitle_style: Optional[dict] = None,
    subtitle_text: str = "",
    channel_id: str | None = None,
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
    final_audio, tracks_generated = build_audio(
        style_prompt, edit_mode, target_duration_seconds, audio_dir,
        user_id=user_id, video_id=video_id,
        music_source_mode=music_source_mode, own_tracks=own_tracks,
    )

    progress("Génération des images", 45)
    image_paths: List[Path] = []
    if image_count > 0:
        from src.pipeline.images import fetch_or_generate_images
        images_dir = output_dir / "source" / "images"
        prompt = f"{style_prompt}, in the visual context of {niche}" if niche else style_prompt
        # A music channel saved before Visuels/Sources existed (or one that
        # never touched that step) has no image_style at all — default to
        # exactly the old always-AI-generate behavior instead of
        # resolve_enabled_image_sources' own general-purpose default
        # ("library", meant for narration channels, which is empty here and
        # would silently produce zero images for every such channel).
        effective_style = image_style if (image_style and image_style.get("sources")) else {"sources": ["ai_generated"], "style_prompt": ""}
        try:
            image_paths = fetch_or_generate_images(
                [prompt] * image_count, images_dir, effective_style,
                unique_generation_count=image_count,
                user_id=user_id, niche=niche, channel_id=channel_id,
            )
        except Exception as e:
            logger.warning(f"Music video background image sourcing failed: {e}")

    progress("Montage de la vidéo", 70)
    zoom_min_pct = float((effects_config or {}).get("zoom_min_pct") or 1.0)
    zoom_max_pct = float((effects_config or {}).get("zoom_max_pct") or 1.12)
    background_video = build_background_video(image_paths, target_duration_seconds, output_dir / "background.mp4", zoom_min_pct=zoom_min_pct, zoom_max_pct=zoom_max_pct)

    progress("Assemblage final", 90)
    output_mp4 = compose_final_video(
        background_video, final_audio, output_dir / "output.mp4",
        watermark_enabled=watermark_enabled,
        effects_config=effects_config, subtitle_style=subtitle_style, subtitle_text=subtitle_text,
    )

    progress("Vidéo prête", 100)
    return output_mp4, tracks_generated
