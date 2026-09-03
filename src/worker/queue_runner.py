import shutil
import json
from concurrent.futures import ThreadPoolExecutor
import signal
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_, func
from src.db.session import SessionLocal, init_db
from src.db.models import Video, Channel, User, VoiceCloneJob, Subscription, Plan
from src.utils.email import SUPPORTED_LOCALES, send_brevo_email, email_shell, EMAIL_ACCENT
from src.config import FRONTEND_BASE_URL
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
from src.config import STORAGE_PATH, AUTOMATION_LAUNCH_SPACING_SECONDS
from src.utils.logger import logger
from src.utils.ffmpeg_runner import get_audio_duration, run_ffmpeg

# Tightened to 48h after the VPS disk filled up (200GB, rendered videos alone
# were 84GB) and started failing deploys mid-build — every video, regardless
# of plan, gets purged this fast by default now. The escape hatch is
# Video.extended_retention (an explicit per-video opt-in, meant to become a
# paid feature later — see videos.py's PATCH endpoint): those videos are
# excluded from this purge entirely and get uploaded to R2 instead of local
# disk (see _finalize_output_storage below), so "keep it longer" doesn't
# just mean "sit on the same small VPS disk longer."
VIDEO_RETENTION_HOURS = 48
# How long before the 48h purge a creator gets emailed a heads-up — enough
# time to notice and download, not so early the warning arrives while the
# video is barely finished rendering.
VIDEO_EXPIRY_WARNING_HOURS_BEFORE = 6
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
    """channel.to_dict() with watermark_enabled decided by the owner's actual
    entitlement, not just trusted from whatever flag ended up stored — this
    is the point where the watermark actually gets burned into (or left out
    of) the video, so it's the one place that must never be wrong regardless
    of how the flag got set:
    - never paid -> watermark forced back ON, even if the flag says off
      (defense in depth against the paywall being bypassed client-side)
    - has paid -> watermark forced OFF, even if the flag still says on —
      a paying creator who simply never touched the toggle (or bought
      credits after the channel was already configured) isn't penalized
      for not remembering to flip a switch."""
    config = channel.to_dict()
    effects = dict(config.get("effects_config") or {})
    from src.utils.billing import user_has_purchased_credits
    effects["watermark_enabled"] = not user_has_purchased_credits(db, channel.user)
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
        # Admin manual override first (see Video.admin_priority — set from
        # the admin Vidéos tab, e.g. "Prioriser cette vidéo"), then paid-tier
        # priority, then FIFO within the same tier. The highest currently-
        # active plan price wins; welcome-credit/free users have a priority
        # value of zero and therefore wait behind every paid tier.
        active_plan_price = (
            db.query(func.coalesce(func.max(Plan.price_fcfa), 0))
            .select_from(Subscription)
            .join(Plan, Subscription.plan_id == Plan.id)
            .filter(
                Subscription.user_id == Channel.user_id,
                Subscription.status == "active",
                Subscription.expires_at > datetime.utcnow(),
            )
            .correlate(Channel)
            .scalar_subquery()
        )
        video = (
            db.query(Video)
            .join(Channel, Video.channel_id == Channel.id)
            .filter(Video.status == VideoStatus.QUEUED.value)
            .order_by(Video.admin_priority.desc(), active_plan_price.desc(), Video.created_at.asc(), Video.id.asc())
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

        # Thumbnail art is independent of the assembled MP4: GPT Image 2 only
        # needs the headline, niche and channel moodboard. Start it immediately
        # so the expensive visual work overlaps the narration/editing render.
        # The future is joined below just before the completed video is exposed.
        thumbnail_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thumbnail")
        thumbnail_destination = video_dir / "thumbnail.jpg"
        # A thumbnail exists only when the creator supplied its style references.
        # The moodboard is therefore both the creative brief and explicit consent
        # to spend thumbnail credits; a legacy boolean alone is never enough.
        thumbnail_style = channel.thumbnail_style or {}
        thumbnail_enabled = bool(thumbnail_style.get("reference_image_paths") or thumbnail_style.get("reference_image_path"))
        thumbnail_future = thumbnail_executor.submit(
            youtube_metadata.generate_thumbnail,
            video_dir / "__thumbnail_source__.mp4", thumbnail_destination,
            video.thumbnail_text or video.title or channel.name or channel.niche or "Nouvelle vidéo",
            channel, video.id,
        ) if thumbnail_enabled else None

        def await_parallel_thumbnail():
            if thumbnail_future is None:
                thumbnail_executor.shutdown(wait=False, cancel_futures=True)
                return None
            try:
                result = thumbnail_future.result()
                logger.info("Parallel GPT Image 2 thumbnail ready for video %s", video.id)
                return result
            except Exception as exc:
                logger.warning("Parallel thumbnail failed for video %s: %s", video.id, exc)
                return None
            finally:
                thumbnail_executor.shutdown(wait=False, cancel_futures=False)

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
                    video_id=video.id,
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
            _finalize_output_storage(db, video, output_mp4)
            video.error_message = None
            video.progress_stage = "Vidéo prête"
            video.progress_percent = 100
            db.commit()
            logger.info(f"Worker successfully reassembled video ID: {video.id}")
            try_ensure_sd_variant(output_mp4)
            await_parallel_thumbnail()
            return True

        # Music Video channels (content_type == "music") skip the entire
        # script/voiceover/subtitles pipeline — see src/pipeline/music_video.py.
        # Kept as a distinct branch here rather than threading content_type
        # through run_video_pipeline itself, which is purpose-built around a
        # script and would need every internal step guarded otherwise.
        if channel.content_type == "music":
            from src.pipeline.music_video import render_music_video

            def update_progress(stage: str, percent: int):
                video.progress_stage = stage
                video.progress_percent = percent
                db.commit()

            # Same absolute rule as _channel_config_for_render for narration
            # videos: this pipeline has no effects_config/watermark_enabled
            # concept of its own, so entitlement is checked directly here
            # rather than trusting any stored flag.
            from src.utils.billing import user_has_purchased_credits
            watermark_enabled = not user_has_purchased_credits(db, channel.user)

            music_config = channel.music_channel_config or {}
            output_mp4, tracks_generated = render_music_video(
                style_prompt=music_config.get("style_prompt") or "",
                edit_mode=music_config.get("edit_mode") or "loop",
                image_count=int(music_config.get("image_count") or 0),
                target_duration_minutes=float(music_config.get("target_duration_minutes") or 10),
                niche=channel.niche,
                output_dir=video_dir,
                progress_callback=update_progress,
                watermark_enabled=watermark_enabled,
                user_id=channel.user_id,
                video_id=video.id,
            )

            try:
                video.duration_seconds = get_audio_duration(output_mp4)
            except Exception:
                video.duration_seconds = None

            video.status = VideoStatus.DONE.value
            video.finished_at = datetime.utcnow()
            _finalize_output_storage(db, video, output_mp4)
            video.error_message = None
            video.progress_stage = "Vidéo prête"
            video.progress_percent = 100
            db.commit()
            logger.info(f"Worker successfully finished rendering music video ID: {video.id}")

            await_parallel_thumbnail()

            if channel.youtube_refresh_token:
                if channel.publish_mode in ("auto", "scheduled"):
                    video.scheduled_publish_at = compute_scheduled_publish_at(channel, video_id=video.id)
                    # Green compliance can publish without intervention.
                    # Orange remains blocked until a creator explicitly approves it.
                    video.approved_for_publish = False
                    db.commit()

            return True

        pre_audio_path = None
        if video.audio_input_path:
            p = Path(video.audio_input_path)
            if p.exists():
                pre_audio_path = p
        if video.input_type == "audio" and pre_audio_path is None:
            raise ValueError("Le fichier audio source est introuvable sur le serveur. Veuillez créer une nouvelle vidéo et le renvoyer.")

        if video.input_type == "audio" and (video.youtube_compliance_report or {}).get("phase") != "audio_preflight":
            from src.pipeline.youtube_compliance import evaluate_audio_compliance
            if not video.audio_rights_confirmed:
                raise ValueError("Les droits sur l’audio et la voix doivent être confirmés avant le rendu.")

            transcript_info = {"transcription_source": "fallback"}
            if video.transcribe_audio:
                from src.pipeline.voiceover import generate_transcript_for_audio
                source_dir = video_dir / "source"
                source_dir.mkdir(parents=True, exist_ok=True)
                raw_vo_path = source_dir / "voiceover.mp3"
                transcript_path = source_dir / "transcript.json"
                if raw_vo_path.exists() and transcript_path.exists():
                    transcript_info = json.loads(transcript_path.read_text(encoding="utf-8"))
                else:
                    video.progress_stage = "Transcription et contrôle de l’audio"
                    video.progress_percent = 5
                    db.commit()
                    run_ffmpeg([
                        "ffmpeg", "-y", "-i", str(pre_audio_path.resolve()),
                        "-c:a", "libmp3lame", "-b:a", "192k", str(raw_vo_path),
                    ])
                    transcript_info = generate_transcript_for_audio(
                        raw_vo_path,
                        fallback_text=video.title or "Audio préenregistré",
                        api_key=izivoice_api_key,
                        user_id=channel.user_id,
                    )
                    transcript_path.write_text(json.dumps(transcript_info, ensure_ascii=False), encoding="utf-8")

            previous_audio = (
                db.query(Video)
                .filter(Video.channel_id == channel.id, Video.id != video.id)
                .order_by(Video.created_at.desc())
                .limit(AUTOMATION_RECENT_TITLES_LIMIT)
                .all()
            )
            audio_report = evaluate_audio_compliance(
                transcript_info, video.title or "", channel, previous_audio,
                source_type=video.audio_source_type or "third_party",
            )
            if transcript_info.get("transcription_source") == "izivoice":
                video.script_text = transcript_info.get("text") or video.script_text
            video.youtube_compliance_report = audio_report
            audit_history = list(video.youtube_compliance_history or [])
            audit_history.append({
                "at": datetime.utcnow().isoformat(),
                "event": "audio_preflight_completed",
                "details": {"score": audio_report["score"], "status": audio_report["status"]},
            })
            video.youtube_compliance_history = audit_history[-50:]
            video.approved_for_publish = False
            db.commit()
            if not audio_report["can_render"]:
                raise ValueError(audio_report["blockers"][0] if audio_report["blockers"] else "Audio bloqué par le contrôle de conformité.")
        # Final backstop, independent of how this row ended up queued: a
        # text-input video with no real script would otherwise render
        # "successfully" into a few seconds of near-silent audio, with the
        # post-render metadata step then improvising a title describing the
        # missing content (the exact "Script manquant..." videos seen in
        # production). generate_daily_script and submit_video_subject both
        # already reject this upstream — this is the last line of defense
        # for any path that doesn't, known or not.
        if video.input_type == "text" and len((video.script_text or "").strip()) < 40:
            raise ValueError("Le script de cette vidéo est vide ou trop court pour générer un rendu.")

        if video.input_type == "text" and (video.youtube_compliance_report or {}).get("phase") != "script_preflight":
            from src.pipeline.youtube_compliance import evaluate_script_compliance
            previous_scripts = (
                db.query(Video)
                .filter(Video.channel_id == channel.id, Video.id != video.id)
                .order_by(Video.created_at.desc())
                .limit(AUTOMATION_RECENT_TITLES_LIMIT)
                .all()
            )
            script_report = evaluate_script_compliance(
                video.script_text or "", video.title or "", channel, previous_scripts
            )
            video.youtube_compliance_report = script_report
            audit_history = list(video.youtube_compliance_history or [])
            audit_history.append({
                "at": datetime.utcnow().isoformat(),
                "event": "script_preflight_completed",
                "details": {"score": script_report["score"], "status": script_report["status"]},
            })
            video.youtube_compliance_history = audit_history[-50:]
            video.approved_for_publish = False
            db.commit()
            if not script_report["can_render"]:
                raise ValueError(script_report["blockers"][0] if script_report["blockers"] else "Scénario bloqué par le contrôle de conformité.")

        active_preflight = video.youtube_compliance_report or {}
        if (
            active_preflight.get("phase") in {"script_preflight", "audio_preflight"}
            and not active_preflight.get("can_render", True)
            and not video.script_compliance_overridden
        ):
            reasons = active_preflight.get("blockers") or ["Contenu bloqué par le contrôle avant montage."]
            raise ValueError(reasons[0])

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
            video_id=video.id,
        )

        try:
            video.duration_seconds = get_audio_duration(output_mp4)
        except Exception:
            video.duration_seconds = None

        video.status = VideoStatus.DONE.value
        video.finished_at = datetime.utcnow()
        _finalize_output_storage(db, video, output_mp4)
        video.source_assets_path = str((video_dir / "source").relative_to(STORAGE_PATH) if STORAGE_PATH in (video_dir / "source").parents else (video_dir / "source"))
        video.error_message = None
        video.progress_stage = "Vidéo prête"
        video.progress_percent = 100
        db.commit()
        logger.info(f"Worker successfully finished rendering video ID: {video.id}")

        try:
            from src.utils.billing import maybe_debit_base_render_fee
            maybe_debit_base_render_fee(db, channel.user, video)
        except Exception as e:
            logger.warning(f"Base render fee check failed for video {video.id}: {e}")

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

        await_parallel_thumbnail()

        # A Trust Score is part of the finished video, not something the
        # creator has to remember to request. Run this final, post-render
        # audit after metadata and thumbnail preparation so every completed
        # card can display a meaningful score immediately.
        try:
            from src.pipeline.youtube_compliance import evaluate_youtube_compliance
            previous = (
                db.query(Video)
                .filter(Video.channel_id == video.channel_id, Video.id != video.id)
                .order_by(Video.created_at.desc())
                .limit(30)
                .all()
            )
            compliance = evaluate_youtube_compliance(video, channel, previous)
            video.youtube_compliance_report = compliance
            history = list(video.youtube_compliance_history or [])
            history.append({"at": datetime.utcnow().isoformat(), "event": "trust_score_completed", "details": {"score": compliance["score"], "status": compliance["status"]}})
            video.youtube_compliance_history = history[-50:]
            db.commit()
        except Exception as e:
            logger.warning(f"Could not calculate Trust Score for video {video.id}: {e}")

        # How this finished video actually reaches YouTube is always the
        # creator's own choice (channel.publish_mode), independent of whether
        # the *script* was auto-generated. A failure here never fails the
        # render — the video stays available in NicheCut either way.
        if channel.youtube_refresh_token:
            if channel.publish_mode in ("auto", "scheduled"):
                video.scheduled_publish_at = compute_scheduled_publish_at(channel, video_id=video.id)
                # Approval is only consumed by the orange compliance path.
                # Green videos publish automatically when their slot arrives.
                video.approved_for_publish = False
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
                # The step that was actually running when this crashed
                # (e.g. "Génération de la voix et transcription", set by the
                # last update_progress() call before the exception) — refresh()
                # above just reloaded it from the last commit. Captured before
                # the next line overwrites it, and folded into error_message
                # so a creator sees exactly where it broke ("Échec à l'étape
                # « ... »") instead of a bare generic failure with no way to
                # tell an audio problem from an images or subtitles one.
                last_stage = video.progress_stage or "Démarrage du rendu"
                video.status = VideoStatus.FAILED.value
                video.finished_at = datetime.utcnow()
                video.error_message = f"Échec à l'étape « {last_stage} » : {_client_facing_error_message(e)}"
                video.progress_stage = "Échec du rendu"
                db.commit()
                # Refund every credit actually spent on this video — but only
                # for a genuine first-render failure. A reassembly (Studio
                # scene edit on an already-delivered video) failing doesn't
                # mean the video was never delivered — it was, before this
                # edit attempt — so nothing to refund there.
                if not video.is_reassembly:
                    try:
                        from src.utils.billing import refund_video_credits
                        refunded = refund_video_credits(db, video.id, f"Remboursement — vidéo échouée ({video.title or video.id})")
                        if refunded:
                            logger.info(f"Refunded {refunded} credits for failed video {video.id}.")
                    except Exception as refund_err:
                        logger.error(f"Failed to refund credits for video {video.id}: {refund_err}")
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
    scheduled_publish_at has arrived. The compliance gate decides whether it
    can leave automatically (green), needs explicit approval (orange), or must
    remain blocked (red)."""
    db = SessionLocal()
    try:
        due = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.youtube_video_id.is_(None))
            .filter(Video.scheduled_publish_at.isnot(None))
            .filter(Video.scheduled_publish_at <= datetime.utcnow())
            .all()
        )
        for video in due:
            channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
            if not channel or channel.publish_mode not in ("auto", "scheduled") or not channel.youtube_refresh_token:
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
    from src.pipeline.youtube_compliance import evaluate_youtube_compliance

    previous = (
        db.query(Video)
        .filter(Video.channel_id == video.channel_id, Video.id != video.id)
        .order_by(Video.created_at.desc())
        .limit(30)
        .all()
    )
    compliance = evaluate_youtube_compliance(video, channel, previous)
    video.youtube_compliance_report = compliance
    audit_history = list(video.youtube_compliance_history or [])
    audit_history.append({
        "at": datetime.utcnow().isoformat(), "event": "automatic_check_completed",
        "details": {"score": compliance["score"], "status": compliance["status"]},
    })
    video.youtube_compliance_history = audit_history[-50:]
    hard_blocked = not compliance["can_human_publish"] and not video.publication_compliance_overridden
    awaiting_review = compliance["requires_human_review"] and not video.approved_for_publish
    if hard_blocked or awaiting_review:
        video.youtube_publish_error = "Publication suspendue par le Contrôle YouTube KappGen."
        video.progress_stage = "Validation YouTube requise"
        db.commit()
        logger.warning("YouTube compliance blocked video %s (score=%s, status=%s).", video.id, compliance["score"], compliance["status"])
        return

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
    # Channel-level publication defaults are appended consistently to every
    # video; per-video AI metadata remains the primary title/description.
    default_description = (channel.youtube_default_description or "").strip()
    if default_description:
        meta["description"] = (meta.get("description") or "").strip() + "\n\n" + default_description
    default_tags = list(channel.youtube_default_tags or [])
    meta["tags"] = list(dict.fromkeys([*(meta.get("tags") or []), *default_tags]))[:500]
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
                output_mp4, existing_thumbnail, meta.get("thumbnail_text") or meta["title"], channel=channel, video_id=video.id
            )
        except Exception as e:
            logger.warning(f"Thumbnail generation failed for video {video.id}, publishing without a custom one: {e}")

    try:
        video.progress_stage = "Publication sur YouTube"
        db.commit()
        video_id = youtube_publisher.publish_video_for_channel(
            channel, output_mp4, meta["title"], meta["description"],
            thumbnail_path=thumbnail_path, tags=meta.get("tags"),
            privacy_status=channel.youtube_privacy_status or "public",
            category_id=channel.youtube_category_id or "22",
        )
        video.youtube_video_id = video_id
        video.youtube_published_at = datetime.utcnow()
        video.youtube_publish_error = None
        video.progress_stage = "Vidéo publiée sur YouTube"
        audit_history = list(video.youtube_compliance_history or [])
        audit_history.append({
            "at": datetime.utcnow().isoformat(), "event": "youtube_published",
            "details": {"youtube_video_id": video_id, "score": compliance["score"]},
        })
        video.youtube_compliance_history = audit_history[-50:]
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
    looks at 'queued' videos. Reset those to 'queued' so they retry automatically;
    the progress bar resets to 0 since a fresh attempt starts over from step 1,
    but run_video_pipeline() itself skips straight past voiceover generation/
    transcription if it finds them already on disk from the interrupted attempt
    (see orchestrator.py) — the expensive Izivoice calls aren't repeated even
    though the displayed progress is. After MAX_AUTO_RESTARTS repeated
    interruptions, stop looping and surface a clear failure instead of retrying
    forever.
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
                if not video.is_reassembly:
                    try:
                        from src.utils.billing import refund_video_credits
                        refunded = refund_video_credits(db, video.id, f"Remboursement — vidéo échouée ({video.title or video.id})")
                        if refunded:
                            logger.info(f"Refunded {refunded} credits for orphaned/interrupted video {video.id}.")
                    except Exception as refund_err:
                        logger.error(f"Failed to refund credits for video {video.id}: {refund_err}")
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

VOICE_CLONE_STUCK_MINUTES = 10

def requeue_orphaned_voice_clone_jobs():
    """On worker startup, any job still marked 'processing' was orphaned by a
    previous process getting killed mid-clone (the Izivoice /clone call can run
    for minutes, well past a container's stop grace period in the worst case).

    Unlike videos, this needed its own explicit recovery: a clone job used to
    stay 'pending' in an open, uncommitted DB transaction for its *entire*
    duration (see process_single_voice_clone_job's old FOR UPDATE SKIP LOCKED
    pattern) — so a hard-killed worker left the row's lock held until Postgres
    itself noticed the dropped connection, which can take a very long time
    (default TCP keepalive settings), not the immediate, active recovery
    requeue_orphaned_videos() does for renders. That's the "stuck on
    Clonage… forever" bug: the job was neither being processed nor visibly
    failed, just invisibly wedged. process_single_voice_clone_job now commits
    a 'processing' status immediately upon picking a job (releasing the lock
    right away), so a killed worker leaves a normal, queryable row instead of
    a dangling lock — this function is what actually resets it back to
    'pending' on the next startup."""
    db = SessionLocal()
    try:
        orphaned = db.query(VoiceCloneJob).filter(VoiceCloneJob.status == "processing").all()
        for job in orphaned:
            logger.warning(f"Re-queuing orphaned voice-clone job {job.id} (interrupted mid-clone).")
            job.status = "pending"
        if orphaned:
            db.commit()
    finally:
        db.close()


def _finalize_output_storage(db, video: Video, output_mp4: Path) -> None:
    """Sets video.output_path (+ storage_backend/output_size_bytes) for a
    just-finished render. Only videos with extended_retention set even
    attempt R2 — everything else gets auto-purged from local disk within
    VIDEO_RETENTION_HOURS anyway, so there's no point spending R2's free-tier
    quota on something that won't outlive it. Uploads when there's tracked
    room under the free-tier cap (see r2_storage.should_upload_to_r2);
    otherwise — R2 not configured, over the cap, or the upload itself
    failed — falls back to the local STORAGE_PATH-relative path exactly like
    before R2 existed. Local file is only deleted after a confirmed-successful
    R2 upload."""
    from src.utils import r2_storage

    try:
        size_bytes = output_mp4.stat().st_size
    except OSError:
        size_bytes = None

    if video.extended_retention and size_bytes and r2_storage.should_upload_to_r2(db, size_bytes):
        object_key = f"channels/{video.channel_id}/videos/{video.id}/output.mp4"
        url = r2_storage.upload_video(output_mp4, object_key)
        if url:
            video.output_path = url
            video.storage_backend = "r2"
            video.output_size_bytes = size_bytes
            try:
                output_mp4.unlink()
            except OSError:
                pass
            return

    video.output_path = str(output_mp4.relative_to(STORAGE_PATH) if STORAGE_PATH in output_mp4.parents else output_mp4)
    video.storage_backend = "local"
    video.output_size_bytes = size_bytes


TRASH_ROOT = STORAGE_PATH / "trash"


def purge_old_render_output(video: Video) -> None:
    """Archives a finished video's rendered output + source assets instead
    of deleting them outright, keeping the DB record (with purged_at set) so
    history/stats stay intact. The local video directory is moved wholesale
    into storage/trash/{channel_id}/{video_id}/ — recoverable server-side,
    not destroyed — one shared trash folder for every user by default, no
    opt-in needed. Only the R2-hosted output.mp4 (when storage_backend is
    "r2") is still actually deleted from R2 itself, since that's a paid
    object store with its own separate lifecycle, not local disk this trash
    folder is meant to declutter."""
    if video.storage_backend == "r2" and video.output_path:
        from src.utils import r2_storage
        object_key = r2_storage.object_key_from_url(video.output_path)
        if object_key:
            r2_storage.delete_video(object_key)

    channel_id = video.channel_id
    video_dir = STORAGE_PATH / "channels" / str(channel_id) / "videos" / str(video.id)
    if video_dir.exists():
        trash_dir = TRASH_ROOT / str(channel_id)
        trash_dir.mkdir(parents=True, exist_ok=True)
        destination = trash_dir / str(video.id)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        shutil.move(str(video_dir), str(destination))


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


def send_video_expiry_warning_email(email: str, video_title: str, hours_left: int, locale: str = "fr") -> None:
    video_url = f"{FRONTEND_BASE_URL}/videos"
    copy = {
        "fr": {
            "eyebrow": "Dernière chance",
            "title": "Ta vidéo va être supprimée bientôt",
            "intro": f"« {video_title} » sera automatiquement supprimée dans environ {hours_left}h pour libérer de l'espace serveur. Télécharge-la maintenant si tu veux la garder.",
            "cta": "Télécharger ma vidéo",
            "footer": "Les vidéos KappGen ne sont conservées que 48h après leur génération. Besoin de les garder plus longtemps ? Contacte le support.",
            "subject": f"⏳ Ta vidéo sera supprimée dans {hours_left}h",
            "preheader": f"« {video_title} » sera supprimée dans environ {hours_left}h — télécharge-la avant qu'il ne soit trop tard.",
            "text": f"« {video_title} » sera automatiquement supprimée dans environ {hours_left}h. Télécharge-la ici avant qu'il ne soit trop tard : {video_url}",
        },
        "en": {
            "eyebrow": "Last chance",
            "title": "Your video is about to be deleted",
            "intro": f'"{video_title}" will be automatically deleted in about {hours_left}h to free up server space. Download it now if you want to keep it.',
            "cta": "Download my video",
            "footer": "KappGen videos are only kept for 48h after generation. Need to keep them longer? Contact support.",
            "subject": f"⏳ Your video will be deleted in {hours_left}h",
            "preheader": f'"{video_title}" will be deleted in about {hours_left}h — download it before it\'s too late.',
            "text": f'"{video_title}" will be automatically deleted in about {hours_left}h. Download it here before it\'s too late: {video_url}',
        },
    }[locale if locale in SUPPORTED_LOCALES else "fr"]

    body = f"""
      <p style="color:{EMAIL_ACCENT};font-size:12px;font-weight:700;letter-spacing:1.4px;margin:0 0 16px;text-transform:uppercase">{copy['eyebrow']}</p>
      <h1 style="color:#eaf6ff;font-size:24px;font-weight:700;margin:0 0 12px;line-height:1.3">{copy['title']}</h1>
      <p style="color:#9badc0;font-size:15px;line-height:1.7;margin:0 0 28px">{copy['intro']}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{EMAIL_ACCENT};border-radius:10px">
                  <a href="{video_url}" style="display:inline-block;padding:12px 24px;color:#07101a;font-size:15px;font-weight:700;text-decoration:none">{copy['cta']}</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
      <p style="color:#5b6779;font-size:13px;line-height:1.6;margin:24px 0 0">{copy['footer']}</p>
    """
    send_brevo_email(
        email,
        copy["subject"],
        email_shell(copy["preheader"], body),
        copy["text"],
    )


def warn_expiring_videos():
    """Emails a creator ~VIDEO_EXPIRY_WARNING_HOURS_BEFORE hours before their
    finished video gets swept up by purge_old_videos_and_uploads() — without
    this, a video just disappeared with zero notice, which is fine for a free
    render cache but not for something a creator might not have downloaded
    yet. Runs on the same hourly tick as the purge, right before it, so the
    warning is always sent with time to spare before deletion actually
    happens. expiry_warning_sent_at guards against re-sending it every hour
    in that window.
    """
    db = SessionLocal()
    try:
        warn_cutoff = datetime.utcnow() - timedelta(hours=VIDEO_RETENTION_HOURS - VIDEO_EXPIRY_WARNING_HOURS_BEFORE)
        purge_cutoff = datetime.utcnow() - timedelta(hours=VIDEO_RETENTION_HOURS)
        expiring_videos = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.purged_at.is_(None))
            .filter(or_(Video.extended_retention.is_(False), Video.retention_until <= datetime.utcnow()))
            .filter(Video.output_path.isnot(None))
            .filter(Video.expiry_warning_sent_at.is_(None))
            .filter(Video.finished_at.isnot(None))
            .filter(Video.finished_at <= warn_cutoff)
            .filter(Video.finished_at > purge_cutoff)
            .all()
        )
        for video in expiring_videos:
            try:
                channel = video.channel or db.query(Channel).filter(Channel.id == video.channel_id).first()
                user = db.query(User).filter(User.id == channel.user_id).first() if channel and channel.user_id else None
                if not user or not user.email:
                    # Pas de propriétaire identifiable : on marque quand même
                    # comme "traité" pour ne pas re-tenter indéfiniment chaque heure.
                    video.expiry_warning_sent_at = datetime.utcnow()
                    continue

                hours_left = max(1, round(VIDEO_RETENTION_HOURS - (datetime.utcnow() - video.finished_at).total_seconds() / 3600))
                title = video.title or (video.script_text or "").strip()[:60] or "Ta vidéo"
                send_video_expiry_warning_email(user.email, title, hours_left, user.locale or "fr")
                video.expiry_warning_sent_at = datetime.utcnow()
                logger.info(f"Sent expiry warning for video {video.id} to {user.email} ({hours_left}h left).")
            except Exception as warn_err:
                logger.warning(f"Failed to send expiry warning for video {video.id}: {warn_err}")
        if expiring_videos:
            db.commit()
    except Exception as e:
        logger.warning(f"Expiry warning pass failed: {e}")
    finally:
        db.close()


def purge_old_videos_and_uploads():
    """
    Frees disk space on the shared VPS:
    - Deletes rendered video files (output.mp4 + source assets) for videos
      finished more than VIDEO_RETENTION_HOURS ago — unless the video has
      extended_retention set, in which case it's skipped entirely (those
      live on R2, not this disk, and aren't meant to be auto-deleted).
      Also skipped if the creator hasn't downloaded it or published it to
      YouTube yet — a video can sit "done" for a couple of days while the
      creator is still deciding whether to publish it, and auto-deleting
      their only copy out from under them just because the clock ran out
      is exactly the kind of surprise this retention job shouldn't cause.
      Once either downloaded_at or youtube_published_at is set, the file
      has a copy living elsewhere and is safe to purge on the usual schedule.
      The DB record is kept (purged_at is set, output_path cleared) so
      history/counters remain.
    - Deletes uploaded source audio files older than UPLOAD_RETENTION_HOURS —
      they're only needed once, at render time, and are never reused after.
    """
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=VIDEO_RETENTION_HOURS)
        stale_videos = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.purged_at.is_(None))
            .filter(or_(Video.extended_retention.is_(False), Video.retention_until <= datetime.utcnow()))
            .filter(Video.finished_at.isnot(None))
            .filter(Video.finished_at < cutoff)
            .filter(or_(Video.downloaded_at.isnot(None), Video.youtube_published_at.isnot(None)))
            .all()
        )
        for video in stale_videos:
            try:
                purge_old_render_output(video)
                video.output_path = None
                video.source_assets_path = None
                video.purged_at = datetime.utcnow()
                logger.info(f"Purged rendered files for video {video.id} (finished {video.finished_at}, older than {VIDEO_RETENTION_HOURS}h).")
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
        creation_source="automatic",
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
    from src.utils.billing import (
        user_can_render,
        estimate_script_generation_cost,
        estimate_video_cost_credits,
        debit_script_generation_cost,
    )
    from src.pipeline.script_writer import DEFAULT_SCRIPT_STRUCTURE
    from src.api.routes.videos import validate_channel_visual_source, MAX_VIDEO_DURATION_SECONDS

    owner = channel.user
    if not owner:
        logger.warning(f"Daily automation: channel {channel.id} ('{channel.name}') has no owner; skipping.")
        return None
    structure = channel.script_structure or DEFAULT_SCRIPT_STRUCTURE
    parts = structure.get("parts") or DEFAULT_SCRIPT_STRUCTURE["parts"]
    planned_words = sum(max(0, int(part.get("word_count", 0) or 0)) for part in parts)
    planned_parts = max(1, len(parts))
    planned_duration = max(3.0, planned_words / 2.5)
    estimated_cost = estimate_script_generation_cost(planned_words, planned_parts)["credits"]
    estimated_cost += estimate_video_cost_credits(
        script_char_count=max(1, planned_words * 6),
        estimated_duration_seconds=planned_duration,
        transcribe_audio=channel.transcribe_audio_default if channel.transcribe_audio_default is not None else True,
        image_style=channel.image_style,
        music_preference=channel.music_preference,
    )
    can_render, reason = user_can_render(db, owner, estimated_cost)
    if not can_render:
        logger.info(f"Daily automation: channel {channel.id} ('{channel.name}') skipped — {reason}")
        _record_automation_failure(db, channel, message=CREDIT_INSUFFICIENT_MESSAGE)
        return None
    try:
        validate_channel_visual_source(channel, db)
    except Exception as exc:
        logger.warning(f"Daily automation: channel {channel.id} ('{channel.name}') has no usable visual source; skipping. ({exc})")
        return None

    # Bug history: this used to fall back to the script's own opening
    # sentence when building the "titles already used" list fed to the next
    # topic-pick prompt — every script here opens with the same narrative
    # hook formula ("Il y a ce moment..."), so the model was shown a whole
    # list of those as if they were the channel's real titles and started
    # imitating that shape for its own "titles", producing long run-on
    # sentences instead of short punchy ones. Now uses the real title,
    # falling back to the script's first line only for legacy videos that
    # somehow have neither (should be rare-to-never going forward).
    recent_video_history = (
            db.query(Video)
            .filter(Video.channel_id == channel.id)
            .order_by(Video.created_at.desc())
            .limit(AUTOMATION_RECENT_TITLES_LIMIT)
            .all()
    )
    recent_titles = [v.title or (v.script_text or "").split("\n")[0][:120] for v in recent_video_history]
    recent_scripts = [v.script_text for v in recent_video_history if (v.script_text or "").strip()]

    # Folded together rather than threading a new parameter through
    # script_writer's whole call chain: the channel description says what
    # the channel is about, the style prompt says how the owner wants it
    # told — both are just extra creative context for topic/script generation.
    combined_style_prompt = "\n".join(filter(None, [
        f"What this channel is about: {channel.description}" if channel.description else None,
        channel.automation_style_prompt,
    ])) or None

    # Created now, before the topic/script even exist, instead of only once
    # generate_daily_script returns — a creator watching "Mes Vidéos" during
    # auto-generation used to see nothing at all until the script (and the
    # audio/scenes/visuals pipeline after it) was already well underway,
    # which read as "it goes straight to Audio" when in reality topic
    # selection and script writing both ran first, just invisibly. This row
    # is updated in place as each stage completes rather than replaced.
    video = Video(
        channel_id=channel.id,
        input_type="text",
        creation_source="automatic",
        script_text="",
        status=VideoStatus.RENDERING.value,
        progress_stage="Recherche du sujet",
        progress_percent=1,
        transcribe_audio=channel.transcribe_audio_default if channel.transcribe_audio_default is not None else True,
        voice_id=getattr(channel, "voice_id", None),
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    def _on_script_progress(stage: str, percent: int) -> None:
        video.progress_stage = stage
        video.progress_percent = percent
        db.commit()

    result = generate_daily_script(
        niche=channel.niche,
        recent_titles=recent_titles,
        style_prompt=combined_style_prompt,
        script_structure=channel.script_structure,
        default_language="French" if (owner.locale or "fr") == "fr" else "English",
        topic_examples=channel.topic_examples,
        use_web_trends=bool(channel.use_web_trends),
        on_progress=_on_script_progress,
        recent_scripts=recent_scripts,
    )
    if not result:
        db.delete(video)
        db.commit()
        _record_automation_failure(db, channel)
        return None

    # The writing model is not trusted to approve its own output. Run the
    # deterministic policy gate after text generation and before voice,
    # imagery, music, or video rendering consume any further credits.
    from src.pipeline.youtube_compliance import evaluate_script_compliance
    script_report = evaluate_script_compliance(
        result["script_text"], result["title"], channel, recent_video_history
    )
    video.title = result["title"]
    video.script_text = result["script_text"]
    video.youtube_compliance_report = script_report
    video.youtube_compliance_history = [{
        "at": datetime.utcnow().isoformat(),
        "event": "script_preflight_completed",
        "details": {"score": script_report["score"], "status": script_report["status"]},
    }]
    video.approved_for_publish = False
    if not script_report["can_render"] and not video.script_compliance_overridden:
        video.status = VideoStatus.FAILED.value
        video.error_message = script_report["blockers"][0] if script_report["blockers"] else "Scénario bloqué par le contrôle de conformité."
        video.progress_stage = "Scénario à corriger"
        video.progress_percent = 0
        db.commit()
        _record_automation_failure(db, channel)
        return None

    from src.utils.billing import user_max_video_duration_seconds
    tier_max_duration = user_max_video_duration_seconds(db, owner)
    effective_max_duration = MAX_VIDEO_DURATION_SECONDS if tier_max_duration is None else min(MAX_VIDEO_DURATION_SECONDS, tier_max_duration)

    estimated_duration = max(3.0, len(result["script_text"].split()) / 2.5)
    if estimated_duration > effective_max_duration:
        logger.warning(
            f"Daily automation: generated script for channel {channel.id} ('{channel.name}') "
            f"would produce a {estimated_duration/60:.0f} min video (max {effective_max_duration//60} for this tier); skipping."
        )
        db.delete(video)
        db.commit()
        return None

    actual_script_cost = debit_script_generation_cost(db, owner, result.get("generation_cost_usd") or 0.0, video_id=video.id)
    remaining_render_cost = estimate_video_cost_credits(
        script_char_count=len(result["script_text"]),
        estimated_duration_seconds=estimated_duration,
        transcribe_audio=channel.transcribe_audio_default if channel.transcribe_audio_default is not None else True,
        image_style=channel.image_style,
        music_preference=channel.music_preference,
    )
    can_render_after_script, post_script_reason = user_can_render(db, owner, remaining_render_cost)
    if not actual_script_cost or not can_render_after_script:
        video.status = VideoStatus.FAILED.value
        video.error_message = post_script_reason or CREDIT_INSUFFICIENT_MESSAGE
        video.progress_stage = "Échec"
        video.progress_percent = 0
        db.commit()
        return None

    video.status = VideoStatus.QUEUED.value
    video.progress_stage = None
    video.progress_percent = 0
    video.estimated_duration_seconds = estimated_duration
    db.commit()
    db.refresh(video)

    return video


def generate_and_queue_music_video(db, channel: Channel) -> Optional[Video]:
    """Queues a new music video for a "music" content_type channel — no
    script/topic to write, everything needed already lives in
    channel.music_channel_config (set once at channel setup). Shared by the
    manual "Nouvelle vidéo" trigger and, later, the daily auto pipeline
    (Phase 5), same relationship generate_and_queue_auto_video has with its
    own on-demand trigger."""
    from src.pipeline.music_video import pick_music_video_title
    from src.utils.billing import user_can_render, IZIVOICE_MUSIC_CREDITS

    owner = channel.user
    if not owner:
        logger.warning(f"Music video: channel {channel.id} ('{channel.name}') has no owner; skipping.")
        return None
    music_config = channel.music_channel_config or {}
    style_prompt = (music_config.get("style_prompt") or "").strip()
    if not style_prompt:
        logger.warning(f"Music video: channel {channel.id} ('{channel.name}') has no style configured; skipping.")
        return None

    recent_titles = [
        v.title for v in (
            db.query(Video)
            .filter(Video.channel_id == channel.id)
            .filter(Video.title.isnot(None))
            .order_by(Video.created_at.desc())
            .limit(AUTOMATION_RECENT_TITLES_LIMIT)
            .all()
        )
    ]
    title = pick_music_video_title(style_prompt, music_config.get("title_examples"), recent_titles)
    target_duration_minutes = float(music_config.get("target_duration_minutes") or 10)
    # The generator may create at most 20 tracks. Pre-authorize that maximum
    # so a long music render can never exceed the creator's available balance.
    can_render, reason = user_can_render(db, owner, IZIVOICE_MUSIC_CREDITS * 20)
    if not can_render:
        logger.info(f"Music video: channel {channel.id} ('{channel.name}') skipped — {reason}")
        return None

    video = Video(
        channel_id=channel.id,
        title=title,
        script_text="",
        input_type="text",
        creation_source="automatic",
        status=VideoStatus.QUEUED.value,
        estimated_duration_seconds=target_duration_minutes * 60.0,
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video


def generate_and_queue_music_video_background(channel_id: str):
    """Same reasoning as generate_and_queue_auto_video_background — fired
    from the on-demand route, runs on its own session/thread so the request
    can return immediately instead of holding the connection open."""
    db = SessionLocal()
    try:
        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        if channel:
            generate_and_queue_music_video(db, channel)
    except Exception as e:
        logger.error(f"generate_and_queue_music_video_background failed for channel {channel_id}: {e}\n{traceback.format_exc()}")
    finally:
        db.close()


def retry_auto_video_script_background(video_id: str):
    """Regenerates the script for an existing Video that failed with none at
    all (see the "no script" branch of retry_video in api/routes/videos.py),
    instead of the plain retry path re-queuing it as-is — which would just
    render the empty script_text "successfully" into a near-silent few-second
    video with a placeholder title, rather than actually retrying what
    failed. Runs on its own session/thread for the same reason
    generate_and_queue_auto_video_background does: script generation makes
    several sequential Claude calls and can run past a request timeout."""
    from src.pipeline.script_writer import generate_daily_script
    from src.api.routes.videos import MAX_VIDEO_DURATION_SECONDS

    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return
        channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
        owner = channel.user if channel else None
        if not channel or not owner:
            video.status = VideoStatus.FAILED.value
            video.error_message = SERVICE_UNAVAILABLE_MESSAGE
            video.progress_stage = "Échec"
            db.commit()
            return

        recent_video_history = (
                db.query(Video)
                .filter(Video.channel_id == channel.id, Video.id != video.id)
                .order_by(Video.created_at.desc())
                .limit(AUTOMATION_RECENT_TITLES_LIMIT)
                .all()
        )
        recent_titles = [v.title or (v.script_text or "").split("\n")[0][:120] for v in recent_video_history]
        recent_scripts = [v.script_text for v in recent_video_history if (v.script_text or "").strip()]
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
            topic_examples=channel.topic_examples,
            use_web_trends=bool(channel.use_web_trends),
            recent_scripts=recent_scripts,
        )
        if not result:
            video.status = VideoStatus.FAILED.value
            video.error_message = SERVICE_UNAVAILABLE_MESSAGE
            video.progress_stage = "Échec"
            db.commit()
            return

        from src.pipeline.youtube_compliance import evaluate_script_compliance
        script_report = evaluate_script_compliance(
            result["script_text"], result["title"], channel, recent_video_history
        )
        video.title = result["title"]
        video.script_text = result["script_text"]
        video.youtube_compliance_report = script_report
        audit_history = list(video.youtube_compliance_history or [])
        audit_history.append({
            "at": datetime.utcnow().isoformat(),
            "event": "script_preflight_completed",
            "details": {"score": script_report["score"], "status": script_report["status"]},
        })
        video.youtube_compliance_history = audit_history[-50:]
        video.approved_for_publish = False
        if not script_report["can_render"] and not video.script_compliance_overridden:
            video.status = VideoStatus.FAILED.value
            video.error_message = script_report["blockers"][0] if script_report["blockers"] else "Scénario bloqué par le contrôle de conformité."
            video.progress_stage = "Scénario à corriger"
            video.progress_percent = 0
            db.commit()
            return

        from src.utils.billing import user_max_video_duration_seconds, debit_script_generation_cost
        tier_max_duration = user_max_video_duration_seconds(db, owner)
        effective_max_duration = MAX_VIDEO_DURATION_SECONDS if tier_max_duration is None else min(MAX_VIDEO_DURATION_SECONDS, tier_max_duration)
        estimated_duration = max(3.0, len(result["script_text"].split()) / 2.5)
        if estimated_duration > effective_max_duration:
            video.status = VideoStatus.FAILED.value
            video.error_message = f"Le script généré ferait une vidéo de {estimated_duration/60:.0f} min, au-delà de la limite de ton palier ({effective_max_duration//60} min)."
            video.progress_stage = "Échec"
            db.commit()
            return

        video.estimated_duration_seconds = estimated_duration
        video.status = VideoStatus.QUEUED.value
        video.progress_stage = "En attente du moteur de rendu"
        video.progress_percent = 0
        db.commit()
        debit_script_generation_cost(db, owner, result.get("generation_cost_usd") or 0.0, video_id=video.id)
    except Exception as e:
        logger.error(f"retry_auto_video_script_background failed for video {video_id}: {e}\n{traceback.format_exc()}")
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                video.status = VideoStatus.FAILED.value
                video.error_message = SERVICE_UNAVAILABLE_MESSAGE
                video.progress_stage = "Échec"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


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
        # content_type == "music" excluded: this loop only knows how to write
        # a narration script (generate_and_queue_auto_video calls Claude with
        # the channel's niche/script_structure, neither of which mean anything
        # for a music channel) — daily automation for music channels is its
        # own not-yet-built feature (Phase 5), not this loop silently
        # mis-running them through the narration pipeline in the meantime.
        channels = db.query(Channel).filter(
            Channel.automation_mode == "auto",
            Channel.is_active.is_(True),
            Channel.content_type != "music",
        ).all()
        for channel in channels:
            # Re-fetch this channel's current state right before using it —
            # the sweep loads every eligible channel once up front, then
            # works through them one by one with a real delay between each
            # (AUTOMATION_LAUNCH_SPACING_SECONDS, plus however long script
            # generation itself takes), so a channel far down the list can
            # be minutes stale by the time its turn comes. A voice (or
            # niche, script structure, etc.) the creator just configured
            # mid-sweep must be picked up by the very next channel processed,
            # not only on the next sweep — an explicit refresh removes any
            # doubt, regardless of SQLAlchemy's own expire-on-commit timing.
            try:
                db.refresh(channel)
            except Exception:
                continue  # channel was deleted/became unreachable mid-sweep
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

            # Only pace actual launches, not skipped channels — otherwise a
            # sweep with many gated/ineligible channels would stall for no
            # reason. See AUTOMATION_LAUNCH_SPACING_SECONDS in config.py.
            if AUTOMATION_LAUNCH_SPACING_SECONDS > 0:
                time.sleep(AUTOMATION_LAUNCH_SPACING_SECONDS)
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

        # Committed right away (releasing the FOR UPDATE lock immediately)
        # instead of staying "pending" in an open transaction for the whole
        # multi-minute Izivoice call — a worker killed mid-clone now leaves a
        # normal, queryable "processing" row that requeue_orphaned_voice_clone_jobs()
        # can actively reset on next startup, instead of a dangling lock that
        # only clears once Postgres notices the dead connection (which can
        # take a very long time) — this was the "stuck on Clonage… forever" bug.
        job.status = "processing"
        db.commit()

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

def _render_worker_loop(worker_name: str, lane_index: int, poll_interval_seconds: float):
    """
    One render lane. lane_index is 1-based and fixed for this thread's whole
    life; every poll it compares itself against the admin's current
    max_concurrent_renders() setting (src/utils/app_settings.py, adjustable
    live from the "Ressources" tab, no redeploy) and only claims a video when
    lane_index is within that limit — otherwise it just idles. This is what
    makes turning concurrency up or down from the admin UI take effect
    within one poll interval: the pool of lane threads is fixed at startup
    (RENDER_LANE_POOL_SIZE), only how many of them are *allowed* to work
    changes.
    """
    from src.utils.app_settings import max_concurrent_renders
    while not _shutdown_requested:
        if lane_index > max_concurrent_renders():
            time.sleep(poll_interval_seconds)
            continue
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
    Starts a fixed pool of render lanes (RENDER_LANE_POOL_SIZE, matching
    MAX_CONCURRENT_RENDERS_CEILING), plus the independent voice-clone and
    periodic-maintenance loops. How many of those lanes are actually allowed
    to claim a video at any moment is controlled live by the admin's
    max_concurrent_renders() setting — see _render_worker_loop above — not by
    how many threads exist, so the admin UI can move it between 1 and the
    ceiling without a restart. Each lane claims its own video via
    process_single_queued_video()'s `with_for_update(skip_locked=True)` row
    lock, so lanes never race for the same video, and every render writes to
    its own video_dir — nothing shared between two videos rendering at once.
    The worker container itself is CPU-capped by Docker (2.5 cores at time of
    writing), which is what actually bounds host impact: raising the admin
    setting lets that budget be shared by more videos instead of raising it,
    so this is safe to turn up without a host resource change — the ceiling
    exists so the admin UI can't be pushed past the point where lanes start
    fighting each other for that same CPU budget instead of finishing videos
    faster. The max_concurrent_renders parameter is kept only for tests/
    one-off callers that want a hard override instead of the live setting.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    from src.utils.error_tracking import init_error_tracking
    from src.utils.app_settings import MAX_CONCURRENT_RENDERS_CEILING, set_max_concurrent_renders
    init_error_tracking("worker")
    init_db()
    requeue_orphaned_videos()
    requeue_orphaned_voice_clone_jobs()
    if max_concurrent_renders is not None:
        set_max_concurrent_renders(max_concurrent_renders)
    pool_size = MAX_CONCURRENT_RENDERS_CEILING

    if single_run:
        # Used for one-shot/manual invocations — render exactly one video and
        # return, same as before parallelization existed.
        process_single_queued_video()
        return

    logger.info(f"Starting Nichecut Background Queue Worker ({pool_size} render lane(s) available, admin-controlled active count)...")
    lanes = [
        threading.Thread(target=_render_worker_loop, args=(f"lane-{i+1}", i + 1, poll_interval_seconds), daemon=True)
        for i in range(pool_size)
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
            warn_expiring_videos()
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
