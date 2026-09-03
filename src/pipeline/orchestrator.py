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
from src.pipeline.clip_builder import analyze_scene_audio_energy, build_image_clip, build_video_clip
from src.pipeline.subtitles import generate_ass_subtitles, overlay_subtitles_on_image
from src.pipeline.music import get_background_music_track
from src.pipeline.audio_mixer import mix_audio_tracks
from src.pipeline.assembler import assemble_final_video, check_ffmpeg_filter
from src.pipeline.editing_direction import resolve_editing_profile


def unresolved_visual_indices(visual_paths) -> list:
    """A scene is resolved only when it has a concrete media path."""
    return [index for index, path in enumerate(visual_paths) if not path]


def plan_visual_slots(scene_count: int, media_mode: str) -> tuple:
    """Decides, per scene, whether it wants an image or a video clip —
    before anything is downloaded/generated. Returns (video_slot_indices,
    image_slot_indices): a set and a sorted list, together covering every
    index in range(scene_count) exactly once.

    "images" reserves every scene for a still (video_slot_indices empty).
    "videos" reserves every scene for a clip. "mixed" reserves the same
    ~1-in-3 cadence B-roll placement already uses for its own clip scenes,
    so both draw from the same set of slots instead of competing for them.
    Planning this first — rather than generating an image for every scene
    and only then asking Pexels to fill whatever's still empty — is what
    makes "mixed" mode actually request stock footage: previously every
    scene already had an image path by the time that step ran, so it never
    found an empty scene to fill and a "mixed" channel silently rendered
    100% images.
    """
    if media_mode == "videos":
        video_slot_indices = set(range(scene_count))
    elif media_mode == "mixed":
        video_slot_indices = set(range(2, scene_count, 3))
    else:
        video_slot_indices = set()
    image_slot_indices = [i for i in range(scene_count) if i not in video_slot_indices]
    return video_slot_indices, image_slot_indices


def _media_checkpoint_is_valid(path: Path, expected_duration: float, tolerance: float = 0.75) -> bool:
    """Whether a completed media artifact can safely be reused after restart.

    Existence alone is insufficient: SIGKILL can leave a partially-written MP4
    or MP3 behind. ffprobe must be able to read it and its duration must remain
    close to the scene duration before it is accepted as a checkpoint.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    actual_duration = get_audio_duration(path)
    return actual_duration > 0 and abs(actual_duration - expected_duration) <= tolerance

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
    video_id: Optional[str] = None,
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
    transcript_json_path = source_dir / "transcript.json"

    if raw_vo_path.exists() and transcript_json_path.exists():
        # output_dir is deterministic per video (see queue_runner.py), so a
        # video re-queued after being interrupted mid-render (server restart,
        # deploy killing the worker, etc.) lands right back in the same
        # directory a previous attempt already wrote to. The voiceover TTS +
        # transcription STT calls are the most expensive, most re-billed step
        # of a restart — reuse what's already on disk instead of paying for
        # and redoing them every single retry.
        logger.info("Step 1/7: Reusing voiceover + transcript from a previous (interrupted) attempt instead of regenerating.")
        progress("Reprise : voix off déjà générée", 8)
        transcript_info = json.loads(transcript_json_path.read_text(encoding="utf-8"))
    elif pre_recorded_audio_path and pre_recorded_audio_path.exists():
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
            transcript_info = generate_transcript_for_audio(raw_vo_path, fallback_text=script_text or "Audio préenregistré", api_key=izivoice_api_key, user_id=channel_config.get("user_id"), video_id=video_id)
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
        _, transcript_info = generate_voiceover(script_text or "Vidéo sans titre", raw_vo_path, voice_id=voice_id, api_key=izivoice_api_key, voice_settings=voice_settings, user_id=channel_config.get("user_id"), transcribe=transcribe_audio, video_id=video_id)

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
    # How many distinct (paid, credit-debited) AI images to generate for this
    # video — creator-configurable in the wizard (image_style.max_unique_images)
    # since cost scales directly with this number. Falls back to the original
    # "first ten minutes of scenes" heuristic for channels saved before that
    # setting existed. The resulting pool is shuffled across the rest of the
    # timeline by fetch_or_generate_images either way, so total video length
    # doesn't drive AI generation cost.
    max_unique_images = image_style_cfg.get("max_unique_images")
    if max_unique_images:
        ai_unique_scene_count = min(int(max_unique_images), len(segments))
    else:
        ai_original_window_seconds = 10 * 60
        ai_unique_scene_count = sum(1 for segment in segments if segment["start"] < ai_original_window_seconds)
    from src.pipeline.images import resolve_enabled_image_sources
    enabled_sources = resolve_enabled_image_sources(image_style_cfg)
    ai_enabled = "ai_generated" in enabled_sources
    if ai_enabled:
        directed_prompts = build_scene_prompts(
            script_text=script_text or "",
            segment_texts=prompts[:ai_unique_scene_count],
            style_prompt=image_style_cfg.get("style_prompt", ""),
            niche=channel_config.get("niche", ""),
        )
        if directed_prompts:
            prompts = directed_prompts + prompts[len(directed_prompts):]

    media_mode = image_style_cfg.get("media_mode", "images")
    video_slot_indices, image_slot_indices = plan_visual_slots(len(segments), media_mode)

    # The manual "Nombre précis" count is source-agnostic (see
    # fetch_or_generate_images) — pass it through whenever the creator set
    # one, whether or not AI generation is enabled for this channel. In
    # "auto" mode, fall back to no cap at all (not the AI-only 10-minute
    # heuristic above, which only makes sense when the count also caps a
    # paid AI call). Scoped to the scenes actually asking for an image now,
    # not the full segment count, since video-slot scenes never call this.
    unique_visual_count = ai_unique_scene_count if max_unique_images else None
    if image_slot_indices:
        image_prompts = [prompts[i] for i in image_slot_indices]
        generated_images = fetch_or_generate_images(
            image_prompts, images_dir, image_style_cfg,
            unique_generation_count=min(len(image_slot_indices), unique_visual_count) if unique_visual_count else None,
            user_id=channel_config.get("user_id"), niche=channel_config.get("niche"), channel_id=channel_config.get("id"),
        )
    else:
        generated_images = []

    visual_paths = [None] * len(segments)
    visual_types = ["image"] * len(segments)
    for position, scene_index in enumerate(image_slot_indices):
        if position < len(generated_images):
            visual_paths[scene_index] = generated_images[position]
    # Snapshot before B-roll/stock/subtitle-burn steps below start
    # overwriting visual_paths — the manifest keeps each scene's original
    # source image (None for a video-slot scene) even once visual_path
    # itself has moved on to a subtitled copy or a clip.
    image_paths = list(visual_paths)

    # Creator-provided B-roll is mixed into the timeline at the same
    # cadence its scenes were already reserved at above ("videos": every
    # scene; "mixed": every third scene; "images": none — B-roll is a video
    # asset, so it has nothing to claim there).
    broll_paths = []
    broll_dir_value = image_style_cfg.get("broll_path")
    if broll_dir_value:
        from src.config import STORAGE_PATH
        candidate_dir = (STORAGE_PATH / broll_dir_value).resolve()
        storage_root = STORAGE_PATH.resolve()
        if storage_root in candidate_dir.parents and candidate_dir.is_dir():
            broll_paths = sorted([p for p in candidate_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}])
    if broll_paths:
        for i in sorted(video_slot_indices):
            visual_paths[i] = broll_paths[(i // 3) % len(broll_paths)]
            visual_types[i] = "video"
        logger.info(f"B-roll enabled: using {sum(t == 'video' for t in visual_types)} creator clip scene(s) from {len(broll_paths)} clip(s).")

    # Stock footage (Pexels) fills the scenes the creator's own clips don't
    # cover — real motion instead of a Ken Burns pan over a still, which is
    # the clearest visual difference between an automated montage and a
    # hand-cut one. Rides along with the "community" source rather than
    # being its own toggle: both are the same promise to the creator ("I
    # don't have to provide these visuals myself"), and stock keeps that
    # promise even for a niche whose shared pool is still empty. Pexels
    # itself is free to KappGen, but per product decision no asset renders
    # invisibly/for free to the creator — a token STOCK_MEDIA_CREDITS charge
    # applies per clip/photo actually used (debited only once it's confirmed
    # usable, so a failed download or an unaffordable balance never bills
    # anything and simply leaves that scene on its fallback image).
    if "community" in enabled_sources:
        from src.config import PEXELS_API_KEY
        if not PEXELS_API_KEY:
            logger.info("Stock footage enabled for this channel but PEXELS_API_KEY is unset; scenes stay on images.")
        else:
            from src.pipeline.scene_director import build_stock_search_queries
            from src.pipeline.stock_video import fetch_stock_clips
            from src.utils.billing import debit_izivoice_usage_by_user_id, STOCK_MEDIA_CREDITS
            user_id = channel_config.get("user_id")

            def _bill_stock_asset() -> bool:
                # Fails open (returns True, i.e. allows the asset) if there's
                # no user to bill — a channel with no owner is an edge case
                # elsewhere in the pipeline too, not something this specific
                # billing step should be the one to newly break on.
                if not user_id:
                    return True
                return debit_izivoice_usage_by_user_id(user_id, STOCK_MEDIA_CREDITS, "stock_media", video_id=video_id)

            # Only scenes still without footage are worth a query/download.
            # The path is the source of truth. A type label alone must never
            # make an empty scene look resolved (especially in video-only
            # mode, where every scene starts out wanting a video).
            open_indices = unresolved_visual_indices(visual_paths)
            if open_indices:
                queries = build_stock_search_queries(
                    segment_texts=[prompts[i] if i < len(prompts) else "" for i in open_indices],
                    niche=channel_config.get("niche", ""),
                )
                if queries:
                    progress("Recherche de séquences vidéo", 45)
                    clips = fetch_stock_clips(queries)
                    billed_clips = {}
                    for position, scene_index in enumerate(open_indices):
                        clip_path = clips.get(position)
                        if clip_path and _bill_stock_asset():
                            visual_paths[scene_index] = clip_path
                            visual_types[scene_index] = "video"
                            billed_clips[position] = clip_path
                    # Scenes footage didn't cover, and that have no image
                    # either (AI tier off/out of quota, empty libraries),
                    # take a real stock photograph rather than the synthetic
                    # placeholder they'd otherwise fall back to.
                    from src.pipeline.stock_video import fetch_stock_photo
                    photo_count = 0
                    for position, scene_index in enumerate(open_indices):
                        if visual_paths[scene_index]:
                            continue
                        photo_path = fetch_stock_photo(queries[position])
                        if photo_path and _bill_stock_asset():
                            visual_paths[scene_index] = photo_path
                            visual_types[scene_index] = "image"
                            photo_count += 1
                    if photo_count:
                        logger.info(f"Stock photos: {photo_count} scene(s) filled from Pexels ({photo_count * STOCK_MEDIA_CREDITS} credits billed).")
                    if billed_clips:
                        logger.info(f"Stock footage: {len(billed_clips)} scene(s) filled from Pexels ({len(billed_clips) * STOCK_MEDIA_CREDITS} credits billed).")
                        # Pexels' API terms require crediting the platform and
                        # recommend crediting the videographer — persisted here
                        # so the publisher can append real credits to the
                        # description (see youtube_metadata.stock_credits_block).
                        from src.pipeline.stock_video import collect_attributions
                        credits = collect_attributions(list(billed_clips.values()))
                        if credits:
                            (source_dir / "stock_credits.json").write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")

    # A stock query can legitimately return no video/photo, the API can be
    # unavailable, or a channel can use only its own B-roll. Never pass an
    # empty path to FFmpeg: fill only the unresolved slots with the normal
    # image pipeline (which itself ends in safe synthetic fallback artwork).
    unresolved_indices = unresolved_visual_indices(visual_paths)
    if unresolved_indices:
        logger.warning(f"{len(unresolved_indices)} scene(s) still have no video; using image fallback assets.")
        fallback_images = fetch_or_generate_images(
            [prompts[i] if i < len(prompts) else (script_text or "")[:200] for i in unresolved_indices],
            images_dir,
            image_style_cfg,
            unique_generation_count=min(len(unresolved_indices), unique_visual_count) if unique_visual_count else None,
            user_id=channel_config.get("user_id"),
            niche=channel_config.get("niche"),
            channel_id=channel_config.get("id"),
        )
        for position, scene_index in enumerate(unresolved_indices):
            if position < len(fallback_images):
                visual_paths[scene_index] = fallback_images[position]
                visual_types[scene_index] = "image"
    if any(not path for path in visual_paths):
        raise RuntimeError("Impossible de trouver ou de créer un visuel pour toutes les scènes.")

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
    has_libass = check_ffmpeg_filter("ass") or check_ffmpeg_filter("subtitles")
    subtitles_enabled = channel_config.get("subtitle_style", {}).get("enabled", True)
    
    if subtitles_enabled and not has_libass:
        logger.info("Applying direct subtitle burn onto image frames...")
        words = transcript_info.get("words", [])
        chunk_size = 6
        for i, img_p in enumerate(visual_paths):
            if visual_types[i] != "image":
                continue
            sub_img = images_dir / f"subtitled_{i+1:03d}.png"
            if words:
                start_idx = (i * chunk_size) % len(words)
                sub_text = " ".join([w["word"] for w in words[start_idx:start_idx + chunk_size]])
            else:
                sub_text = script_text[:40]
            overlay_subtitles_on_image(img_p, sub_img, sub_text, channel_config.get("subtitle_style", {}))
            visual_paths[i] = sub_img

    # 6. Build Dynamic Motion Video Clips
    logger.info("Step 5/7: Rendering motion video clips (Ken Burns effect)...")
    progress("Animation des scènes", 65)
    zoom_min = channel_config.get("effects_config", {}).get("zoom_min_pct", 1.0)
    zoom_max = channel_config.get("effects_config", {}).get("zoom_max_pct", 1.12)
    editing_profile = resolve_editing_profile(channel_config.get("niche", ""))
    logger.info(f"Niche-aware editing direction: {editing_profile['name']} ({channel_config.get('niche') or 'general'})")

    scene_energy = analyze_scene_audio_energy(raw_vo_path, segments)

    # Each scene's clip is independent (its own ffmpeg subprocess), so building
    # them one at a time was leaving CPU cores idle for no reason — this was the
    # single biggest sequential bottleneck for long videos (dozens of clips).
    # Bounded by MAX_CLIP_RENDER_WORKERS (see config.py) rather than
    # os.cpu_count() — the latter reports the host's full core count, not
    # what this container is actually capped to.
    clip_paths = [clips_dir / f"clip_{i+1:03d}.mp4" for i in range(len(segments))]
    from src.config import MAX_CLIP_RENDER_WORKERS
    max_clip_workers = max(1, MAX_CLIP_RENDER_WORKERS)
    missing_clip_indices = [
        i for i, seg in enumerate(segments)
        if not _media_checkpoint_is_valid(clip_paths[i], seg["duration"])
    ]
    missing_clip_index_set = set(missing_clip_indices)
    reused_clip_count = len(clip_paths) - len(missing_clip_indices)
    if reused_clip_count:
        logger.info(f"Resume checkpoint: reusing {reused_clip_count}/{len(clip_paths)} completed scene clip(s).")
    with ThreadPoolExecutor(max_workers=max_clip_workers) as pool:
        futures = [
            pool.submit(
                build_video_clip if visual_types[i] == "video" else build_image_clip,
                **({"video_path": visual_paths[i]} if visual_types[i] == "video" else {"image_path": visual_paths[i]}),
                output_clip_path=clip_paths[i],
                duration=seg["duration"],
                zoom_min_pct=zoom_min,
                zoom_max_pct=zoom_max,
                energy=scene_energy[i],
                scene_index=i,
                editing_profile=editing_profile,
            )
            for i, seg in enumerate(segments)
            if i in missing_clip_index_set
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

    missing_audio_indices = [
        i for i, seg in enumerate(segments)
        if not _media_checkpoint_is_valid(audio_segment_paths[i], seg["duration"])
    ]
    reused_audio_count = len(audio_segment_paths) - len(missing_audio_indices)
    if reused_audio_count:
        logger.info(f"Resume checkpoint: reusing {reused_audio_count}/{len(audio_segment_paths)} completed audio segment(s).")
    with ThreadPoolExecutor(max_workers=max_clip_workers) as pool:
        futures = [pool.submit(_trim_segment, i, segments[i]) for i in missing_audio_indices]
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
            "image_path": str(image_paths[i]) if image_paths[i] is not None else None,
            "visual_path": str(visual_paths[i]),
            "visual_type": visual_types[i],
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
    
    if _media_checkpoint_is_valid(mixed_audio_path, total_duration, tolerance=2.0):
        logger.info("Resume checkpoint: reusing completed mixed audio track.")
    elif music_pref.get("enabled", True):
        music_track = get_background_music_track(
            music_pref=music_pref,
            duration=total_duration,
            channel_id=channel_config.get("id"),
            niche=channel_config.get("niche"),
            script_text=script_text,
            user_id=channel_config.get("user_id"),
            video_id=video_id,
        )
        mix_audio_tracks(
            voiceover_path=raw_vo_path,
            music_path=music_track,
            output_audio_path=mixed_audio_path,
            music_volume=music_pref.get("volume", 0.15),
            processing=music_pref,
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
        scene_energy=scene_energy,
        editing_profile=editing_profile,
        subtitles_preburned=subtitles_enabled and not has_libass,
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
    video_id: Optional[str] = None,
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
            user_id=channel_config.get("user_id"),
        )
        mix_audio_tracks(
            voiceover_path=raw_vo_path,
            music_path=music_track,
            output_audio_path=mixed_audio_path,
            music_volume=music_pref.get("volume", 0.15),
            processing=music_pref,
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
