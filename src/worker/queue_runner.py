import shutil
import signal
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
from src.pipeline import youtube_publisher
from src.pipeline import youtube_metadata
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
        from src.utils.credentials import izivoice_key_for_user
        izivoice_api_key = izivoice_key_for_user(channel.user)

        video_dir = STORAGE_PATH / "channels" / str(channel.id) / "videos" / str(video.id)

        if video.is_reassembly:
            # Studio editor request — pending_edit says which lightweight edit
            # to run instead of the full pipeline. No pending_edit (or an
            # unrecognized/legacy "image" type) means a plain scene-image swap:
            # rebuild output.mp4 from the kept clips/subtitles/audio only.
            edit = video.pending_edit or {}
            edit_type = edit.get("type", "image")
            channel_config = channel.to_dict()
            channel_config["voice_id"] = video.voice_id
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
                    izivoice_api_key=izivoice_api_key,
                )
                # Keep the database's canonical script aligned with the
                # scene-level narration edits. YouTube metadata generation and
                # the Studio header both consume this field later.
                try:
                    import json
                    transcript_path = video_dir / "source" / "transcript.json"
                    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
                    if transcript.get("text"):
                        video.script_text = transcript["text"]
                except Exception as sync_err:
                    logger.warning(f"Could not sync edited script for video {video.id}: {sync_err}")
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
            voice_id=video.voice_id,
            izivoice_api_key=izivoice_api_key,
            voice_settings=(channel.to_dict().get("voice_settings") or {}),
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

        # Propose a ready-to-publish YouTube title/description as soon as the
        # video exists, for every channel — not just auto-mode ones — so the
        # creator always has something reviewable/editable rather than a blank
        # field until the moment they hit "Publier".
        if not video.title or not video.youtube_description:
            try:
                meta = youtube_metadata.generate_metadata(video, channel)
                if not video.title:
                    video.title = meta["title"]
                if not video.youtube_description:
                    video.youtube_description = meta["description"]
                db.commit()
            except Exception as e:
                logger.warning(f"Could not pre-generate YouTube title/description for video {video.id}: {e}")

        # How this finished video actually reaches YouTube is always the
        # creator's own choice (channel.publish_mode), independent of whether
        # the *script* was auto-generated. A failure here never fails the
        # render — the video stays available in NicheCut either way.
        if channel.youtube_refresh_token:
            if channel.publish_mode == "auto":
                try_publish_to_youtube(db, channel, video, output_mp4)
            elif channel.publish_mode == "scheduled":
                video.scheduled_publish_at = compute_scheduled_publish_at(channel)
                db.commit()
                logger.info(f"Video {video.id} scheduled to publish at {video.scheduled_publish_at} (channel {channel.id}).")
            # "manual": leave the video as-is — the creator downloads it or
            # publishes on demand from NicheCut.

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

def _channel_zone(channel: Channel) -> ZoneInfo:
    """Every creator's channel carries its own IANA timezone (auto-detected
    client-side at creation) — never a single region imposed on everyone.
    Falls back to Africa/Douala (this app's home market) if unset or invalid."""
    try:
        return ZoneInfo(channel.timezone or "Africa/Douala")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Africa/Douala")


def compute_scheduled_publish_at(channel: Channel) -> datetime:
    """A finished video is scheduled `publish_schedule_day_offset` days out,
    at `publish_schedule_hour`, both read in the channel's own timezone —
    the same daily rule for every video on this channel."""
    zone = _channel_zone(channel)
    local_now = datetime.now(zone)
    target_date = local_now.date() + timedelta(days=channel.publish_schedule_day_offset or 0)
    target_local = datetime(
        target_date.year, target_date.month, target_date.day,
        channel.publish_schedule_hour or 8, tzinfo=zone,
    )
    return target_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)  # naive UTC for storage


SCHEDULED_PUBLISH_CHECK_INTERVAL_SECONDS = 300  # every 5 min is plenty for a daily-granularity schedule


def run_scheduled_publishes():
    """Publishes any video whose channel is in publish_mode='scheduled', whose
    scheduled_publish_at has arrived, AND that the creator has approved. The
    whole point of "scheduled" is the video renders ahead of time so there's
    a review window before it goes live — a video sitting unapproved past its
    scheduled_publish_at is left alone, not silently skipped or force-published;
    it publishes the moment the creator approves it, whenever that is."""
    db = SessionLocal()
    try:
        due = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.youtube_video_id.is_(None))
            .filter(Video.scheduled_publish_at.isnot(None))
            .filter(Video.scheduled_publish_at <= datetime.utcnow())
            .filter(Video.approved_for_publish.is_(True))
            .all()
        )
        for video in due:
            channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
            if not channel or channel.publish_mode != "scheduled" or not channel.youtube_refresh_token:
                continue
            if not video.output_path:
                continue
            output_mp4 = STORAGE_PATH / video.output_path
            if not output_mp4.exists():
                logger.warning(f"Scheduled publish skipped for video {video.id}: output file missing on disk.")
                continue
            try_publish_to_youtube(db, channel, video, output_mp4)
    except Exception as e:
        logger.warning(f"Scheduled-publish pass failed: {e}")
    finally:
        db.close()


def try_publish_to_youtube(db, channel: Channel, video: Video, output_mp4: Path) -> None:
    # video.status is already DONE at this point — progress_stage is reused
    # purely as a visible "what's happening now" signal so the client sees
    # this extra step too, not just the render itself.
    video.progress_stage = "Préparation de la publication YouTube"
    db.commit()

    # Reuse the title/description already proposed (and possibly edited by
    # the creator) right after the render finished, instead of silently
    # regenerating and overwriting them at the last second.
    meta = youtube_metadata.generate_metadata(video, channel)
    if video.title:
        meta["title"] = video.title
    if video.youtube_description:
        meta["description"] = video.youtube_description
    thumbnail_path = None
    try:
        video.progress_stage = "Génération de la miniature"
        db.commit()
        thumbnail_path = youtube_metadata.generate_thumbnail(
            output_mp4, output_mp4.with_name("thumbnail.jpg"), meta.get("thumbnail_text") or meta["title"]
        )
    except Exception as e:
        logger.warning(f"Thumbnail generation failed for video {video.id}, publishing without a custom one: {e}")

    try:
        video.progress_stage = "Publication sur YouTube"
        db.commit()
        video_id = youtube_publisher.publish_video_for_channel(
            channel, output_mp4, meta["title"], meta["description"],
            thumbnail_path=thumbnail_path, tags=meta.get("tags"),
        )
        video.youtube_video_id = video_id
        video.youtube_published_at = datetime.utcnow()
        video.youtube_publish_error = None
        video.progress_stage = "Vidéo publiée sur YouTube"
        db.commit()
        logger.info(f"Auto-published video {video.id} to YouTube (channel {channel.id}) as {video_id}.")
    except Exception as e:
        video.youtube_publish_error = str(e)[:500]
        video.progress_stage = "Échec de la publication YouTube"
        db.commit()
        logger.warning(f"YouTube auto-publish failed for video {video.id} (channel {channel.id}): {e}")


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


AUTOMATION_WINDOW_START_HOUR = 7
AUTOMATION_WINDOW_END_HOUR = 11
AUTOMATION_CHECK_INTERVAL_SECONDS = 600  # every 10 min is plenty for a once-daily trigger
AUTOMATION_RECENT_TITLES_LIMIT = 20


def _channel_local_date_and_seconds(channel: Channel) -> tuple:
    """Today's date and seconds-into-day, read in this channel's own
    timezone — each creator's window is anchored to their own clock, not a
    single region imposed on everyone."""
    local_now = datetime.now(_channel_zone(channel))
    seconds_into_day = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    return local_now.strftime("%Y-%m-%d"), seconds_into_day


def _channel_target_seconds_today(channel_id: str, today_str: str, index: int = 0, total: int = 1) -> int:
    """A per-channel-per-day random target time inside the automation window,
    deterministic from (channel_id, date, index) so it doesn't change across
    worker restarts (would otherwise let a channel fire twice, or miss its
    slot, on every redeploy). When a channel generates more than one video a
    day (videos_per_day > 1), the window is split into `total` equal
    sub-slots so they land spread through the morning instead of clustering."""
    import random
    rng = random.Random(f"{channel_id}-{today_str}-{index}")
    window_s = (AUTOMATION_WINDOW_END_HOUR - AUTOMATION_WINDOW_START_HOUR) * 3600
    base_s = AUTOMATION_WINDOW_START_HOUR * 3600
    total = max(1, total)
    sub_start = base_s + (window_s * index) // total
    sub_end = base_s + (window_s * (index + 1)) // total
    return rng.randint(sub_start, max(sub_start, sub_end - 1))


def generate_and_queue_auto_video(db, channel: Channel) -> Optional[Video]:
    """Asks Claude for a fresh topic + full script (grounded in the channel's
    niche, steering clear of its recent titles) and queues it for rendering
    exactly like a manually-submitted text video. Shared by the passive daily
    pipeline below and by an on-demand "generate now" trigger — the auto
    pipeline is meant to be zero-touch, so a creator on automation_mode
    "auto" who explicitly asks for a video right now should get exactly this,
    not the manual script/voice form."""
    from src.pipeline.script_writer import generate_daily_script

    recent_titles = [
        (v.script_text or "").split("\n")[0][:120]
        for v in (
            db.query(Video)
            .filter(Video.channel_id == channel.id)
            .order_by(Video.created_at.desc())
            .limit(AUTOMATION_RECENT_TITLES_LIMIT)
            .all()
        )
    ]

    result = generate_daily_script(
        niche=channel.niche,
        recent_titles=recent_titles,
        style_prompt=channel.automation_style_prompt,
        script_structure=channel.script_structure,
    )
    if not result:
        return None

    video = Video(
        channel_id=channel.id,
        title=result["title"],
        script_text=result["script_text"],
        input_type="text",
        audio_input_path=None,
        status=VideoStatus.QUEUED.value,
        estimated_duration_seconds=max(3.0, len(result["script_text"].split()) / 2.5),
        voice_id=getattr(channel, "voice_id", None),
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video


def run_daily_automation():
    """
    Zero-human-input daily pipeline: for every channel with automation_mode
    == "auto", generates up to `videos_per_day` videos per local day, each in
    its own randomized sub-slot inside the 7h-11h window, via
    generate_and_queue_auto_video — same pipeline from there on as a
    manually-submitted text video. `auto_videos_generated_today` tracks how
    many have fired so far today (reset to 0 the moment the local date rolls
    over) so this works without a timezone-aware "count today's videos" query.
    """
    db = SessionLocal()
    try:
        channels = db.query(Channel).filter(Channel.automation_mode == "auto").all()
        for channel in channels:
            today_str, seconds_into_day = _channel_local_date_and_seconds(channel)
            if channel.active_days:
                weekday = datetime.strptime(today_str, "%Y-%m-%d").weekday()  # 0=Monday..6=Sunday
                if weekday not in channel.active_days:
                    continue
            if channel.last_auto_run_date != today_str:
                channel.last_auto_run_date = today_str
                channel.auto_videos_generated_today = 0
                db.commit()

            quota = max(1, channel.videos_per_day or 1)
            already = channel.auto_videos_generated_today or 0
            if already >= quota:
                continue

            target_seconds = _channel_target_seconds_today(channel.id, today_str, index=already, total=quota)
            if seconds_into_day < target_seconds:
                continue  # this slot hasn't arrived yet for this channel

            video = generate_and_queue_auto_video(db, channel)
            if not video:
                # Leave the counter untouched so this slot is retried on the
                # next check within today's window instead of silently
                # skipping it on a transient Claude failure.
                logger.warning(f"Daily automation: script generation failed for channel {channel.id} ('{channel.name}'); will retry.")
                continue

            channel.auto_videos_generated_today = already + 1
            db.commit()
            logger.info(f"Daily automation: queued auto-generated video {already + 1}/{quota} for channel {channel.id} ('{channel.name}') — \"{video.title}\".")
    except Exception as e:
        logger.warning(f"Daily automation pass failed: {e}")
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
    last_automation_check = 0.0
    last_scheduled_publish_check = 0.0
    while True:
        processed = process_single_queued_video()
        if single_run or _shutdown_requested:
            break
        now = time.time()
        if now - last_purge > PURGE_INTERVAL_SECONDS:
            purge_old_videos_and_uploads()
            purge_stale_edit_assets()
            last_purge = now
        if now - last_automation_check > AUTOMATION_CHECK_INTERVAL_SECONDS:
            run_daily_automation()
            last_automation_check = now
        if now - last_scheduled_publish_check > SCHEDULED_PUBLISH_CHECK_INTERVAL_SECONDS:
            run_scheduled_publishes()
            last_scheduled_publish_check = now
        if not processed:
            time.sleep(poll_interval_seconds)

if __name__ == "__main__":
    start_queue_worker()
