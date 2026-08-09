import time
import traceback
from datetime import datetime
from pathlib import Path
from src.db.session import SessionLocal, init_db
from src.db.models import Video, Channel
from src.models.project import VideoStatus
from src.pipeline.orchestrator import run_video_pipeline
from src.config import STORAGE_PATH
from src.utils.logger import logger
from src.utils.ffmpeg_runner import get_audio_duration

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

        logger.info(f"Worker picked queued video ID: {video.id} (Channel: {video.channel_id})")
        video.status = VideoStatus.RENDERING.value
        video.started_at = datetime.utcnow()
        video.progress_stage = "Démarrage du rendu"
        video.progress_percent = 2
        db.commit()

        channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
        if not channel:
            raise ValueError(f"Channel {video.channel_id} not found in database.")

        video_dir = STORAGE_PATH / "channels" / str(channel.id) / "videos" / str(video.id)
        
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

def requeue_orphaned_videos():
    """
    On worker startup, any video still marked 'rendering' was orphaned by a
    previous process being killed mid-render (e.g. a deployment restarting the
    container) — nothing else would ever pick it back up since the picker only
    looks at 'queued' videos. Reset those to 'queued' so they retry automatically.
    """
    db = SessionLocal()
    try:
        orphaned = db.query(Video).filter(Video.status == VideoStatus.RENDERING.value).all()
        for video in orphaned:
            logger.warning(f"Re-queuing orphaned video {video.id} (was stuck in 'rendering', likely from a restart mid-render).")
            video.status = VideoStatus.QUEUED.value
            video.started_at = None
        if orphaned:
            db.commit()
    finally:
        db.close()

def start_queue_worker(poll_interval_seconds: float = 2.0, single_run: bool = False):
    """
    Main loop for background worker polling SQLite for queued videos.
    """
    init_db()
    requeue_orphaned_videos()
    logger.info("Starting Nichecut Background Queue Worker...")
    while True:
        processed = process_single_queued_video()
        if single_run:
            break
        if not processed:
            time.sleep(poll_interval_seconds)

if __name__ == "__main__":
    start_queue_worker()
