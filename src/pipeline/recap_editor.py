"""Orchestrates the Recap pipeline end to end: source video (upload or
YouTube link) -> transcript -> summary narration script -> voiceover ->
still-frame captures of the source (never a video clip of it, see
recap_frames.py) -> Ken Burns assembly.

Modeled on facecam_editor.py's shape (its own separate, non-script-writer
pipeline) — see that file's docstring for why this backend stays Python +
ffmpeg + Pillow only, no extra runtime dependency introduced here either.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from src.config import STORAGE_PATH
from src.db.models import Video, Channel
from src.models.project import VideoStatus
from src.pipeline.audio_extract import ensure_extracted_audio
from src.pipeline.facecam_transcribe import transcribe_words
from src.pipeline.recap_frames import (
    choose_frame_timestamps, detect_scene_changes, snap_to_scene_changes, extract_frames,
)
from src.pipeline.clip_builder import build_image_clip
from src.pipeline.assembler import assemble_final_video
from src.pipeline.voiceover import generate_voiceover
from src.pipeline.subtitles import generate_ass_subtitles
from src.utils.ffmpeg_runner import get_audio_duration

logger = logging.getLogger(__name__)


def _manifest_dir(video: Video) -> Path:
    d = STORAGE_PATH / "recap" / video.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_recap_script(transcript_text: str, channel: Channel) -> str:
    """One LLM call, through the shared admin-ranked provider chain (Claude/
    Kie/Gemini/... — see ai_text.py), instructed to paraphrase rather than
    quote: reduces (does not eliminate) the risk of reproducing the source's
    actual dialogue verbatim, on top of recap_frames.py never lifting a video
    clip of the source, only spaced-out still frames."""
    from src.pipeline.ai_text import generate_text

    niche = (channel.niche or "").strip()
    instruction = f"""Tu es scénariste pour une chaîne YouTube de résumés vidéo{f" (niche : {niche})" if niche else ""}.
Voici la transcription intégrale d'une vidéo source :

---
{transcript_text[:12000]}
---

Écris un script de narration en français qui RÉSUME cette vidéo pour un spectateur qui ne l'a pas vue :
accroche, contexte, 4 à 6 temps forts de l'histoire/du contenu, une conclusion ou un avis.

Règles strictes :
- Reformule et paraphrase toujours avec tes propres mots — ne recopie JAMAIS une réplique ou une phrase de la transcription mot pour mot (sauf, exceptionnellement, une expression de 2-3 mots).
- Ne mentionne pas que tu lis une transcription ; écris comme un narrateur qui raconte l'histoire.
- Réponds uniquement avec le texte de narration, sans titre, sans balises, sans commentaire."""
    return generate_text(
        instruction, max_tokens=2000, operation="recap_script",
        user_id=channel.user_id, channel_id=channel.id,
    ).strip()


def run_recap_pipeline(video_id: str, db: Session) -> None:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.raw_asset_path:
        raise ValueError(f"Recap video {video_id} has no raw_asset_path")
    channel = db.query(Channel).filter(Channel.id == video.channel_id).first()

    source_path = STORAGE_PATH / video.raw_asset_path
    manifest_dir = _manifest_dir(video)

    video.progress_stage = "Transcription de la vidéo source"
    db.commit()
    source_audio = ensure_extracted_audio(source_path)
    transcript = transcribe_words(source_audio)
    transcript_text = " ".join(w["text"] for w in transcript["words"])
    source_duration = transcript["duration"] or get_audio_duration(source_audio)

    video.progress_stage = "Écriture du script de résumé"
    db.commit()
    script_text = _build_recap_script(transcript_text, channel)
    video.script_text = script_text
    db.commit()

    video.progress_stage = "Génération de la voix off"
    db.commit()
    narration_path = manifest_dir / "narration.mp3"
    narration_path, tts_meta = generate_voiceover(
        script_text, narration_path, voice_id=channel.voice_id,
        user_id=channel.user_id, channel_id=channel.id, video_id=video.id,
    )
    narration_duration = get_audio_duration(narration_path)

    video.progress_stage = "Capture des images clés de la vidéo source"
    db.commit()
    target_timestamps = choose_frame_timestamps(source_duration, narration_duration)
    scene_changes = detect_scene_changes(source_path)
    snapped_timestamps = snap_to_scene_changes(target_timestamps, scene_changes)
    frames_dir = manifest_dir / "frames"
    frames = extract_frames(source_path, snapped_timestamps, frames_dir)
    if not frames:
        raise RuntimeError("Aucune capture d'image n'a pu être extraite de la vidéo source.")

    video.progress_stage = "Montage du résumé"
    db.commit()
    clip_duration = narration_duration / len(frames)
    clips_dir = manifest_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: List[Path] = []
    for i, frame in enumerate(frames):
        clip_path = clips_dir / f"clip_{i:03d}.mp4"
        build_image_clip(frame, clip_path, duration=clip_duration, scene_index=i)
        clip_paths.append(clip_path)

    subtitle_style = (channel.subtitle_style or {}) if channel else {}
    subtitle_ass = manifest_dir / "subtitles.ass"
    words = tts_meta.get("words") if isinstance(tts_meta, dict) else None
    if words:
        generate_ass_subtitles({"words": words}, subtitle_style, subtitle_ass)
        subtitles_preburned = False
    else:
        subtitle_ass.write_text("")
        subtitles_preburned = True

    video_output_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    video_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = video_output_dir / "output.mp4"
    assemble_final_video(
        clip_paths=clip_paths,
        audio_path=narration_path,
        subtitle_ass_path=subtitle_ass,
        output_path=output_path,
        branding_config=(channel.branding if channel else None),
        subtitles_preburned=subtitles_preburned,
    )

    video.duration_seconds = narration_duration
    video.status = VideoStatus.DONE.value
    video.finished_at = datetime.utcnow()
    video.progress_stage = "Vidéo prête"
    video.progress_percent = 100
    db.commit()

    from src.pipeline.youtube_metadata import generate_thumbnail
    from src.pipeline.transcode import try_ensure_sd_variant
    from src.worker.queue_runner import _finalize_output_storage

    thumbnail_destination = video_output_dir / "thumbnail.jpg"
    try:
        generate_thumbnail(output_path, thumbnail_destination, video.title or "Résumé", None, video.id, strict=False)
        video.thumbnail_error = None
    except Exception as exc:
        video.thumbnail_error = "La miniature n'a pas pu être générée."
        logger.warning("Recap thumbnail generation failed for video %s: %s", video.id, exc)

    try_ensure_sd_variant(output_path)
    _finalize_output_storage(db, video, output_path)
    db.commit()
