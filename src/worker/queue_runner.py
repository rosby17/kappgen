import shutil
import signal
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from src.db.session import SessionLocal, init_db
from src.db.models import Video, Channel
from src.models.project import VideoStatus
from src.pipeline.orchestrator import (
    run_video_pipeline,
    reassemble_video_output,
    edit_scene_subtitle_text,
    regenerate_scene_audio,
)
from src.pipeline.transcode import try_ensure_sd_variant
from src.config import STORAGE_PATH
from src.utils.logger import logger
from src.utils.ffmpeg_runner import get_audio_duration

# Kept at 7 days so the post-render editor's "swap a bad scene image" window
# never outlives the video itself (edit assets are a subset of what this purges).
VIDEO_RETENTION_DAYS = 7
# Editable scene assets (images/clips kept for the post-render editor) get
# their own, separate purge — either at this deadline, or immediately if the
# user explicitly closes the editor.
EDIT_ASSETS_RETENTION_DAYS = 7
UPLOAD_RETENTION_HOURS = 48
PURGE_INTERVAL_SECONDS = 3600

def process_single_queued_video() -> bool:
    """
    Picks the oldest 'queued' video, marks it 'rendering', runs pipeline, and records result.
    Returns True if a video was processed, False if queue was empty.
    """
    db = SessionLocal()
    video = None
    try:
        # Among everything currently waiting, shortest estimated video first —
        # a long render in progress is never interrupted, but once the worker
        # is free again it picks the shortest queued job so quick requests
        # don't sit behind someone else's hour-long video. Ties (or unknown
        # estimates) fall back to arrival order.
        video = (
            db.query(Video)
            .filter(Video.status == VideoStatus.QUEUED.value)
            .order_by(Video.estimated_duration_seconds.asc().nullslast(), Video.created_at.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not video:
            db.close()
            return False

        logger.info(f"Worker picked queued video ID: {video.id} (Channel: {video.channel_id}, reassembly={video.is_reassembly})")
        video.status = VideoStatus.RENDERING.value
        video.started_at = datetime.utcnow()
        video.progress_stage = "Réassemblage de la vidéo" if video.is_reassembly else "Démarrage du rendu"
        video.progress_percent = 2
        db.commit()

        channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
        if not channel:
            raise ValueError(f"Channel {video.channel_id} not found in database.")

        video_dir = STORAGE_PATH / "channels" / str(channel.id) / "videos" / str(video.id)

        if video.is_reassembly:
            # Studio editor request — pending_edit says which lightweight edit
            # to run instead of the full pipeline. No pending_edit (or an
            # unrecognized/legacy "image" type) means a plain scene-image swap:
            # rebuild output.mp4 from the kept clips/subtitles/audio only.
            edit = video.pending_edit or {}
            edit_type = edit.get("type", "image")
            channel_config = channel.to_dict()
            if edit_type == "subtitle_text":
                output_mp4 = edit_scene_subtitle_text(
                    channel_config=channel_config,
                    output_dir=video_dir,
                    scene_index=edit["scene_index"],
                    new_text=edit.get("text") or "",
                )
            elif edit_type == "audio":
                output_mp4 = regenerate_scene_audio(
                    channel_config=channel_config,
                    output_dir=video_dir,
                    scene_index=edit["scene_index"],
                    new_text=edit.get("text") or "",
                )
            else:
                output_mp4 = reassemble_video_output(channel_config=channel_config, output_dir=video_dir)
            video.pending_edit = None
            try:
                video.duration_seconds = get_audio_duration(output_mp4)
            except Exception:
                pass
            video.status = VideoStatus.DONE.value
            video.is_reassembly = False
            video.finished_at = datetime.utcnow()
            video.output_path = str(output_mp4.relative_to(STORAGE_PATH) if STORAGE_PATH in output_mp4.parents else output_mp4)
            video.error_message = None
            video.progress_stage = "Vidéo prête"
            video.progress_percent = 100
            db.commit()
            logger.info(f"Worker successfully reassembled video ID: {video.id}")
            try_ensure_sd_variant(output_mp4)
            return True

        pre_audio_path = None
        if video.audio_input_path:
            p = Path(video.audio_input_path)
            if p.exists():
                pre_audio_path = p
        if video.input_type == "audio" and pre_audio_path is None:
            raise ValueError("Le fichier audio source est introuvable sur le serveur. Veuillez créer une nouvelle vidéo et le renvoyer.")
                
        # Execute render pipeline
        def update_progress(stage: str, percent: int):
            video.progress_stage = stage
            video.progress_percent = percent
            db.commit()

        output_mp4 = run_video_pipeline(
            channel_config=channel.to_dict(),
            script_text=video.script_text,
            output_dir=video_dir,
            pre_recorded_audio_path=pre_audio_path,
            progress_callback=update_progress,
            transcribe_audio=video.transcribe_audio,
        )

        try:
            video.duration_seconds = get_audio_duration(output_mp4)
        except Exception:
            video.duration_seconds = None

        video.status = VideoStatus.DONE.value
        video.finished_at = datetime.utcnow()
        video.output_path = str(output_mp4.relative_to(STORAGE_PATH) if STORAGE_PATH in output_mp4.parents else output_mp4)
        video.source_assets_path = str((video_dir / "source").relative_to(STORAGE_PATH) if STORAGE_PATH in (video_dir / "source").parents else (video_dir / "source"))
        video.error_message = None
        video.progress_stage = "Vidéo prête"
        video.progress_percent = 100
        db.commit()
        logger.info(f"Worker successfully finished rendering video ID: {video.id}")

        # Pre-generate the SD download variant now, while the video is fresh —
        # by the time a user actually clicks "Télécharger (SD)" it's usually
        # already sitting on disk instead of making them wait through a
        # multi-minute live transcode. Runs after the DB commit above so
        # "Vidéo prête" shows immediately regardless of how long this takes.
        try_ensure_sd_variant(output_mp4)

        return True

    except Exception as e:
        logger.error(f"Error processing video rendering: {e}")
        if video:
            try:
                db.refresh(video)
                video.status = VideoStatus.FAILED.value
                video.finished_at = datetime.utcnow()
                video.error_message = f"{str(e)}\n{traceback.format_exc()}"
                video.progress_stage = "Échec du rendu"
                db.commit()
            except Exception as db_err:
                logger.error(f"Failed to update video failed status: {db_err}")
        return False
    finally:
        db.close()

MAX_AUTO_RESTARTS = 4

def requeue_orphaned_videos():
    """
    On worker startup, any video still marked 'rendering' was orphaned by a
    previous process being killed mid-render (e.g. a deployment restarting the
    container) — nothing else would ever pick it back up since the picker only
    looks at 'queued' videos. Reset those to 'queued' so they retry automatically
    (rendering isn't resumable — it restarts from the beginning, so the progress
    bar is reset to 0 immediately rather than showing a stale percentage while
    "queued"). After MAX_AUTO_RESTARTS repeated interruptions, stop looping and
    surface a clear failure instead of retrying forever.
    """
    db = SessionLocal()
    try:
        orphaned = db.query(Video).filter(Video.status == VideoStatus.RENDERING.value).all()
        for video in orphaned:
            video.restart_count = (video.restart_count or 0) + 1
            if video.restart_count > MAX_AUTO_RESTARTS:
                logger.error(f"Video {video.id} interrupted {video.restart_count} times; giving up instead of restarting again.")
                video.status = VideoStatus.FAILED.value
                video.finished_at = datetime.utcnow()
                video.error_message = (
                    f"Le rendu a été interrompu {video.restart_count} fois par des redémarrages du serveur "
                    "avant de pouvoir se terminer. Relancez-le manuellement une fois le serveur stable."
                )
                video.progress_stage = "Échec du rendu"
            else:
                logger.warning(f"Re-queuing orphaned video {video.id} (interrupted mid-render, restart #{video.restart_count}) — restarting from the beginning.")
                video.status = VideoStatus.QUEUED.value
                video.started_at = None
                video.progress_stage = f"En reprise après interruption du serveur (tentative {video.restart_count + 1})"
                video.progress_percent = 0
        if orphaned:
            db.commit()
    finally:
        db.close()

def purge_old_render_output(video: Video) -> None:
    """Deletes a finished video's rendered output + source assets from disk,
    keeping the DB record (with purged_at set) so history/stats stay intact."""
    channel_id = video.channel_id
    video_dir = STORAGE_PATH / "channels" / str(channel_id) / "videos" / str(video.id)
    if video_dir.exists():
        shutil.rmtree(video_dir, ignore_errors=True)


def purge_edit_assets(video: Video) -> None:
    """Deletes the heavy scene images/clips kept for the post-render editor,
    without touching output.mp4 or the small source files (voiceover, transcript,
    subtitles) — the video stays downloadable/watchable, just no longer editable."""
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    for sub in ("source/images", "source/clips", "source/audio_segments"):
        p = video_dir / sub
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    scenes_manifest = video_dir / "source" / "scenes.json"
    scenes_manifest.unlink(missing_ok=True)


def purge_stale_edit_assets():
    """Background sweep for the EDIT_ASSETS_RETENTION_DAYS window — most users
    trigger this earlier via the explicit 'close editor' action instead."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=EDIT_ASSETS_RETENTION_DAYS)
        stale = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.edit_assets_purged_at.is_(None))
            .filter(Video.finished_at.isnot(None))
            .filter(Video.finished_at < cutoff)
            .all()
        )
        for video in stale:
            try:
                purge_edit_assets(video)
                video.edit_assets_purged_at = datetime.utcnow()
                logger.info(f"Purged edit assets (images/clips) for video {video.id}, finished {video.finished_at}.")
            except Exception as purge_err:
                logger.warning(f"Failed to purge edit assets for video {video.id}: {purge_err}")
        if stale:
            db.commit()
    except Exception as e:
        logger.warning(f"Edit-assets purge pass failed: {e}")
    finally:
        db.close()


def purge_old_videos_and_uploads():
    """
    Frees disk space on the shared VPS:
    - Deletes rendered video files (output.mp4 + source assets) for videos
      finished more than VIDEO_RETENTION_DAYS ago. The DB record is kept
      (purged_at is set, output_path cleared) so history/counters remain.
    - Deletes uploaded source audio files older than UPLOAD_RETENTION_HOURS —
      they're only needed once, at render time, and are never reused after.
    """
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=VIDEO_RETENTION_DAYS)
        stale_videos = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.purged_at.is_(None))
            .filter(Video.finished_at.isnot(None))
            .filter(Video.finished_at < cutoff)
            .all()
        )
        for video in stale_videos:
            try:
                purge_old_render_output(video)
                video.output_path = None
                video.source_assets_path = None
                video.purged_at = datetime.utcnow()
                logger.info(f"Purged rendered files for video {video.id} (finished {video.finished_at}, older than {VIDEO_RETENTION_DAYS}d).")
            except Exception as purge_err:
                logger.warning(f"Failed to purge video {video.id}: {purge_err}")
        if stale_videos:
            db.commit()

        # Uploaded source audio is only needed once, at render time (it gets
        # copied into the video's own source/ dir when picked up by the
        # worker); anything older than the retention window is safe to drop
        # even if a video record still points at it.
        uploads_dir = STORAGE_PATH / "uploads"
        if uploads_dir.exists():
            upload_cutoff = time.time() - (UPLOAD_RETENTION_HOURS * 3600)
            for f in uploads_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < upload_cutoff:
                    f.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Storage purge pass failed: {e}")
    finally:
        db.close()


_shutdown_requested = False

def _handle_shutdown_signal(signum, frame):
    # Best-effort: a deploy/restart's SIGTERM lands here instead of killing
    # the process outright. We don't abort — process_single_queued_video()
    # keeps running its current render to completion — we just stop picking
    # up a *new* video afterwards. Whether this actually saves the in-flight
    # render still depends on the container's stop grace period; if Docker's
    # timeout expires first, SIGKILL takes the whole container regardless and
    # requeue_orphaned_videos() picks up the pieces on next boot.
    global _shutdown_requested
    logger.warning("Worker received shutdown signal; finishing current render (if any) before exiting.")
    _shutdown_requested = True

def start_queue_worker(poll_interval_seconds: float = 2.0, single_run: bool = False):
    """
    Main loop for background worker polling SQLite for queued videos.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    init_db()
    requeue_orphaned_videos()
    logger.info("Starting Nichecut Background Queue Worker...")
    last_purge = 0.0
    while True:
        processed = process_single_queued_video()
        if single_run or _shutdown_requested:
            break
        now = time.time()
        if now - last_purge > PURGE_INTERVAL_SECONDS:
            purge_old_videos_and_uploads()
            purge_stale_edit_assets()
            last_purge = now
        if not processed:
            time.sleep(poll_interval_seconds)

if __name__ == "__main__":
    start_queue_worker()
