import json
from pathlib import Path
from typing import Dict, Any, Optional
from src.utils.logger import logger
from src.pipeline.voiceover import generate_voiceover
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
    pre_recorded_audio_path: Optional[Path] = None
) -> Path:
    """
    Orchestrates the entire video generation pipeline for a given script/audio and channel configuration.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    images_dir = source_dir / "images"
    clips_dir = source_dir / "clips"
    
    # 1. Save Config Snapshot & Script
    (source_dir / "script.txt").write_text(script_text or "", encoding="utf-8")
    (source_dir / "config_snapshot.json").write_text(json.dumps(channel_config, indent=2), encoding="utf-8")
    
    # 2. Voiceover & Audio Alignment Setup
    raw_vo_path = source_dir / "voiceover.mp3"
    
    if pre_recorded_audio_path and pre_recorded_audio_path.exists():
        logger.info(f"Step 1/7: Using pre-recorded audio file: {pre_recorded_audio_path}")
        # Copy pre-recorded audio or convert to MP3
        from src.utils.ffmpeg_runner import run_ffmpeg, get_audio_duration
        cmd = ["ffmpeg", "-y", "-i", str(pre_recorded_audio_path.resolve()), "-c:a", "libmp3lame", "-b:a", "192k", str(raw_vo_path)]
        run_ffmpeg(cmd)
        
        duration = get_audio_duration(raw_vo_path)
        words_list = [w for w in (script_text or "").split() if w.strip()]
        if not words_list:
            words_list = ["Audio", "préenregistré", "déjà", "prêt"]
            
        word_dur = max(0.2, duration / len(words_list))
        words_timed = []
        for idx, w in enumerate(words_list):
            words_timed.append({
                "word": w,
                "start": round(idx * word_dur, 2),
                "end": round((idx + 1) * word_dur, 2)
            })
            
        transcript_info = {
            "text": script_text or "Audio préenregistré",
            "duration": duration,
            "words": words_timed
        }
    else:
        logger.info("Step 1/7: Generating voiceover audio via TTS...")
        _, transcript_info = generate_voiceover(script_text or "Vidéo sans titre", raw_vo_path)

    (source_dir / "transcript.json").write_text(json.dumps(transcript_info, indent=2), encoding="utf-8")
    
    total_duration = transcript_info.get("duration", 10.0)
    
    # 3. Calculate Pacing Segments
    logger.info("Step 2/7: Calculating dynamic image pacing...")
    segments = calculate_pacing_segments(total_duration)
    
    # 4. Fetch or Generate Images
    logger.info("Step 3/7: Preparing image pool...")
    prompts = [f"Scene for text section {i+1}" for i in range(len(segments))]
    image_paths = fetch_or_generate_images(prompts, images_dir, channel_config.get("image_style"))
    
    # 5. Generate Subtitles ASS file
    logger.info("Step 4/7: Formatting ASS subtitles...")
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
        
    # 7. Mix Voiceover and Background Music
    logger.info("Step 6/7: Mixing audio tracks...")
    music_pref = channel_config.get("music_preference", {})
    mixed_audio_path = source_dir / "mixed_audio.mp3"
    
    if music_pref.get("enabled", True):
        music_track = get_background_music_track(
            style=music_pref.get("track_id_or_style", "ambient"),
            duration=total_duration
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
    final_output_path = output_dir / "output.mp4"
    assemble_final_video(
        clip_paths=clip_paths,
        audio_path=mixed_audio_path,
        subtitle_ass_path=subtitle_ass_path,
        output_path=final_output_path,
        effects_config=channel_config.get("effects_config"),
        branding_config=channel_config.get("branding")
    )
    
    logger.info(f"Pipeline successfully rendered video to {final_output_path}")
    return final_output_path
