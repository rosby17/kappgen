import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from src.utils.logger import logger
from src.pipeline.voiceover import generate_voiceover, generate_transcript_for_audio, synthetic_word_timings
from src.utils.ffmpeg_runner import get_audio_duration
from src.pipeline.pacing import calculate_pacing_segments
from src.pipeline.images import fetch_or_generate_images
from src.pipeline.scene_director import build_scene_prompts
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
    transcribe_audio: bool = True,
    voice_id: Optional[str] = None,
    izivoice_api_key: Optional[str] = None,
    voice_settings: Optional[Dict[str, Any]] = None,
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

        if transcribe_audio:
            # Real spoken content is unknown for uploaded audio (script_text is only
            # the filename-derived title), so transcribe via Izivoice speech-to-text
            # to get accurate subtitle text and word-level timing. This is billable
            # (Izivoice STT credits) — callers can opt out via transcribe_audio=False.
            transcript_info = generate_transcript_for_audio(raw_vo_path, fallback_text=script_text or "Audio préenregistré", api_key=izivoice_api_key)
        else:
            # Skips the paid STT call entirely — subtitles fall back to the video's
            # title evenly spread over the audio's duration (same fallback already
            # used when Izivoice isn't configured at all), not real captions.
            logger.info("Transcription IA désactivée pour cette vidéo ; sous-titres approximatifs à partir du titre.")
            duration = get_audio_duration(raw_vo_path)
            fallback_text = script_text or "Audio préenregistré"
            transcript_info = {
                "text": fallback_text,
                "duration": duration,
                "words": synthetic_word_timings(fallback_text, duration),
            }
    else:
        progress("Génération de la voix et transcription", 8)
        logger.info("Step 1/7: Generating voiceover audio via TTS...")
        _, transcript_info = generate_voiceover(script_text or "Vidéo sans titre", raw_vo_path, voice_id=voice_id, api_key=izivoice_api_key, voice_settings=voice_settings)

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
    # the AI generator had no idea what the video was even about). The same
    # word-range lookup also gives each scene its own [word_start_idx, word_end_idx]
    # into all_words — the missing link between image-scenes and subtitle-word
    # timing that the post-render editor needs to edit one scene's caption/audio
    # without touching the rest of the video.
    all_words = transcript_info.get("words") or []
    prompts = []
    word_ranges = []  # (start_idx, end_idx) per segment, end_idx inclusive; (None, None) if no words overlap
    for seg in segments:
        start_idx = end_idx = None
        seg_word_strs = []
        for idx, w in enumerate(all_words):
            if w.get("start", 0) < seg["end"] and w.get("end", 0) > seg["start"]:
                if start_idx is None:
                    start_idx = idx
                end_idx = idx
                seg_word_strs.append(w["word"])
        word_ranges.append((start_idx, end_idx))
        seg_text = " ".join(seg_word_strs).strip()
        prompts.append(seg_text if seg_text else (script_text or "")[:200])

    # For AI-generated visuals, upgrade the raw narration text into real
    # image-generation prompts sharing one consistent "visual bible" (one
    # Claude call, sees the whole script + every scene at once). Falls back
    # to the raw narration prompts above if this fails or isn't configured —
    # library-only channels skip this entirely since prompts aren't used.
    image_style_cfg = channel_config.get("image_style") or {}
    # AI image credits are capped to the first ten minutes. The resulting
    # per-video pool is shuffled for later scenes by fetch_or_generate_images.
    # Library/hybrid modes keep their existing behavior because they either use
    # creator-owned assets or already generate only half of the requested set.
    ai_original_window_seconds = 10 * 60
    ai_unique_scene_count = sum(1 for segment in segments if segment["start"] < ai_original_window_seconds)
    if image_style_cfg.get("source") in ("ai_generated", "hybrid"):
        prompt_source = prompts[:ai_unique_scene_count] if image_style_cfg.get("source") == "ai_generated" else prompts
        directed_prompts = build_scene_prompts(
            script_text=script_text or "",
            segment_texts=prompt_source,
            style_prompt=image_style_cfg.get("style_prompt", ""),
            niche=channel_config.get("niche", ""),
        )
        if directed_prompts:
            prompts = directed_prompts + prompts[len(directed_prompts):]

    image_paths = fetch_or_generate_images(
        prompts,
        images_dir,
        image_style_cfg,
        unique_generation_count=ai_unique_scene_count if image_style_cfg.get("source") == "ai_generated" else None,
    )
    
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
    zoom_min = channel_config.get("effects_config", {}).get("zoom_min_pct", 1.0)
    zoom_max = channel_config.get("effects_config", {}).get("zoom_max_pct", 1.12)

    # Each scene's clip is independent (its own ffmpeg subprocess), so building
    # them one at a time was leaving CPU cores idle for no reason — this was the
    # single biggest sequential bottleneck for long videos (dozens of clips).
    # Bounded by CPU count so a small VPS doesn't get more concurrent ffmpeg
    # processes than it has cores to actually run them on.
    clip_paths = [clips_dir / f"clip_{i+1:03d}.mp4" for i in range(len(segments))]
    max_clip_workers = max(1, min(os.cpu_count() or 2, 4))
    with ThreadPoolExecutor(max_workers=max_clip_workers) as pool:
        futures = [
            pool.submit(
                build_image_clip,
                image_path=img_path,
                output_clip_path=clip_paths[i],
                duration=seg["duration"],
                zoom_min_pct=zoom_min,
                zoom_max_pct=zoom_max,
            )
            for i, (seg, img_path) in enumerate(zip(segments, subtitled_image_paths))
        ]
        for f in futures:
            f.result()  # surface the first exception instead of silently dropping it

    # Per-scene audio segments: trimmed straight out of the master voiceover
    # (no extra TTS/STT calls) so the editor can later re-splice a single
    # scene's narration without re-synthesizing the other scenes' audio.
    audio_segments_dir = source_dir / "audio_segments"
    audio_segments_dir.mkdir(parents=True, exist_ok=True)
    audio_segment_paths = [audio_segments_dir / f"scene_{i+1:03d}.mp3" for i in range(len(segments))]
    from src.utils.ffmpeg_runner import run_ffmpeg as _run_ffmpeg_trim

    def _trim_segment(i: int, seg: Dict[str, float]) -> None:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_vo_path.resolve()),
            "-ss", f"{seg['start']:.3f}", "-to", f"{seg['end']:.3f}",
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(audio_segment_paths[i]),
        ]
        _run_ffmpeg_trim(cmd)

    with ThreadPoolExecutor(max_workers=max_clip_workers) as pool:
        futures = [pool.submit(_trim_segment, i, seg) for i, seg in enumerate(segments)]
        for f in futures:
            f.result()

    # Manifest of each scene's source image + built clip + audio segment + word
    # range, keyed by index — lets the post-render editor locate and rebuild a
    # single scene (image, caption text, or narration) without needing to
    # re-run TTS/pacing/image-fetch for the whole video.
    scenes_manifest = [
        {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "duration": seg["duration"],
            "image_path": str(image_paths[i]),
            "clip_path": str(clip_paths[i]),
            "audio_segment_path": str(audio_segment_paths[i]),
            "word_start_idx": word_ranges[i][0],
            "word_end_idx": word_ranges[i][1],
            "text": " ".join(
                w["word"] for w in all_words[word_ranges[i][0]:word_ranges[i][1] + 1]
            ) if word_ranges[i][0] is not None else "",
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
    progress("Montage final", 90)
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


def _require_editable_scene(scenes_manifest: list, scene_index: int) -> Dict[str, Any]:
    if scene_index < 0 or scene_index >= len(scenes_manifest):
        raise ValueError(f"Scene index {scene_index} out of range.")
    scene = scenes_manifest[scene_index]
    if scene.get("word_start_idx") is None:
        raise ValueError(
            "Cette scène n’a pas de plage de mots associée (vidéo générée avant cette fonctionnalité) — "
            "seul le remplacement d’image est disponible."
        )
    return scene


def edit_scene_subtitle_text(
    channel_config: Dict[str, Any],
    output_dir: Path,
    scene_index: int,
    new_text: str,
) -> Path:
    """
    Corrects one scene's caption text WITHOUT touching its audio — the new text
    is spread evenly over the scene's existing time window (same mechanism as
    the no-STT-key fallback), the other scenes' word ranges are shifted to
    account for the (possibly different) word count, and only the ASS file +
    final assembly are rebuilt. No TTS/STT call, no clip rebuild, no re-timing
    of scene start/end.
    """
    source_dir = output_dir / "source"
    scenes_path = source_dir / "scenes.json"
    transcript_path = source_dir / "transcript.json"
    if not scenes_path.exists() or not transcript_path.exists():
        raise FileNotFoundError(f"Missing scenes.json/transcript.json for {output_dir} — video predates edit support or was purged.")

    scenes_manifest = json.loads(scenes_path.read_text(encoding="utf-8"))
    transcript_info = json.loads(transcript_path.read_text(encoding="utf-8"))
    scene = _require_editable_scene(scenes_manifest, scene_index)

    words = transcript_info.get("words") or []
    old_start_idx, old_end_idx = scene["word_start_idx"], scene["word_end_idx"]
    new_words = synthetic_word_timings(new_text, scene["duration"])
    for w in new_words:
        w["start"] = round(w["start"] + scene["start"], 2)
        w["end"] = round(w["end"] + scene["start"], 2)

    old_count = old_end_idx - old_start_idx + 1
    delta_count = len(new_words) - old_count
    words = words[:old_start_idx] + new_words + words[old_end_idx + 1:]

    scene["text"] = new_text
    scene["word_end_idx"] = old_start_idx + len(new_words) - 1
    for later in scenes_manifest[scene_index + 1:]:
        if later.get("word_start_idx") is not None:
            later["word_start_idx"] += delta_count
            later["word_end_idx"] += delta_count

    transcript_info["words"] = words
    transcript_info["text"] = " ".join(w["word"] for w in words)
    transcript_path.write_text(json.dumps(transcript_info, indent=2), encoding="utf-8")
    scenes_path.write_text(json.dumps(scenes_manifest, indent=2), encoding="utf-8")

    subtitle_ass_path = source_dir / "subtitles.ass"
    generate_ass_subtitles(
        transcript_info=transcript_info,
        style_config=channel_config.get("subtitle_style", {}),
        output_ass_path=subtitle_ass_path,
    )

    clip_paths = [Path(s["clip_path"]) for s in scenes_manifest]
    clip_durations = [s["duration"] for s in scenes_manifest]
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
    logger.info(f"Edited scene {scene_index} subtitle text and reassembled {final_output_path}")
    return final_output_path


def regenerate_scene_audio(
    channel_config: Dict[str, Any],
    output_dir: Path,
    scene_index: int,
    new_text: str,
    izivoice_api_key: Optional[str] = None,
) -> Path:
    """
    Re-records one scene's narration via TTS and re-times the whole video
    around it: that scene's own Ken Burns clip is rebuilt at the new audio
    duration, every later scene's start/end shifts by the resulting delta
    (their own clips are untouched — only their position in the final
    concat/xfade moves), the full word/subtitle timeline is recomposited, all
    per-scene audio segments are re-spliced into a new voiceover track, and
    the video is reassembled. Confirmed behavior: later scenes re-time rather
    than staying fixed-duration.
    """
    import shutil
    from src.utils.ffmpeg_runner import run_ffmpeg

    source_dir = output_dir / "source"
    scenes_path = source_dir / "scenes.json"
    transcript_path = source_dir / "transcript.json"
    if not scenes_path.exists() or not transcript_path.exists():
        raise FileNotFoundError(f"Missing scenes.json/transcript.json for {output_dir} — video predates edit support or was purged.")

    scenes_manifest = json.loads(scenes_path.read_text(encoding="utf-8"))
    transcript_info = json.loads(transcript_path.read_text(encoding="utf-8"))
    scene = _require_editable_scene(scenes_manifest, scene_index)

    # 1. Re-record this scene's narration.
    audio_segments_dir = source_dir / "audio_segments"
    tmp_audio_path = audio_segments_dir / f"_tmp_scene_{scene_index}.mp3"
    _, snippet_transcript = generate_voiceover(
        new_text,
        tmp_audio_path,
        voice_id=channel_config.get("voice_id"),
        api_key=izivoice_api_key,
        voice_settings=channel_config.get("voice_settings") or {},
    )
    new_duration = max(snippet_transcript.get("duration", 0.5), 0.5)
    old_duration = scene["duration"]
    delta_duration = new_duration - old_duration

    segment_path = Path(scene["audio_segment_path"])
    shutil.move(str(tmp_audio_path), str(segment_path))

    # 2. Rebuild only this scene's clip, at the new duration.
    effects = channel_config.get("effects_config", {})
    build_image_clip(
        image_path=Path(scene["image_path"]),
        output_clip_path=Path(scene["clip_path"]),
        duration=new_duration,
        zoom_min_pct=effects.get("zoom_min_pct", 1.0),
        zoom_max_pct=effects.get("zoom_max_pct", 1.12),
    )

    # 3. Re-time this scene and every later one (their own clips don't change,
    # only where they land in the final concat/xfade).
    scene["duration"] = new_duration
    scene["end"] = round(scene["start"] + new_duration, 2)
    for later in scenes_manifest[scene_index + 1:]:
        later["start"] = round(later["start"] + delta_duration, 2)
        later["end"] = round(later["end"] + delta_duration, 2)

    # 4. Recomposite the word timeline: this scene's new words (offset to its
    # unchanged start), every later scene's existing words shifted by delta.
    words = transcript_info.get("words") or []
    old_start_idx, old_end_idx = scene["word_start_idx"], scene["word_end_idx"]
    new_words = snippet_transcript.get("words") or []
    for w in new_words:
        w["start"] = round(w["start"] + scene["start"], 2)
        w["end"] = round(w["end"] + scene["start"], 2)

    words_before = words[:old_start_idx]
    words_after = words[old_end_idx + 1:]
    for w in words_after:
        w["start"] = round(w["start"] + delta_duration, 2)
        w["end"] = round(w["end"] + delta_duration, 2)
    words = words_before + new_words + words_after

    old_count = old_end_idx - old_start_idx + 1
    delta_count = len(new_words) - old_count
    scene["text"] = new_text
    scene["word_end_idx"] = old_start_idx + len(new_words) - 1
    for later in scenes_manifest[scene_index + 1:]:
        if later.get("word_start_idx") is not None:
            later["word_start_idx"] += delta_count
            later["word_end_idx"] += delta_count

    total_duration = scenes_manifest[-1]["end"] if scenes_manifest else new_duration
    transcript_info["words"] = words
    transcript_info["text"] = " ".join(w["word"] for w in words)
    transcript_info["duration"] = total_duration
    transcript_path.write_text(json.dumps(transcript_info, indent=2), encoding="utf-8")
    scenes_path.write_text(json.dumps(scenes_manifest, indent=2), encoding="utf-8")

    # 5. Regenerate subtitles from the recomposited word timeline.
    subtitle_ass_path = source_dir / "subtitles.ass"
    generate_ass_subtitles(
        transcript_info=transcript_info,
        style_config=channel_config.get("subtitle_style", {}),
        output_ass_path=subtitle_ass_path,
    )

    # 6. Re-splice every scene's audio segment, in order, into a fresh voiceover.
    raw_vo_path = source_dir / "voiceover.mp3"
    concat_list_path = audio_segments_dir / "concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for s in scenes_manifest:
            clean_path = str(Path(s["audio_segment_path"]).resolve()).replace("'", "'\\''")
            f.write(f"file '{clean_path}'\n")
    run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
        "-c:a", "libmp3lame", "-b:a", "192k", str(raw_vo_path),
    ])
    concat_list_path.unlink(missing_ok=True)

    # 7. Remix with music at the new total duration.
    music_pref = channel_config.get("music_preference", {})
    mixed_audio_path = source_dir / "mixed_audio.mp3"
    if music_pref.get("enabled", True):
        music_track = get_background_music_track(
            music_pref=music_pref,
            duration=total_duration,
            channel_id=channel_config.get("id"),
            niche=channel_config.get("niche"),
            script_text=transcript_info["text"],
        )
        mix_audio_tracks(
            voiceover_path=raw_vo_path,
            music_path=music_track,
            output_audio_path=mixed_audio_path,
            music_volume=music_pref.get("volume", 0.15),
        )
    else:
        mixed_audio_path = raw_vo_path

    # 8. Reassemble the final video with the updated clips/durations.
    clip_paths = [Path(s["clip_path"]) for s in scenes_manifest]
    clip_durations = [s["duration"] for s in scenes_manifest]
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
    logger.info(f"Regenerated scene {scene_index} audio (Δ{delta_duration:+.2f}s) and reassembled {final_output_path}")
    return final_output_path
