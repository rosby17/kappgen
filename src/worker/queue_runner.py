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

def process_single_queued_video() -> bool:
    """
    Picks the oldest 'queued' video, marks it 'rendering', runs pipeline, and records result.
    Returns True if a video was processed, False if queue was empty.
    """
    db = SessionLocal()
    video = None
    try:
        video = (
            db.query(Video)
            .filter(Video.status == VideoStatus.QUEUED.value)
            .order_by(Video.created_at.asc())
            .first()
        )
        if not video:
            db.close()
            return False

        logger.info(f"Worker picked queued video ID: {video.id} (Channel: {video.channel_id})")
        video.status = VideoStatus.RENDERING.value
        video.started_at = datetime.utcnow()
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
                
        # Execute render pipeline
        output_mp4 = run_video_pipeline(
            channel_config=channel.to_dict(),
            script_text=video.script_text,
            output_dir=video_dir,
            pre_recorded_audio_path=pre_audio_path
        )

        video.status = VideoStatus.DONE.value
        video.finished_at = datetime.utcnow()
        video.output_path = str(output_mp4.relative_to(STORAGE_PATH.parent) if STORAGE_PATH.parent in output_mp4.parents else output_mp4)
        video.source_assets_path = str((video_dir / "source").relative_to(STORAGE_PATH.parent) if STORAGE_PATH.parent in (video_dir / "source").parents else (video_dir / "source"))
        video.error_message = None
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
                db.commit()
            except Exception as db_err:
                logger.error(f"Failed to update video failed status: {db_err}")
        return False
    finally:
        db.close()

def start_queue_worker(poll_interval_seconds: float = 2.0, single_run: bool = False):
    """
    Main loop for background worker polling SQLite for queued videos.
    """
    init_db()
    logger.info("Starting Nichecut Background Queue Worker...")
    while True:
        processed = process_single_queued_video()
        if single_run:
            break
        if not processed:
            time.sleep(poll_interval_seconds)

if __name__ == "__main__":
    start_queue_worker()
