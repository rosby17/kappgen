import json
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from src.utils.logger import logger
from src.pipeline.voiceover import generate_voiceover, generate_transcript_for_audio
from src.pipeline.pacing import calculate_pacing_segments
from src.pipeline.images import fetch_or_generate_images
from src.pipeline.clip_builder import build_image_clip
from src.pipeline.subtitles import generate_ass_subtitles, overlay_subtitles_on_image
from src.pipeline.music import get_background_music_track
from src.pipeline.audio_mixer import mix_audio_tracks
from src.pipeline.assembler import assemble_final_video, check_ffmpeg_filter

def run_video_pipeline(
    channel_config: Dict[str, Any],
    script_text: str,
    output_dir: Path,
    pre_recorded_audio_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> Path:
    """
    Orchestrates the entire video generation pipeline for a given script/audio and channel configuration.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    images_dir = source_dir / "images"
    clips_dir = source_dir / "clips"

    def progress(stage: str, percent: int):
        if progress_callback:
            progress_callback(stage, percent)
    
    # 1. Save Config Snapshot & Script
    (source_dir / "script.txt").write_text(script_text or "", encoding="utf-8")
    (source_dir / "config_snapshot.json").write_text(json.dumps(channel_config, indent=2), encoding="utf-8")
    
    # 2. Voiceover & Audio Alignment Setup
    raw_vo_path = source_dir / "voiceover.mp3"
    
    if pre_recorded_audio_path and pre_recorded_audio_path.exists():
        progress("Préparation et transcription de l’audio", 8)
        logger.info(f"Step 1/7: Using pre-recorded audio file: {pre_recorded_audio_path}")
        # Copy pre-recorded audio or convert to MP3
        from src.utils.ffmpeg_runner import run_ffmpeg
        cmd = ["ffmpeg", "-y", "-i", str(pre_recorded_audio_path.resolve()), "-c:a", "libmp3lame", "-b:a", "192k", str(raw_vo_path)]
        run_ffmpeg(cmd)

        # Real spoken content is unknown for uploaded audio (script_text is only the
        # filename-derived title), so transcribe via Izivoice speech-to-text to get
        # accurate subtitle text and word-level timing.
        transcript_info = generate_transcript_for_audio(raw_vo_path, fallback_text=script_text or "Audio préenregistré")
    else:
        progress("Génération de la voix et transcription", 8)
        logger.info("Step 1/7: Generating voiceover audio via TTS...")
        _, transcript_info = generate_voiceover(script_text or "Vidéo sans titre", raw_vo_path)

    (source_dir / "transcript.json").write_text(json.dumps(transcript_info, indent=2), encoding="utf-8")
    
    total_duration = transcript_info.get("duration", 10.0)
    
    # 3. Calculate Pacing Segments
    logger.info("Step 2/7: Calculating dynamic image pacing...")
    progress("Découpage du script en scènes", 25)
    segments = calculate_pacing_segments(total_duration)
    
    # 4. Fetch or Generate Images
    logger.info("Step 3/7: Preparing image pool...")
    progress("Préparation des visuels", 35)
    # Each segment's prompt is the actual narration spoken during its time window
    # (previously a content-blind placeholder like "Scene for text section 3" —
    # the AI generator had no idea what the video was even about).
    all_words = transcript_info.get("words") or []
    prompts = []
    for seg in segments:
        seg_words = [w["word"] for w in all_words if w.get("start", 0) < seg["end"] and w.get("end", 0) > seg["start"]]
        seg_text = " ".join(seg_words).strip()
        prompts.append(seg_text if seg_text else (script_text or "")[:200])
    image_paths = fetch_or_generate_images(prompts, images_dir, channel_config.get("image_style"))
    
    # 5. Generate Subtitles ASS file
    logger.info("Step 4/7: Formatting ASS subtitles...")
    progress("Création des sous-titres", 55)
    subtitle_ass_path = source_dir / "subtitles.ass"
    generate_ass_subtitles(
        transcript_info=transcript_info,
        style_config=channel_config.get("subtitle_style", {}),
        output_ass_path=subtitle_ass_path
    )
    
    # Check if FFmpeg has libass 'subtitles' filter; if not, overlay text directly onto images
    has_libass = check_ffmpeg_filter("subtitles")
    subtitled_image_paths = []
    
    if not has_libass:
        logger.info("Applying direct subtitle burn onto image frames...")
        words = transcript_info.get("words", [])
        chunk_size = 6
        for i, img_p in enumerate(image_paths):
            sub_img = images_dir / f"subtitled_{i+1:03d}.png"
            if words:
                start_idx = (i * chunk_size) % len(words)
                sub_text = " ".join([w["word"] for w in words[start_idx:start_idx + chunk_size]])
            else:
                sub_text = script_text[:40]
            overlay_subtitles_on_image(img_p, sub_img, sub_text, channel_config.get("subtitle_style", {}))
            subtitled_image_paths.append(sub_img)
    else:
        subtitled_image_paths = image_paths

    # 6. Build Dynamic Motion Video Clips
    logger.info("Step 5/7: Rendering motion video clips (Ken Burns effect)...")
    progress("Animation des scènes", 65)
    clip_paths = []
    zoom_min = channel_config.get("effects_config", {}).get("zoom_min_pct", 1.0)
    zoom_max = channel_config.get("effects_config", {}).get("zoom_max_pct", 1.12)

    for i, (seg, img_path) in enumerate(zip(segments, subtitled_image_paths)):
        clip_file = clips_dir / f"clip_{i+1:03d}.mp4"
        build_image_clip(
            image_path=img_path,
            output_clip_path=clip_file,
            duration=seg["duration"],
            zoom_min_pct=zoom_min,
            zoom_max_pct=zoom_max
        )
        clip_paths.append(clip_file)

    # Manifest of each scene's source image + built clip, keyed by index — lets
    # the post-render editor ("replace this image") locate and rebuild a single
    # scene without needing to re-run TTS/pacing/image-fetch for the whole video.
    scenes_manifest = [
        {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "duration": seg["duration"],
            "image_path": str(image_paths[i]),
            "clip_path": str(clip_paths[i]),
        }
        for i, seg in enumerate(segments)
    ]
    (source_dir / "scenes.json").write_text(json.dumps(scenes_manifest, indent=2), encoding="utf-8")
        
    # 7. Mix Voiceover and Background Music
    logger.info("Step 6/7: Mixing audio tracks...")
    progress("Mixage de la voix et de la musique", 82)
    music_pref = channel_config.get("music_preference", {})
    mixed_audio_path = source_dir / "mixed_audio.mp3"
    
    if music_pref.get("enabled", True):
        music_track = get_background_music_track(
            music_pref=music_pref,
            duration=total_duration,
            channel_id=channel_config.get("id"),
            niche=channel_config.get("niche"),
            script_text=script_text
        )
        mix_audio_tracks(
            voiceover_path=raw_vo_path,
            music_path=music_track,
            output_audio_path=mixed_audio_path,
            music_volume=music_pref.get("volume", 0.15)
        )
    else:
        mixed_audio_path = raw_vo_path

    # 8. Assemble Final Video Output
    logger.info("Step 7/7: Assembling final 1080p MP4...")
    progress("Assemblage du MP4 final", 90)
    final_output_path = output_dir / "output.mp4"
    assemble_final_video(
        clip_paths=clip_paths,
        audio_path=mixed_audio_path,
        subtitle_ass_path=subtitle_ass_path,
        output_path=final_output_path,
        effects_config=channel_config.get("effects_config"),
        branding_config=channel_config.get("branding"),
        clip_durations=[seg["duration"] for seg in segments],
        subtitle_style=channel_config.get("subtitle_style"),
    )
    
    logger.info(f"Pipeline successfully rendered video to {final_output_path}")

    # images_dir/clips_dir are kept (not deleted here) so the post-render editor
    # can swap a bad scene image and reassemble without redoing TTS/image-gen —
    # they're purged later by the retention job or when the user closes the editor.
    # assembler's own scratch dir (concat list file) is short-lived and safe to drop now.
    try:
        import shutil
        temp_dir = output_dir / "temp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as cleanup_err:
        logger.warning(f"Non-fatal: failed to clean up scratch render files for {output_dir}: {cleanup_err}")

    return final_output_path


def reassemble_video_output(
    channel_config: Dict[str, Any],
    output_dir: Path,
) -> Path:
    """
    Rebuilds output.mp4 from an already-rendered video's kept scene clips,
    subtitles, and mixed audio — used by the post-render editor after a scene
    image (and its clip) has been swapped, so a fix doesn't require redoing
    TTS, pacing, or image generation for the whole video.
    """
    source_dir = output_dir / "source"
    scenes_path = source_dir / "scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"No scenes.json found for {output_dir} — this video predates edit support or was already purged.")

    scenes_manifest = json.loads(scenes_path.read_text(encoding="utf-8"))
    clip_paths = [Path(s["clip_path"]) for s in scenes_manifest]
    clip_durations = [s["duration"] for s in scenes_manifest]

    subtitle_ass_path = source_dir / "subtitles.ass"
    mixed_audio_path = source_dir / "mixed_audio.mp3"
    if not mixed_audio_path.exists():
        mixed_audio_path = source_dir / "voiceover.mp3"

    final_output_path = output_dir / "output.mp4"
    assemble_final_video(
        clip_paths=clip_paths,
        audio_path=mixed_audio_path,
        subtitle_ass_path=subtitle_ass_path,
        output_path=final_output_path,
        effects_config=channel_config.get("effects_config"),
        branding_config=channel_config.get("branding"),
        clip_durations=clip_durations,
        subtitle_style=channel_config.get("subtitle_style"),
    )
    logger.info(f"Reassembled video output to {final_output_path}")
    return final_output_path
