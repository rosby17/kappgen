import shutil
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
import signal
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_, and_
from src.db.session import SessionLocal, init_db
from src.db.models import Video, Channel, User, VoiceCloneJob
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

# REINSTATED Sept 2026: moving every finished render to Backblaze B2 (see
# _finalize_output_storage) removed the original reason for a purge clock
# (the VPS disk filling up), so it was dropped for a while — but B2 storage
# isn't free either, and a video nobody ever comes back to still costs money
# to keep forever. 30 days free, then a creator pays in RETENTION_TIERS
# (videos.py) to push retention_until further out — always a fresh, finite
# purchase, on purpose: no "à vie" tier, so an inactive video's storage cost
# never goes permanently unpaid. See purge_old_videos_and_uploads /
# POLICY_REINSTATED_AT below for the grandfathering that keeps this from
# retroactively deleting years of videos finished before this shipped.
VIDEO_RETENTION_HOURS = 24 * 30
VIDEO_EXPIRY_WARNING_HOURS_BEFORE = 48
# Editable scene assets (images/clips kept for the post-render editor) get
# their own, separate purge — either at this deadline, or immediately if the
# user explicitly closes the editor. Tightened from 7 to 3 days (Sept 2026):
# now that purge_edit_assets archives to B2 before deleting (never a real
# loss), there's no reason to let 3 extra days of editable-window footprint
# pile up on local disk.
EDIT_ASSETS_RETENTION_DAYS = 3
UPLOAD_RETENTION_HOURS = 48
PURGE_INTERVAL_SECONDS = 3600

# Shown to the creator instead of a raw exception/traceback when a render
# fails because of an underlying paid-provider outage (exhausted API
# credits, a locked account, a rate limit) — deliberately generic, no
# mention of credits/quotas/API keys, which are our problem to fix, not
# something a creator can act on.
SERVICE_UNAVAILABLE_MESSAGE = "Les serveurs de KappGen sont temporairement indisponibles. Veuillez réessayer plus tard."


class VideoCancelledError(Exception):
    """Raised from update_progress (both the narration and music branches)
    when a creator cancels a video (POST /{video_id}/cancel) while it's
    still queued or rendering. Checked between pipeline stages rather than
    inside them, so an in-flight render stops at the next stage boundary
    instead of finishing into a result nobody wanted."""
    pass

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
        # One public rule: first launched, first processed. A platform admin
        # may explicitly set admin_priority for an exceptional intervention,
        # but plan/tier is never an implicit queue priority.
        #
        # Automatic videos are inserted immediately with an empty script so
        # their place is durable. Their background script writer owns that
        # empty row; the render worker must wait until it has real content.
        ready_for_render = or_(
            Video.is_reassembly.is_(True),
            Channel.automation_mode != "auto",
            # Music channels (content_type == "music") never have a
            # script_text at all — render_music_video works from
            # music_channel_config directly (style prompt, image count,
            # duration), skipping the script/voiceover pipeline entirely.
            # Without this exemption, an auto-mode music channel's video
            # never satisfied the script_text check below and stayed
            # "queued" forever — no background script writer was ever
            # going to fill in a field this content type doesn't use.
            Channel.content_type == "music",
            # Same reasoning, same latent bug: a facecam upload (creator's
            # own talking-head recording, edited via facecam_editor.py) never
            # has a script_text either — nothing generates one. An auto-mode
            # channel would otherwise trap it in "queued" forever exactly
            # like the music case above.
            Video.input_type == "facecam",
            and_(Video.script_text.is_not(None), Video.script_text != ""),
        )
        video = (
            db.query(Video)
            .join(Channel, Video.channel_id == Channel.id)
            .filter(Video.status == VideoStatus.QUEUED.value, ready_for_render)
            .order_by(Video.admin_priority.desc(), Video.created_at.asc(), Video.id.asc())
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

        # Facecam videos (uploaded talking-head recording, cut/verified/
        # b-roll'd/carded by facecam_editor.py) follow a completely different
        # shape than the faceless script->TTS->stock pipeline below — no
        # thumbnail-from-frame prefetch, no compliance preflight, no scene
        # list. Branch out early and let this function's existing except
        # block (FAILED status + credit refund) cover it same as any other
        # video on exception.
        if video.input_type == "facecam":
            from src.pipeline.facecam_editor import run_facecam_pipeline
            run_facecam_pipeline(video.id, db)
            return True

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
        # `disabled` is the explicit opt-out ("je ne veux pas de miniature") —
        # checked first so stale reference_image_paths left over from before
        # the creator opted out can never silently re-enable paid generation.
        thumbnail_style = channel.thumbnail_style or {}
        thumbnail_enabled = (not thumbnail_style.get("disabled")) and bool(
            thumbnail_style.get("reference_image_paths") or thumbnail_style.get("reference_image_path")
        )
        # /retry and /retry-visuals both re-queue this same video row without
        # ever touching thumbnail.jpg — only images/clips/scenes.json (or
        # nothing at all, for a plain full retry) get cleared. Without this
        # check, every retry silently paid THUMBNAIL_CREDITS again for a
        # brand-new AI thumbnail the creator never asked for, on top of
        # whatever the retry itself was scoped to fix. A creator who
        # deliberately wants a new one already has the explicit regenerate/
        # resync endpoints in videos.py for that.
        thumbnail_already_exists = thumbnail_destination.exists()
        # video.thumbnail_text is only ever populated by the post-render
        # metadata step further down — at this point, before render has even
        # started, it's still empty, so this used to fall back straight to
        # video.title (the full headline, often 10+ words). GPT Image 2 tries
        # to cram all of it into the reserved ~42% text area and, past a
        # certain length, starts dropping/truncating words mid-render
        # ("EMPÊC", "DÉGIVRE T") instead of the short 2-7 word punchline it's
        # actually designed for. Generating the short thumbnail_text now
        # (cheap, single call) costs a few seconds up front but keeps the
        # much longer parallel image render fed with text that actually fits.
        if not video.thumbnail_text:
            try:
                video.thumbnail_text = youtube_metadata.generate_metadata(video, channel).get("thumbnail_text")
                db.commit()
            except Exception as exc:
                logger.warning(f"Early thumbnail_text generation failed for video {video.id}, falling back to the full title: {exc}")
        thumbnail_future = thumbnail_executor.submit(
            youtube_metadata.generate_thumbnail,
            video_dir / "__thumbnail_source__.mp4", thumbnail_destination,
            video.thumbnail_text or video.title or channel.name or channel.niche or "Nouvelle vidéo",
            channel, video.id, strict=True,
        ) if (thumbnail_enabled and not thumbnail_already_exists) else None

        def await_parallel_thumbnail():
            """Returns (path, ai_used) — ai_used=False means generate_thumbnail
            fell all the way through to its own fallback (a plain solid-color
            frame, since __thumbnail_source__.mp4 never exists this early —
            the parallel job starts before the video is rendered, so it has
            no real frame to grab either). The caller re-attempts a proper
            thumbnail once the real output.mp4 exists — see the ai_used check
            right after this is awaited below."""
            if thumbnail_future is None:
                thumbnail_executor.shutdown(wait=False, cancel_futures=True)
                return None, False
            try:
                result, ai_used = thumbnail_future.result()
                logger.info("Parallel thumbnail ready for video %s (ai_used=%s)", video.id, ai_used)
                return result, ai_used
            except Exception as exc:
                logger.warning("Parallel thumbnail failed for video %s: %s", video.id, exc)
                return None, False
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
            video.error_message = None
            video.progress_stage = "Vidéo prête"
            video.progress_percent = 100
            db.commit()
            logger.info(f"Worker successfully reassembled video ID: {video.id}")
            # Must run BEFORE _finalize_output_storage: both read output_mp4
            # from local disk, which that call uploads to B2 and deletes
            # locally on success — reversing the order left every SD-variant
            # pregeneration and thumbnail retry failing with "No such file or
            # directory" against a file already moved to B2 (see queue_runner
            # commit history, Sept 2026 B2 migration).
            try_ensure_sd_variant(output_mp4)
            await_parallel_thumbnail()
            _finalize_output_storage(db, video, output_mp4)
            db.commit()
            return True

        # Music Video channels (content_type == "music") skip the entire
        # script/voiceover/subtitles pipeline — see src/pipeline/music_video.py.
        # Kept as a distinct branch here rather than threading content_type
        # through run_video_pipeline itself, which is purpose-built around a
        # script and would need every internal step guarded otherwise.
        if channel.content_type == "music":
            from src.pipeline.music_video import render_music_video

            def update_progress(stage: str, percent: int):
                # Re-checked fresh from the DB every stage transition — a
                # cancellation lands via a different request/session, so this
                # ORM object wouldn't otherwise see it before its own next
                # refresh/query.
                if db.query(Video.status).filter(Video.id == video.id).scalar() == VideoStatus.CANCELLED.value:
                    raise VideoCancelledError(f"Video {video.id} cancelled by the creator.")
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
            configured_duration_seconds = float(
                music_config.get("target_duration_seconds")
                or float(music_config.get("target_duration_minutes") or 10) * 60.0
            )
            # The queued record owns the chosen duration. This lets every
            # automatic render vary naturally while a retry keeps its exact
            # original target instead of changing length part-way through.
            target_duration_seconds = float(video.estimated_duration_seconds or configured_duration_seconds)
            raw_image_count = music_config.get("image_count")
            if raw_image_count in (None, "", "auto"):
                # No fixed UI cap here (there used to be one, hardcoded to 0-3
                # regardless of how long the video was, which looped the same
                # handful of images for hours on a long compilation) — instead
                # scale with the actual target duration: one distinct image
                # roughly every 40s, so a fixed image never overstays its
                # welcome. Mirrors the frontend's own 'auto' default exactly.
                image_count = max(1, round(target_duration_seconds / 40))
            else:
                image_count = int(raw_image_count)
            output_mp4, tracks_generated = render_music_video(
                style_prompt=music_config.get("style_prompt") or "",
                edit_mode=music_config.get("edit_mode") or "loop",
                image_count=image_count,
                target_duration_seconds=target_duration_seconds,
                niche=channel.niche,
                output_dir=video_dir,
                progress_callback=update_progress,
                watermark_enabled=watermark_enabled,
                user_id=channel.user_id,
                video_id=video.id,
                music_source_mode=music_config.get("music_source_mode") or "ai_generate",
                own_tracks=(channel.music_preference or {}).get("tracks") or [],
                image_style=channel.image_style,
                effects_config=channel.effects_config,
                subtitle_style=channel.subtitle_style,
                subtitle_text=music_config.get("subtitle_text") or "",
                channel_id=channel.id,
            )

            try:
                video.duration_seconds = get_audio_duration(output_mp4)
            except Exception:
                video.duration_seconds = None

            video.status = VideoStatus.DONE.value
            video.finished_at = datetime.utcnow()
            video.error_message = None
            video.progress_stage = "Vidéo prête"
            video.progress_percent = 100
            db.commit()
            logger.info(f"Worker successfully finished rendering music video ID: {video.id}")

            try:
                from src.utils.billing import debit_base_render_fee
                debit_base_render_fee(db, channel.user, video)
            except Exception as e:
                logger.warning(f"Base render fee debit failed for video {video.id}: {e}")

            # Before _finalize_output_storage — see the reassembly branch
            # above for why the order matters.
            await_parallel_thumbnail()
            _finalize_output_storage(db, video, output_mp4)
            db.commit()

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
            if db.query(Video.status).filter(Video.id == video.id).scalar() == VideoStatus.CANCELLED.value:
                raise VideoCancelledError(f"Video {video.id} cancelled by the creator.")
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
        video.source_assets_path = str((video_dir / "source").relative_to(STORAGE_PATH) if STORAGE_PATH in (video_dir / "source").parents else (video_dir / "source"))
        video.error_message = None
        video.progress_stage = "Vidéo prête"
        video.progress_percent = 100
        db.commit()
        logger.info(f"Worker successfully finished rendering video ID: {video.id}")

        try:
            from src.utils.billing import debit_base_render_fee
            debit_base_render_fee(db, channel.user, video)
        except Exception as e:
            logger.warning(f"Base render fee debit failed for video {video.id}: {e}")

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
            # `if not video.title` alone left two real classes of ugly title
            # stuck forever, since both leave title non-empty from the moment
            # the row is created: the automatic-video placeholder ("<channel>
            # — nouvelle vidéo", set at creation before the topic is even
            # picked — see generate_and_queue_auto_video's own on_title
            # callback, which normally replaces it early but not on every
            # path), and an audio upload's filename-derived fallback when the
            # filename itself was a raw upload id ("upload_07a27e10-5f0d-...")
            # rather than anything a creator actually typed. Both get folded
            # into the same "needs a real title" check as an empty one,
            # rather than only ever helping a video that started with no
            # title at all.
            title_is_placeholder = not video.title or video.title.strip().endswith("— nouvelle vidéo") or bool(
                re.search(r"[0-9a-f]{8}[\s-][0-9a-f]{4}[\s-][0-9a-f]{4}[\s-][0-9a-f]{4}[\s-][0-9a-f]{12}", video.title, re.IGNORECASE)
            )
            meta = youtube_metadata.generate_metadata(video, channel, reuse_existing=not title_is_placeholder)
            if title_is_placeholder:
                video.title = meta["title"]
            if not video.youtube_description:
                video.youtube_description = meta["description"]
            if not video.thumbnail_text:
                video.thumbnail_text = meta["thumbnail_text"]
            db.commit()
        except Exception as e:
            logger.warning(f"Could not pre-generate YouTube title/description for video {video.id}: {e}")

        _, thumbnail_ai_used = await_parallel_thumbnail()
        # Every finished video must have a visible card thumbnail. Channels
        # without a reference style skip the parallel AI job, but still get a
        # representative frame from the finished MP4 here.
        if not thumbnail_ai_used and not thumbnail_already_exists:
            # The parallel attempt above started before the video existed and
            # failed its AI call — strict=True means it raised rather than
            # writing a generic, unstyled placeholder (see
            # generate_thumbnail's docstring: publishing something with none
            # of the creator's actual reference style was worse than a clear
            # "couldn't make one" state). Retry once for real now that
            # output_mp4 exists — transient AI timeouts do happen — still
            # strict, so a second failure leaves no thumbnail file at all
            # rather than a mediocre one.
            try:
                youtube_metadata.generate_thumbnail(
                    output_mp4, thumbnail_destination,
                    video.thumbnail_text or video.title or channel.name or channel.niche or "Nouvelle vidéo",
                    channel if thumbnail_enabled else None, video.id, strict=False,
                )
                video.thumbnail_error = None
                logger.info(f"Post-render thumbnail retry for video {video.id} succeeded.")
            except Exception as exc:
                video.thumbnail_error = (
                    "La miniature n'a pas pu être générée dans le style de la chaîne. "
                    "Réessaie dans quelques minutes, ou régénère-la manuellement."
                )
                logger.warning(f"Post-render thumbnail retry failed for video {video.id}, leaving no thumbnail: {exc}")
            db.commit()

        # Only now, after every step that reads output_mp4 from local disk
        # (SD-variant pregeneration, the thumbnail frame-grab fallback above)
        # — uploading to B2 and deleting the local copy any earlier left both
        # of those failing with "No such file or directory" against a file
        # that had already been moved (Sept 2026 B2 migration regression).
        _finalize_output_storage(db, video, output_mp4)
        db.commit()

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

    except VideoCancelledError:
        # Not a failure — the creator stopped it on purpose (see
        # POST /{video_id}/cancel). status is already "cancelled", set by
        # that request; leave it as-is instead of overwriting it below with
        # "failed". Still refunds whatever credits this attempt already
        # spent, same courtesy as a genuine failure.
        if video:
            logger.info(f"Video {video.id} rendering stopped: cancelled by the creator.")
            try:
                db.refresh(video)
                if not video.is_reassembly:
                    from src.utils.billing import refund_video_credits
                    refunded = refund_video_credits(db, video.id, f"Remboursement — vidéo annulée ({video.title or video.id})")
                    if refunded:
                        logger.info(f"Refunded {refunded} credits for cancelled video {video.id}.")
            except Exception as cancel_err:
                logger.error(f"Failed to finalize cancellation for video {video.id}: {cancel_err}")
        return False

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
    meta = youtube_metadata.generate_metadata(video, channel, reuse_existing=True)
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
    #
    # Deliberately NOT output_mp4.with_name("thumbnail.jpg"): for a video
    # whose render was moved to B2, output_mp4 here is a throwaway temp
    # download (see _publish_video_background) materialized just for this
    # upload — a sibling "thumbnail.jpg" in that temp dir never exists, which
    # silently fell through to generating a brand-new AI thumbnail on every
    # publish, ignoring whatever the creator actually saw (and possibly
    # regenerated) on the video card. thumbnail.jpg always lives locally next
    # to the video's real storage folder regardless of where output.mp4 itself
    # ended up — same convention _ensure_local_thumbnail uses.
    existing_thumbnail = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id) / "thumbnail.jpg"
    thumbnail_path = existing_thumbnail if existing_thumbnail.exists() else None
    if not thumbnail_path:
        try:
            video.progress_stage = "Génération de la miniature"
            db.commit()
            thumbnail_path, _ = youtube_metadata.generate_thumbnail(
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


def _wait_for_auto_video_turn(db, video: Video) -> bool:
    """Hold automatic script preparation in the same FIFO as rendering.

    Auto videos get a durable queued row as soon as the creator launches
    them. The script writer may only start once every older non-final video
    has finished, unless an administrator explicitly gave this row a higher
    priority. This avoids a newer video looking active while an older one is
    waiting, and makes the full workflow (topic, script and render) FIFO.
    """
    while True:
        db.refresh(video)
        if video.status in (VideoStatus.DONE.value, VideoStatus.FAILED.value):
            return False
        current_priority = int(video.admin_priority or 0)
        older_position = or_(
            Video.created_at < video.created_at,
            and_(Video.created_at == video.created_at, Video.id < video.id),
        )
        earlier_work = (
            db.query(Video.id)
            .filter(
                Video.id != video.id,
                Video.status.in_([VideoStatus.QUEUED.value, VideoStatus.RENDERING.value]),
                or_(
                    Video.admin_priority > current_priority,
                    and_(Video.admin_priority == current_priority, older_position),
                ),
            )
            .first()
        )
        if not earlier_work:
            return True
        video.status = VideoStatus.QUEUED.value
        video.progress_stage = "En attente de la vidéo précédente"
        video.progress_percent = 0
        db.commit()
        time.sleep(2)
        db.expire_all()

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
        restart_script_ids = []
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
                # An automatic video interrupted during topic/script writing
                # has no script yet. It is intentionally excluded from the
                # render picker, so restart its writer after this transaction.
                if video.creation_source == "automatic" and not (video.script_text or "").strip():
                    restart_script_ids.append(video.id)
        if orphaned:
            db.commit()

        # A second, separate kind of orphan: an automatic video can also be
        # interrupted while STILL 'queued' — either in _wait_for_auto_video_turn's
        # FIFO wait (generate_and_queue_auto_video/retry_auto_video_script_background
        # both set status back to 'queued' on every loop tick while waiting for an
        # older video ahead of it) or in the brief window between the row being
        # inserted and that wait starting. The render picker permanently ignores
        # an automatic 'queued' row with no script_text (see ready_for_render
        # above), and nothing else was ever going to come back and restart its
        # writer — this is exactly the "ghost video, no title, never launches"
        # bug: ​a row stuck here forever, silently, with only its placeholder
        # title ("<channel> — nouvelle vidéo") to show for it.
        stuck_queued_ids = [
            v.id for v in (
                db.query(Video)
                .join(Channel, Video.channel_id == Channel.id)
                .filter(
                    Video.status == VideoStatus.QUEUED.value,
                    Video.creation_source == "automatic",
                    or_(Video.script_text.is_(None), Video.script_text == ""),
                    Channel.content_type != "music",
                )
                .all()
            )
            if v.id not in restart_script_ids
        ]
        for video_id in stuck_queued_ids:
            logger.warning(f"Re-queuing stranded queued video {video_id} (interrupted before its script writer ever started).")
        for video_id in restart_script_ids + stuck_queued_ids:
            threading.Thread(target=retry_auto_video_script_background, args=(video_id,), daemon=True).start()
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
    just-finished render. Since the Sept 2026 move to Backblaze B2 (~1/5 the
    cost of R2, no meaningful free-tier cap to ration), EVERY finished render
    uploads to B2 by default — not just extended_retention ones — freeing the
    VPS's own small shared disk immediately instead of only after a 48h purge
    window (that destructive auto-delete policy is gone; see
    purge_old_videos_and_uploads). Falls back to the local
    STORAGE_PATH-relative path only when B2 isn't configured or the upload
    itself failed — never lose a render because B2 had a bad moment. Local
    file is only deleted after a confirmed-successful B2 upload."""
    from src.utils import b2_storage

    try:
        size_bytes = output_mp4.stat().st_size
    except OSError:
        size_bytes = None

    # The whole B2 attempt is best-effort: an unexpected exception here (not
    # just upload_video returning falsy — a real bug once left a video
    # "done" with output_path permanently None, since this function used to
    # abort entirely instead of falling through to the local path below) must
    # never prevent the local-path fallback from being set.
    try:
        if size_bytes and b2_storage.should_upload_to_b2(db, size_bytes):
            channel_name = video.channel.name if video.channel else None
            channel_slug = b2_storage.slugify(channel_name)
            channel_short = b2_storage.short_id(video.channel_id)
            video_short = b2_storage.short_id(video.id)
            object_key = f"channels/{channel_short}-{channel_slug}/videos/{video_short}-{video.id}/output.mp4"
            url = b2_storage.upload_video(output_mp4, object_key)
            if url:
                video.output_path = url
                video.storage_backend = "b2"
                video.output_size_bytes = size_bytes
                try:
                    output_mp4.unlink()
                except OSError:
                    pass
                return
    except Exception as exc:
        logger.warning(f"B2 upload attempt failed for video {video.id}, falling back to local storage: {exc}")

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
    opt-in needed. Only the remote-hosted output.mp4 (storage_backend "b2",
    or legacy "r2" for videos uploaded before the Sept 2026 migration) is
    still actually deleted from the object store itself, since that's a paid
    resource with its own separate lifecycle, not local disk this trash
    folder is meant to declutter. Called from purge_old_videos_and_uploads'
    reinstated VIDEO_RETENTION_HOURS sweep — the caller is responsible for
    setting video.purged_at afterward, this function only moves/deletes the
    files themselves."""
    if video.storage_backend == "b2" and video.output_path:
        from src.utils import b2_storage
        object_key = b2_storage.object_key_from_url(video.output_path)
        if object_key:
            b2_storage.delete_video(object_key)
    elif video.storage_backend == "r2" and video.output_path:
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


def _edit_assets_b2_prefix(video: Video) -> str:
    from src.utils import b2_storage
    channel_name = video.channel.name if video.channel else None
    channel_slug = b2_storage.slugify(channel_name)
    channel_short = b2_storage.short_id(video.channel_id)
    video_short = b2_storage.short_id(video.id)
    return f"channels/{channel_short}-{channel_slug}/videos/{video_short}-{video.id}/edit_assets"


def purge_edit_assets(video: Video) -> None:
    """Archives then deletes the heavy scene images/clips kept for the
    post-render editor, without touching output.mp4 or the small source
    files (voiceover, transcript, subtitles) — the video stays
    downloadable/watchable, just no longer editable in-place until
    restore_edit_assets brings it back (see below). Archived to B2 first
    (never straight-deleted): scenes.json is archived alongside the
    image/clip/audio directories (not just deleted) specifically so a
    restore has the manifest to go with the assets it references, not just
    orphaned files with nothing tying them back into scenes."""
    from src.utils import b2_storage
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    prefix = _edit_assets_b2_prefix(video)
    for sub in ("source/images", "source/clips", "source/audio_segments"):
        p = video_dir / sub
        if not p.exists():
            continue
        archived = b2_storage.archive_directory(p, f"{prefix}/{sub.split('/')[-1]}")
        if archived or not b2_storage.is_b2_configured():
            # Delete locally once safely archived — or, if B2 isn't even
            # configured, fall back to the old behavior (straight delete)
            # rather than accumulating disk forever with archival unavailable.
            shutil.rmtree(p, ignore_errors=True)
        else:
            logger.warning(f"Skipped local delete of {p} for video {video.id} — B2 archive failed, keeping local copy.")
    scenes_manifest = video_dir / "source" / "scenes.json"
    if scenes_manifest.exists():
        manifest_archived = b2_storage.upload_file(scenes_manifest, f"{prefix}/scenes.json")
        if manifest_archived or not b2_storage.is_b2_configured():
            scenes_manifest.unlink(missing_ok=True)
        else:
            logger.warning(f"Skipped deleting scenes.json for video {video.id} — B2 archive failed, keeping local copy.")


def restore_edit_assets(video: Video) -> bool:
    """Counterpart to purge_edit_assets: brings the archived scene
    images/clips/audio segments and scenes.json manifest back to local disk
    so a creator can reopen the editor on a video whose assets were already
    purged. Returns False if nothing was archived for this video (B2 wasn't
    configured at purge time, or the video predates edit-asset archiving
    entirely) — that case is genuinely unrecoverable, not a bug to retry."""
    from src.utils import b2_storage
    video_dir = STORAGE_PATH / "channels" / str(video.channel_id) / "videos" / str(video.id)
    prefix = _edit_assets_b2_prefix(video)
    source_dir = video_dir / "source"
    manifest_ok = b2_storage.download_file(f"{prefix}/scenes.json", source_dir / "scenes.json")
    if not manifest_ok:
        return False
    any_assets = False
    for sub in ("images", "clips", "audio_segments"):
        if b2_storage.restore_directory(f"{prefix}/{sub}", source_dir / sub):
            any_assets = True
    return any_assets


EDIT_ASSETS_RESTORE_GRACE_DAYS = 3


def purge_stale_edit_assets():
    """Background sweep for the EDIT_ASSETS_RETENTION_DAYS window — most users
    trigger this earlier via the explicit 'close editor' action instead. Also
    catches videos whose assets were brought back via restore_edit_assets but
    then left untouched again for EDIT_ASSETS_RESTORE_GRACE_DAYS — restoring
    on demand must not turn into permanent local storage, or we're back to
    the unbounded-disk-growth problem this whole purge system exists to
    avoid."""
    db = SessionLocal()
    try:
        purge_cutoff = datetime.utcnow() - timedelta(days=EDIT_ASSETS_RETENTION_DAYS)
        restore_cutoff = datetime.utcnow() - timedelta(days=EDIT_ASSETS_RESTORE_GRACE_DAYS)
        never_purged = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.edit_assets_purged_at.is_(None))
            .filter(Video.edit_assets_restored_at.is_(None))
            .filter(Video.finished_at.isnot(None))
            .filter(Video.finished_at < purge_cutoff)
            .all()
        )
        restored_and_stale = (
            db.query(Video)
            .filter(Video.status == VideoStatus.DONE.value)
            .filter(Video.edit_assets_restored_at.isnot(None))
            .filter(Video.edit_assets_restored_at < restore_cutoff)
            .all()
        )
        stale = never_purged + restored_and_stale
        for video in stale:
            try:
                purge_edit_assets(video)
                video.edit_assets_purged_at = datetime.utcnow()
                video.edit_assets_restored_at = None
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


# Videos finished before this date are grandfathered — exempt from the
# reinstated 30-day auto-purge below, permanently. Without this, flipping
# the purge back on would immediately sweep up years of existing videos
# nobody ever paid to extend, since none of them have retention_until set
# under a policy that didn't exist yet when they finished. Only videos
# finished from here on are subject to the new clock.
POLICY_REINSTATED_AT = datetime(2026, 9, 5)


def purge_old_videos_and_uploads():
    """
    REINSTATED Sept 2026: a finished video gets VIDEO_RETENTION_HOURS (30
    days) free on B2 (see _finalize_output_storage), then this archives it
    to storage/trash/ (purge_old_render_output — recoverable server-side,
    not destroyed) unless extended_retention/retention_until (see
    RETENTION_TIERS, videos.py) pushes it further out. This had been
    dropped for a while once every render started moving to B2 automatically
    (no more VPS-disk pressure to justify it) — but B2 storage isn't free,
    and an inactive video's cost shouldn't go permanently unpaid forever.
    POLICY_REINSTATED_AT grandfathers every video finished before this
    shipped, so turning the clock back on doesn't retroactively delete a
    creator's existing library.
    Also still deletes uploaded source audio files older than
    UPLOAD_RETENTION_HOURS — temp working storage, only ever needed once, at
    render time, never a user's finished output.
    """
    try:
        db = SessionLocal()
        try:
            purge_cutoff = datetime.utcnow() - timedelta(hours=VIDEO_RETENTION_HOURS)
            expired_videos = (
                db.query(Video)
                .filter(Video.status == VideoStatus.DONE.value)
                .filter(Video.purged_at.is_(None))
                .filter(Video.output_path.isnot(None))
                .filter(Video.finished_at.isnot(None))
                .filter(Video.finished_at >= POLICY_REINSTATED_AT)
                .filter(Video.finished_at <= purge_cutoff)
                .filter(or_(Video.extended_retention.is_(False), Video.retention_until <= datetime.utcnow()))
                .all()
            )
            for video in expired_videos:
                try:
                    purge_old_render_output(video)
                    video.purged_at = datetime.utcnow()
                except Exception as purge_err:
                    logger.warning(f"Failed to purge video {video.id}: {purge_err}")
            if expired_videos:
                db.commit()
                logger.info(f"Purged {len(expired_videos)} video(s) past their {VIDEO_RETENTION_HOURS // 24}-day retention window.")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Video purge pass failed: {e}")

    try:
        uploads_dir = STORAGE_PATH / "uploads"
        if uploads_dir.exists():
            upload_cutoff = time.time() - (UPLOAD_RETENTION_HOURS * 3600)
            for f in uploads_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < upload_cutoff:
                    f.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Storage purge pass failed: {e}")


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
        # The row is created only after this preflight. Without a visible
        # failure card, an on-demand launch looked like it started and then
        # vanished because the frontend's optimistic placeholder had nothing
        # real to replace it with.
        detail = getattr(exc, "detail", None) or "La source visuelle de cette chaîne n'est pas prête."
        logger.warning(f"Daily automation: channel {channel.id} ('{channel.name}') has no usable visual source; skipping. ({detail})")
        _record_automation_failure(db, channel, message=str(detail))
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
        # Placeholder until generate_daily_script returns the real one below
        # — without it the row shows "(sans titre)" in "Mes Vidéos"/admin for
        # the whole topic-research + script-writing window, which reads as
        # broken even though nothing has failed.
        title=f"{channel.name or channel.niche} — nouvelle vidéo",
        # A durable FIFO position is recorded before any AI work starts.
        # The worker ignores auto rows with an empty script; this background
        # writer claims the row only when every older video has finished.
        status=VideoStatus.QUEUED.value,
        progress_stage="En attente dans la file",
        progress_percent=0,
        transcribe_audio=channel.transcribe_audio_default if channel.transcribe_audio_default is not None else True,
        voice_id=getattr(channel, "voice_id", None),
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    if not _wait_for_auto_video_turn(db, video):
        return None
    video.status = VideoStatus.RENDERING.value
    video.progress_stage = "Recherche du sujet"
    video.progress_percent = 1
    db.commit()

    # A video may have waited minutes before its turn. Refresh its recent
    # history now so the topic generator also avoids the titles produced by
    # those earlier videos while it was waiting.
    recent_video_history = (
        db.query(Video)
        .filter(Video.channel_id == channel.id, Video.id != video.id)
        .order_by(Video.created_at.desc())
        .limit(AUTOMATION_RECENT_TITLES_LIMIT)
        .all()
    )
    recent_titles = [v.title or (v.script_text or "").split("\n")[0][:120] for v in recent_video_history]
    recent_scripts = [v.script_text for v in recent_video_history if (v.script_text or "").strip()]

    def _on_script_progress(stage: str, percent: int) -> None:
        video.progress_stage = stage
        video.progress_percent = percent
        db.commit()

    def _on_title_picked(title: str) -> None:
        # Saved the moment the topic is chosen, well before the script
        # itself is written, so "Mes Vidéos" shows the real title instead of
        # a generic placeholder for the entire (often multi-minute) writing
        # phase that follows.
        video.title = title
        db.commit()

    def _on_partial_script(text: str, completed_parts: int, total_parts: int) -> None:
        video.script_text = text
        video.progress_stage = f"Rédaction du script · partie {completed_parts}/{total_parts}"
        video.progress_percent = 8 + round(16 * completed_parts / max(1, total_parts))
        db.commit()

    result = generate_daily_script(
        niche=channel.niche,
        recent_titles=recent_titles,
        style_prompt=combined_style_prompt,
        script_structure=channel.script_structure,
        default_language="French" if (owner.locale or "fr") == "fr" else "English",
        topic_examples=channel.topic_examples,
        use_web_trends=bool(channel.use_web_trends),
        youtube_topic_sources=channel.youtube_topic_sources,
        on_progress=_on_script_progress,
        recent_scripts=recent_scripts,
        on_title=_on_title_picked,
        on_partial_script=_on_partial_script,
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


def _music_video_duration_seconds(music_config: dict) -> float:
    """Choose a stable, slightly non-round target for one music render."""
    configured_duration_seconds = float(
        music_config.get("target_duration_seconds")
        or float(music_config.get("target_duration_minutes") or 10) * 60.0
    )
    variation_seconds = min(15, max(3, round(configured_duration_seconds * 0.017)))
    offsets = [offset for offset in range(-variation_seconds, variation_seconds + 1) if offset]
    return max(30.0, configured_duration_seconds + random.choice(offsets))


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
    music_source_mode = music_config.get("music_source_mode") or "ai_generate"
    # A style description drives both the AI-generated track and the AI
    # background images — required when generating either. In "library"
    # mode (own uploaded tracks), it's optional: pick_music_video_title
    # below already has its own generic fallback when it's blank.
    if not style_prompt and music_source_mode != "library":
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
    # Exact 10:00 / 20:00 outputs look mechanically generated. Keep the
    # creator's selected duration as the centre, with a bounded natural
    # variation (10:00 becomes e.g. 09:51 or 10:07).
    target_duration_seconds = _music_video_duration_seconds(music_config)
    # Imported music has no generation cost. Izivoice needs the conservative
    # reserve because a compilation can contain several generated tracks.
    if music_source_mode == "ai_generate":
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
        estimated_duration_seconds=target_duration_seconds,
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

        # Script retries obey exactly the same durable FIFO order as a new
        # automatic launch. The render picker ignores this empty-script row
        # until the writer has claimed its turn below.
        if not _wait_for_auto_video_turn(db, video):
            return
        video.status = VideoStatus.RENDERING.value
        video.progress_stage = "Régénération du script…"
        video.progress_percent = 1
        db.commit()

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

        def _on_title_picked(title: str) -> None:
            video.title = title
            db.commit()

        def _on_partial_script(text: str, completed_parts: int, total_parts: int) -> None:
            video.script_text = text
            video.progress_stage = f"Rédaction du script · partie {completed_parts}/{total_parts}"
            video.progress_percent = 8 + round(16 * completed_parts / max(1, total_parts))
            db.commit()

        result = generate_daily_script(
            niche=channel.niche,
            recent_titles=recent_titles,
            style_prompt=combined_style_prompt,
            script_structure=channel.script_structure,
            default_language="French" if (owner.locale or "fr") == "fr" else "English",
            topic_examples=channel.topic_examples,
            use_web_trends=bool(channel.use_web_trends),
            youtube_topic_sources=channel.youtube_topic_sources,
            recent_scripts=recent_scripts,
            on_title=_on_title_picked,
            on_partial_script=_on_partial_script,
            preset_title=(video.title or "").strip() if not (video.title or "").endswith(" — nouvelle vidéo") else None,
        )
        if not result:
            video.status = VideoStatus.FAILED.value
            video.script_text = ""
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


COMMUNITY_IMAGE_TAGGING_BATCH_SIZE = 5
COMMUNITY_IMAGE_TAGGING_INTERVAL_SECONDS = 120  # a few images every 2 minutes — steady catch-up, no burst


def tag_untagged_community_images():
    """Runs a vision model over a small batch of shared-library images that
    have never been tagged, and stores the resulting keywords.

    Public-library search only ever matched a source channel's NICHE — a
    search for "chessboard" found nothing unless the sharing channel's niche
    happened to contain that word, however clearly a chessboard was actually
    in the picture. Tagging happens here, in the background, a handful of
    images at a time, rather than on the search request itself: a vision call
    is too slow/rate-limited to sit in a search's critical path, and this way
    a burst of new shares doesn't spike search latency for everyone.

    Every folder that has ever been shared is tagged here regardless of its
    curation status (pending/approved/flagged) — search itself still only
    reads from approved folders (see search_public_library), but classifying
    only on approval meant a folder's very first search after being approved
    found nothing until the tagging pass caught up. Getting there ahead of
    approval means the catalogue is already searchable the moment an admin
    flips the switch.

    Also classifies each image's actual niche (independent of the sharing
    channel's own niche label) in the same pass, and auto-inserts a
    CommunityLibraryImagePlacement row when it disagrees with the channel's
    declared niche — see classify_image_niche in vision.py. This is what
    lets the shared pools grow correctly even from channels whose library
    isn't perfectly on-topic for their own niche.
    """
    from src.config import STORAGE_PATH
    from src.db.models import CommunityLibraryFolder, CommunityLibraryImagePlacement, CommunityLibraryImageTag
    from src.api.routes.channels import ALLOWED_LIBRARY_EXTENSIONS
    from src.api.routes.admin import KNOWN_NICHES
    from src.pipeline.vision import analyze_image_content_tags, classify_image_niche
    import mimetypes

    db = SessionLocal()
    try:
        folders = db.query(CommunityLibraryFolder).all()
        already_tagged = {
            (row.channel_id, row.filename)
            for row in db.query(CommunityLibraryImageTag.channel_id, CommunityLibraryImageTag.filename).all()
        }
        existing_placements = {
            (row.channel_id, row.filename)
            for row in db.query(CommunityLibraryImagePlacement.channel_id, CommunityLibraryImagePlacement.filename).all()
        }
        tagged_this_pass = 0
        for folder in folders:
            if tagged_this_pass >= COMMUNITY_IMAGE_TAGGING_BATCH_SIZE:
                break
            library_dir = STORAGE_PATH / "channels" / folder.channel_id / "library"
            if not library_dir.is_dir():
                continue
            for asset in sorted(library_dir.iterdir(), key=lambda item: item.name):
                if tagged_this_pass >= COMMUNITY_IMAGE_TAGGING_BATCH_SIZE:
                    break
                if not asset.is_file() or asset.suffix.lower() not in ALLOWED_LIBRARY_EXTENSIONS:
                    continue
                if (folder.channel_id, asset.name) in already_tagged:
                    continue
                media_type = mimetypes.guess_type(asset.name)[0] or "image/jpeg"
                image_bytes = asset.read_bytes()
                try:
                    tags = analyze_image_content_tags(image_bytes, media_type)
                except Exception as exc:
                    logger.warning(f"Content tagging failed for {folder.channel_id}/{asset.name}: {exc}")
                    tags = []
                # Auto niche classification: an image's actual content
                # sometimes belongs to a different niche than the channel
                # that shared it, so it should still feed THAT niche's pool
                # rather than sitting invisible to every search/render that
                # doesn't happen to match the sharing channel's own label.
                # Never touches a row an admin already set by hand.
                detected_niche = None
                try:
                    detected_niche = classify_image_niche(image_bytes, media_type, KNOWN_NICHES)
                except Exception as exc:
                    logger.warning(f"Niche classification failed for {folder.channel_id}/{asset.name}: {exc}")
                if (
                    detected_niche
                    and detected_niche.casefold() != (folder.niche or "").casefold()
                    and (folder.channel_id, asset.name) not in existing_placements
                ):
                    db.add(CommunityLibraryImagePlacement(
                        channel_id=folder.channel_id,
                        filename=asset.name,
                        niche=detected_niche,
                    ))
                    existing_placements.add((folder.channel_id, asset.name))
                db.add(CommunityLibraryImageTag(
                    channel_id=folder.channel_id,
                    filename=asset.name,
                    tags_json=json.dumps(tags),
                    detected_niche=detected_niche,
                ))
                db.commit()
                already_tagged.add((folder.channel_id, asset.name))
                tagged_this_pass += 1
        if tagged_this_pass:
            logger.info(f"Tagged and niche-classified {tagged_this_pass} community library image(s).")
    except Exception as exc:
        logger.error(f"tag_untagged_community_images failed: {exc}")
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
        channels = db.query(Channel).filter(
            Channel.automation_mode == "auto",
            Channel.is_active.is_(True),
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

            # Reserve the slot BEFORE generating, with a conditional UPDATE
            # keyed on the value just read (optimistic lock), not after —
            # generate_and_queue_auto_video can run for minutes (a
            # multi-part script is several sequential Claude calls), and the
            # old and new worker containers briefly overlap during every
            # deploy (Coolify starts the replacement before killing the
            # original). Both processes used to read the same stale
            # `already` count, both pass the `already >= quota` check, and
            # both generate — this channel got a 2nd unwanted video the same
            # day this way more than once. Only the process whose UPDATE
            # actually matches a row (rowcount > 0) may proceed; the loser
            # sees rowcount 0 and skips, exactly as if it were over quota.
            reserved = db.query(Channel).filter(
                Channel.id == channel.id,
                Channel.auto_videos_generated_today == already,
            ).update({"auto_videos_generated_today": already + 1})
            db.commit()
            if not reserved:
                continue

            try:
                video = generate_and_queue_music_video(db, channel) if channel.content_type == "music" else generate_and_queue_auto_video(db, channel)
            except Exception as exc:
                # An unhandled exception here (e.g. a DB session error mid
                # script-write) used to propagate straight out of this
                # for-loop and abort run_daily_automation entirely — not just
                # for this channel, for every channel still left in this
                # sweep's list too. Rolling back and continuing keeps one
                # channel's bad luck from stalling the rest. The empty-script
                # row this attempt already committed (see generate_and_queue_
                # auto_video's early db.add/db.commit) is also cleaned up
                # here so it doesn't sit forever as a queued ghost the render
                # worker will never touch (see ready_for_render) — this is
                # exactly what left 7 channels' auto videos stuck for up to
                # 19h on 2026-09-04 before this fix.
                logger.warning(f"Daily automation: exception generating video for channel {channel.id} ('{channel.name}'): {exc}")
                db.rollback()
                if channel.content_type != "music":
                    db.query(Video).filter(
                        Video.channel_id == channel.id,
                        Video.status == VideoStatus.QUEUED.value,
                        or_(Video.script_text.is_(None), Video.script_text == ""),
                    ).update(
                        {"status": VideoStatus.FAILED.value, "error_message": "Génération automatique interrompue par une erreur inattendue."},
                        synchronize_session=False,
                    )
                db.commit()
                video = None
            if not video:
                # Generation failed (or was legitimately skipped, e.g.
                # insufficient credit) — release the slot so this is
                # retried on the next check within today's window instead
                # of silently burning it on a transient failure.
                db.query(Channel).filter(Channel.id == channel.id).update(
                    {"auto_videos_generated_today": already}
                )
                db.commit()
                logger.warning(f"Daily automation: script generation failed for channel {channel.id} ('{channel.name}'); will retry.")
                continue

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
    last_community_tagging = 0.0
    while not _shutdown_requested:
        now = time.time()
        if now - last_purge > PURGE_INTERVAL_SECONDS:
            # REINSTATED Sept 2026 alongside VIDEO_RETENTION_HOURS above —
            # warn first so a creator has VIDEO_EXPIRY_WARNING_HOURS_BEFORE
            # to extend before purge_old_videos_and_uploads sweeps it up.
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
        if now - last_community_tagging > COMMUNITY_IMAGE_TAGGING_INTERVAL_SECONDS:
            tag_untagged_community_images()
            last_community_tagging = now
        time.sleep(poll_interval_seconds)

    # SIGTERM landed — let every in-flight render lane finish its current
    # video before the process actually exits.
    for lane in lanes:
        lane.join()

if __name__ == "__main__":
    start_queue_worker()
