import shutil
import signal
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from src.db.session import SessionLocal, init_db
from src.db.models import Video, Channel, VoiceCloneJob
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
from src.config import STORAGE_PATH, MAX_CONCURRENT_RENDERS
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

# Shown to the creator instead of a raw exception/traceback when a render
# fails because of an underlying paid-provider outage (exhausted API
# credits, a locked account, a rate limit) — deliberately generic, no
# mention of credits/quotas/API keys, which are our problem to fix, not
# something a creator can act on.
SERVICE_UNAVAILABLE_MESSAGE = "Les serveurs de KappGen sont temporairement indisponibles. Veuillez réessayer plus tard."

# Unlike SERVICE_UNAVAILABLE_MESSAGE (our own provider credits/outages — the
# creator can't act on those), an empty KappGen credit balance is entirely
# the creator's own thing to fix, so it gets its own clear, actionable
# message instead of being hidden behind the generic one.
CREDIT_INSUFFICIENT_MESSAGE = (
    "La génération automatique est en pause : ton solde de crédits KappGen est épuisé. "
    "Recharge des crédits pour que cette chaîne continue à écrire et publier ses vidéos automatiquement."
)

# Case-insensitive substrings that reliably show up in a paid provider's own
# error text when the problem is billing/quota/rate-limit related, across
# Anthropic, OpenAI, fal.ai and Izivoice's actual wording seen in production.
_BILLING_ERROR_MARKERS = (
    "credit balance", "insufficient_quota", "insufficient quota", "quota exceeded",
    "top_up", "top-up", "user is locked", "rate limit", "rate-limited", "429",
    "payment required", "402", "billing", "plans & billing", "purchase credits",
)


def _client_facing_error_message(exc: Exception) -> str:
    """Never surfaces a raw exception/traceback to a creator — those are for
    server logs only. A billing/quota-shaped error from any paid provider
    becomes the same generic outage message (see SERVICE_UNAVAILABLE_MESSAGE);
    anything else keeps its own message (still no traceback) since it's
    usually something the creator CAN act on (e.g. a corrupt upload)."""
    text = str(exc)
    if any(marker in text.lower() for marker in _BILLING_ERROR_MARKERS):
        return SERVICE_UNAVAILABLE_MESSAGE
    return text

def _channel_config_for_render(db, channel: Channel) -> dict:
    """channel.to_dict() with the watermark forced back on if the owner no
    longer has an active subscription. The stored watermark_enabled flag
    alone can't be trusted at render time: it only reflects whether they had
    an active subscription the moment they toggled it off (enforced in
    channels.py's create/update routes) — a creator could disable it while
    subscribed, then simply let the subscription lapse afterwards, and every
    future render would stay watermark-free forever with nothing else ever
    re-checking. This is the actual enforcement point: whatever the flag
    says, the real render always reflects the owner's *current* status."""
    config = channel.to_dict()
    effects = dict(config.get("effects_config") or {})
    if not effects.get("watermark_enabled", True):
        from src.utils.billing import user_has_active_subscription
        if not user_has_active_subscription(db, channel.user):
            effects["watermark_enabled"] = True
            config["effects_config"] = effects
    return config


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
            channel_config = _channel_config_for_render(db, channel)
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
            channel_config=_channel_config_for_render(db, channel),
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

        # Propose a ready-to-publish YouTube title/description/thumbnail as
        # soon as the video exists, for every channel — not just auto-mode
        # ones — so the creator always has something reviewable/ready rather
        # than a blank field until the moment they hit "Publier".
        # Always run metadata generation (not just when title/description are
        # missing) so thumbnail_text — the short caption actually baked into
        # the thumbnail image — gets computed even for videos that already
        # had a title set elsewhere (e.g. from the submission form). Using
        # the full `title` there instead produced garbled, overlong thumbnail
        # text (a whole opening sentence crammed onto the image).
        try:
            meta = youtube_metadata.generate_metadata(video, channel)
            if not video.title:
                video.title = meta["title"]
            if not video.youtube_description:
                video.youtube_description = meta["description"]
            if not video.thumbnail_text:
                video.thumbnail_text = meta["thumbnail_text"]
            db.commit()
        except Exception as e:
            logger.warning(f"Could not pre-generate YouTube title/description for video {video.id}: {e}")

        try:
            youtube_metadata.generate_thumbnail(
                output_mp4, output_mp4.with_name("thumbnail.jpg"), video.thumbnail_text or video.title or channel.name, channel=channel
            )
        except Exception as e:
            logger.warning(f"Could not pre-generate thumbnail for video {video.id}: {e}")

        # How this finished video actually reaches YouTube is always the
        # creator's own choice (channel.publish_mode), independent of whether
        # the *script* was auto-generated. A failure here never fails the
        # render — the video stays available in NicheCut either way.
        if channel.youtube_refresh_token:
            if channel.publish_mode == "auto":
                try_publish_to_youtube(db, channel, video, output_mp4)
            elif channel.publish_mode == "scheduled":
                video.scheduled_publish_at = compute_scheduled_publish_at(channel, video_id=video.id)
                db.commit()
                logger.info(f"Video {video.id} scheduled to publish at {video.scheduled_publish_at} (channel {channel.id}).")
            # "manual": leave the video as-is — the creator downloads it or
            # publishes on demand from NicheCut.

        return True

    except Exception as e:
        # Full exception + traceback stays server-side only — the creator
        # gets a clean, sanitized message (see _client_facing_error_message).
        logger.error(f"Error processing video rendering: {e}\n{traceback.format_exc()}")
        if video:
            try:
                db.refresh(video)
                video.status = VideoStatus.FAILED.value
                video.finished_at = datetime.utcnow()
                video.error_message = _client_facing_error_message(e)
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


def compute_scheduled_publish_at(channel: Channel, video_id: str = "") -> datetime:
    """A finished video is scheduled `publish_schedule_day_offset` days out,
    at an hour that depends on `publish_time_mode`:
    - "fixed": exactly `publish_schedule_hour`, every time.
    - "range" (default): a randomized time inside the channel's own publish
      window (automation_window_start/end_hour — despite the name, this
      window now governs WHEN videos go live, not when scripts get written;
      see run_daily_automation for why that gating was removed from script
      generation).
    Either way, rolled forward to the next day allowed by `active_days` if
    the target day isn't one of them.
    automation_window_start/end_hour always have a non-null DB default
    (7/11), so before publish_time_mode existed this function had no way to
    tell "fixed" and "range" apart — it silently ignored publish_schedule_hour
    for every channel. publish_time_mode is what "Heure fixe" vs
    "Plage horaire" in the wizard actually controls now."""
    import random
    zone = _channel_zone(channel)
    local_now = datetime.now(zone)
    target_date = local_now.date() + timedelta(days=channel.publish_schedule_day_offset or 0)

    if channel.active_days:
        for _ in range(8):  # at most one full week forward
            if target_date.weekday() in channel.active_days:
                break
            target_date += timedelta(days=1)

    if (channel.publish_time_mode or "range") == "fixed":
        target_hour = channel.publish_schedule_hour or 8
    else:
        start_hour = channel.automation_window_start_hour if channel.automation_window_start_hour is not None else 7
        end_hour = channel.automation_window_end_hour if channel.automation_window_end_hour is not None else 11
        if end_hour <= start_hour:
            end_hour = start_hour + 1
        rng = random.Random(f"{channel.id}-{target_date.isoformat()}-{video_id}")
        target_hour = rng.randint(start_hour, end_hour - 1)

    target_local = datetime(
        target_date.year, target_date.month, target_date.day,
        target_hour, tzinfo=zone,
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


YOUTUBE_IDENTITY_SYNC_INTERVAL_SECONDS = 6 * 3600  # every 6 hours — a rename/avatar change on YouTube isn't urgent


def run_youtube_identity_sync():
    """Keeps every connected channel's name/handle/avatar in sync with the
    real YouTube channel automatically — a creator renaming their channel or
    changing its photo directly on YouTube used to require a manual 'resync'
    click here; now it just catches up on its own within a few hours."""
    db = SessionLocal()
    try:
        channels = db.query(Channel).filter(Channel.youtube_refresh_token.isnot(None)).all()
        for channel in channels:
            try:
                access_token = youtube_publisher.get_valid_access_token(channel)
                if not access_token:
                    continue
                channel_info = youtube_publisher.fetch_own_channel_info(access_token)
                if not channel_info:
                    continue
                channel.youtube_channel_id = channel_info["id"]
                channel.youtube_channel_title = channel_info["title"]
                channel.youtube_channel_handle = channel_info.get("handle")
                channel.youtube_channel_thumbnail_url = channel_info.get("thumbnail_url")
                channel.name = channel_info["title"]
                if not channel.description:
                    channel.description = channel_info.get("description") or None
                # Backfills the video-overlay logo from the YouTube avatar for
                # any already-connected channel that never got one — no-op
                # once a logo (manual or auto) is set. See channels.py's
                # _fill_logo_from_youtube_avatar for the full rationale.
                from src.api.routes.channels import _fill_logo_from_youtube_avatar
                _fill_logo_from_youtube_avatar(channel, channel_info.get("thumbnail_url"))
                db.commit()
            except Exception as e:
                logger.warning(f"YouTube identity sync failed for channel {channel.id}: {e}")
    except Exception as e:
        logger.warning(f"YouTube identity sync pass failed: {e}")
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
    if video.thumbnail_text:
        meta["thumbnail_text"] = video.thumbnail_text
    elif meta.get("thumbnail_text"):
        video.thumbnail_text = meta["thumbnail_text"]
        db.commit()
    # Usually already sitting on disk — generated right after the render
    # finished, alongside the title/description. Only regenerate here if
    # that earlier pass failed for some reason.
    existing_thumbnail = output_mp4.with_name("thumbnail.jpg")
    thumbnail_path = existing_thumbnail if existing_thumbnail.exists() else None
    if not thumbnail_path:
        try:
            video.progress_stage = "Génération de la miniature"
            db.commit()
            thumbnail_path = youtube_metadata.generate_thumbnail(
                output_mp4, existing_thumbnail, meta.get("thumbnail_text") or meta["title"], channel=channel
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


AUTOMATION_CHECK_INTERVAL_SECONDS = 600  # every 10 min is plenty for a once-daily trigger
AUTOMATION_RECENT_TITLES_LIMIT = 20


def _channel_local_date_and_seconds(channel: Channel) -> tuple:
    """Today's date and seconds-into-day, read in this channel's own
    timezone — each creator's window is anchored to their own clock, not a
    single region imposed on everyone."""
    local_now = datetime.now(_channel_zone(channel))
    seconds_into_day = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    return local_now.strftime("%Y-%m-%d"), seconds_into_day


def _record_automation_failure(db, channel: Channel, message: str = SERVICE_UNAVAILABLE_MESSAGE) -> None:
    """Surfaces a script-generation failure as a visible Échec card instead
    of a server-log-only skip — a creator watching "Nouvelle Vidéo" do
    nothing has no way to tell a real outage apart from the automation
    simply not being set up right. Capped at one card per channel per local
    day, regardless of which message it carries: the worker retries silently
    every ~10 min on a hard outage (like an exhausted API credit balance),
    and a fresh Échec card every cycle would flood the video list for
    something the creator can only wait out (or top up) anyway."""
    today_str, _ = _channel_local_date_and_seconds(channel)
    last_failure = (
        db.query(Video)
        .filter(
            Video.channel_id == channel.id,
            Video.status == VideoStatus.FAILED.value,
            Video.progress_stage == "Échec",
        )
        .order_by(Video.created_at.desc())
        .first()
    )
    if last_failure:
        last_failure_zone = last_failure.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(_channel_zone(channel))
        if last_failure_zone.strftime("%Y-%m-%d") == today_str:
            return
    db.add(Video(
        channel_id=channel.id,
        input_type="text",
        script_text="",
        status=VideoStatus.FAILED.value,
        error_message=message,
        progress_stage="Échec",
        progress_percent=0,
    ))
    db.commit()


def generate_and_queue_auto_video(db, channel: Channel) -> Optional[Video]:
    """Asks Claude for a fresh topic + full script (grounded in the channel's
    niche, steering clear of its recent titles) and queues it for rendering
    exactly like a manually-submitted text video. Shared by the passive daily
    pipeline below and by an on-demand "generate now" trigger — the auto
    pipeline is meant to be zero-touch, so a creator on automation_mode
    "auto" who explicitly asks for a video right now should get exactly this,
    not the manual script/voice form."""
    from src.pipeline.script_writer import generate_daily_script
    from src.utils.billing import user_can_render
    from src.api.routes.videos import validate_channel_visual_source, MAX_VIDEO_DURATION_SECONDS

    owner = channel.user
    if not owner:
        logger.warning(f"Daily automation: channel {channel.id} ('{channel.name}') has no owner; skipping.")
        return None
    can_render, reason = user_can_render(db, owner)
    if not can_render:
        logger.info(f"Daily automation: channel {channel.id} ('{channel.name}') skipped — {reason}")
        _record_automation_failure(db, channel, message=CREDIT_INSUFFICIENT_MESSAGE)
        return None
    try:
        validate_channel_visual_source(channel, db)
    except Exception as exc:
        logger.warning(f"Daily automation: channel {channel.id} ('{channel.name}') has no usable visual source; skipping. ({exc})")
        return None

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

    # Folded together rather than threading a new parameter through
    # script_writer's whole call chain: the channel description says what
    # the channel is about, the style prompt says how the owner wants it
    # told — both are just extra creative context for topic/script generation.
    combined_style_prompt = "\n".join(filter(None, [
        f"What this channel is about: {channel.description}" if channel.description else None,
        channel.automation_style_prompt,
    ])) or None

    result = generate_daily_script(
        niche=channel.niche,
        recent_titles=recent_titles,
        style_prompt=combined_style_prompt,
        script_structure=channel.script_structure,
        default_language="French" if (owner.locale or "fr") == "fr" else "English",
    )
    if not result:
        _record_automation_failure(db, channel)
        return None

    estimated_duration = max(3.0, len(result["script_text"].split()) / 2.5)
    if estimated_duration > MAX_VIDEO_DURATION_SECONDS:
        logger.warning(
            f"Daily automation: generated script for channel {channel.id} ('{channel.name}') "
            f"would produce a {estimated_duration/60:.0f} min video (max {MAX_VIDEO_DURATION_SECONDS//60}); skipping."
        )
        return None

    video = Video(
        channel_id=channel.id,
        title=result["title"],
        script_text=result["script_text"],
        input_type="text",
        audio_input_path=None,
        status=VideoStatus.QUEUED.value,
        estimated_duration_seconds=estimated_duration,
        transcribe_audio=channel.transcribe_audio_default if channel.transcribe_audio_default is not None else True,
        voice_id=getattr(channel, "voice_id", None),
    )
    db.add(video)
    if owner.free_videos_used < owner.free_video_quota_granted:
        owner.free_videos_used += 1
    db.commit()
    db.refresh(video)

    from src.utils.billing import debit_script_generation_cost
    debit_script_generation_cost(db, owner, result.get("generation_cost_usd") or 0.0)
    return video


def generate_and_queue_auto_video_background(channel_id: str):
    """Runs generate_and_queue_auto_video on its own DB session/thread, for the
    "Nouvelle vidéo" on-demand trigger: the several sequential Claude calls
    inside script generation can run long enough to exceed a proxy/gateway
    request timeout, which the browser then misreports as a CORS failure
    rather than a timeout. The route just fires this and returns immediately;
    the frontend already treats generate-now as fire-and-forget and polls via
    fetchChannelVideos/fetchAllVideos."""
    db = SessionLocal()
    try:
        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        if not channel:
            return
        video = generate_and_queue_auto_video(db, channel)
        if not video:
            logger.warning(f"generate-now: script generation failed for channel {channel_id}.")
    except Exception as e:
        logger.warning(f"generate-now background run failed for channel {channel_id}: {e}")
    finally:
        db.close()


def run_daily_automation():
    """
    Zero-human-input daily pipeline: for every channel with automation_mode
    == "auto", generates up to `videos_per_day` videos per local day via
    generate_and_queue_auto_video — same pipeline from there on as a
    manually-submitted text video. `auto_videos_generated_today` tracks how
    many have fired so far today (reset to 0 the moment the local date rolls
    over) so this works without a timezone-aware "count today's videos" query.

    Script generation is optionally time- and day-gated by
    script_generation_hour/script_generation_days — separate from
    automation_window_start/end_hour + active_days, which only control when
    a FINISHED video goes LIVE on YouTube (see compute_scheduled_publish_at).
    Left unset (the default), scripts are written as soon as the day's slot
    is free, which also means the daily quota is reliably met even if the
    worker is briefly down during what would otherwise be a narrow window.
    Setting an hour lets a creator pin generation to a known, checkable time
    (e.g. to verify automation is actually firing for a given channel).
    """
    db = SessionLocal()
    try:
        channels = db.query(Channel).filter(Channel.automation_mode == "auto").all()
        for channel in channels:
            today_str, seconds_into_day = _channel_local_date_and_seconds(channel)
            if channel.last_auto_run_date != today_str:
                channel.last_auto_run_date = today_str
                channel.auto_videos_generated_today = 0
                db.commit()

            quota = max(1, channel.videos_per_day or 1)
            already = channel.auto_videos_generated_today or 0
            if already >= quota:
                continue

            if channel.script_generation_days:
                local_weekday = datetime.now(_channel_zone(channel)).weekday()
                if local_weekday not in channel.script_generation_days:
                    continue

            if channel.script_generation_hour is not None:
                local_minutes = seconds_into_day // 60
                target_minutes = channel.script_generation_hour * 60 + (channel.script_generation_minute or 0)
                if local_minutes < target_minutes:
                    continue

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


def process_single_voice_clone_job() -> bool:
    """Picks the oldest pending VoiceCloneJob and runs it to completion.
    Returns True if a job was processed, False if the queue was empty. Mirrors
    process_single_queued_video()'s locking pattern so this can safely share
    the worker process with the render lanes without two lanes double-picking
    the same job."""
    from src.pipeline.voice_clone import process_voice_clone_job
    from src.utils.credentials import izivoice_key_for_user
    from src.db.models import User

    db = SessionLocal()
    try:
        job = (
            db.query(VoiceCloneJob)
            .filter(VoiceCloneJob.status == "pending")
            .order_by(VoiceCloneJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            return False

        user = db.query(User).filter(User.id == job.user_id).first()
        api_key = izivoice_key_for_user(user) if user else None
        if not api_key:
            job.status = "error"
            job.error_message = "Izivoice n'est pas configuré."
            db.commit()
            return True

        logger.info(f"Worker picked voice-clone job {job.id} (channel {job.channel_id}).")
        process_voice_clone_job(db, job, api_key)
        return True
    except Exception as e:
        logger.error(f"Voice-clone worker pass failed: {e}")
        return True
    finally:
        db.close()


def _voice_clone_worker_loop(poll_interval_seconds: float):
    """Separate lane from video rendering — cloning is a quick, interactive
    action a creator is actively waiting on in the UI, so it shouldn't have
    to wait behind a long video render to get picked up."""
    while not _shutdown_requested:
        try:
            processed = process_single_voice_clone_job()
        except Exception as e:
            logger.error(f"Voice-clone lane hit an unexpected error: {e}")
            processed = False
        if not processed:
            time.sleep(poll_interval_seconds)


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

def _render_worker_loop(worker_name: str, poll_interval_seconds: float):
    """
    One render lane: repeatedly claims and renders the next queued video,
    independently of every other lane. `process_single_queued_video()` claims
    its row with `SELECT ... FOR UPDATE SKIP LOCKED`, so any number of these
    loops can run concurrently (across threads, even across processes) without
    two lanes ever picking up the same video.
    """
    while not _shutdown_requested:
        try:
            processed = process_single_queued_video()
        except Exception as e:
            # A lane must never die silently — an uncaught exception here
            # would permanently drop this render slot for the rest of the
            # process's life instead of just failing the one video.
            logger.error(f"Render lane {worker_name} hit an unexpected error: {e}")
            processed = False
        if not processed:
            time.sleep(poll_interval_seconds)


def start_queue_worker(poll_interval_seconds: float = 2.0, single_run: bool = False, max_concurrent_renders: Optional[int] = None):
    """
    Starts MAX_CONCURRENT_RENDERS render lanes so multiple videos (and thus
    multiple users' TTS/audio jobs) render at the same time instead of the old
    strictly-sequential one-video-at-a-time queue, plus one periodic-maintenance
    loop (purge, automation, scheduled publish, YouTube sync) that only ever
    runs once regardless of render concurrency.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    init_db()
    requeue_orphaned_videos()
    concurrency = max_concurrent_renders or MAX_CONCURRENT_RENDERS

    if single_run:
        # Used for one-shot/manual invocations — render exactly one video and
        # return, same as before parallelization existed.
        process_single_queued_video()
        return

    logger.info(f"Starting Nichecut Background Queue Worker ({concurrency} concurrent render lane(s))...")
    lanes = [
        threading.Thread(target=_render_worker_loop, args=(f"lane-{i+1}", poll_interval_seconds), daemon=True)
        for i in range(max(1, concurrency))
    ]
    lanes.append(threading.Thread(target=_voice_clone_worker_loop, args=(poll_interval_seconds,), daemon=True))
    for lane in lanes:
        lane.start()

    last_purge = 0.0
    last_automation_check = 0.0
    last_scheduled_publish_check = 0.0
    last_youtube_identity_sync = 0.0
    while not _shutdown_requested:
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
        if now - last_youtube_identity_sync > YOUTUBE_IDENTITY_SYNC_INTERVAL_SECONDS:
            run_youtube_identity_sync()
            last_youtube_identity_sync = now
        time.sleep(poll_interval_seconds)

    # SIGTERM landed — let every in-flight render lane finish its current
    # video before the process actually exits.
    for lane in lanes:
        lane.join()

if __name__ == "__main__":
    start_queue_worker()
