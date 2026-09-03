import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Float, Integer, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship, backref
from src.db.session import Base
from src.models.project import VideoStatus

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False, default="Créateur KappGen")
    hashed_password = Column(String(255), nullable=False)
    picture_url = Column(String(1024), nullable=True)
    phone = Column(String(50), nullable=True)
    auth_provider = Column(String(50), nullable=False, default="password")  # "password" | "google"
    locale = Column(String(5), nullable=False, default="fr")  # "fr" | "en" — detected from Accept-Language at signup
    izivoice_api_key_encrypted = Column(Text, nullable=True)
    izivoice_key_prefix = Column(String(20), nullable=True)
    izivoice_connected_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    is_admin = Column(Boolean, nullable=False, default=False)
    email_verified = Column(Boolean, nullable=False, default=False)
    email_verify_token = Column(String(64), nullable=True)
    email_verify_sent_at = Column(DateTime, nullable=True)
    # Legacy columns retained for schema compatibility only. Free access is
    # exclusively the one-time 10,000-credit welcome pot.
    free_video_quota_granted = Column(Integer, nullable=False, default=0)
    free_videos_used = Column(Integer, nullable=False, default=0)

    # Relationships
    channels = relationship("Channel", back_populates="user", cascade="all, delete-orphan")
    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", foreign_keys="Subscription.user_id", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "picture_url": self.picture_url,
            "phone": self.phone,
            "auth_provider": self.auth_provider,
            "locale": self.locale,
            "izivoice_connected": bool(self.izivoice_api_key_encrypted),
            "izivoice_key_prefix": self.izivoice_key_prefix,
            "izivoice_connected_at": self.izivoice_connected_at.isoformat() if self.izivoice_connected_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "channel_count": len(self.channels) if self.channels else 0,
            "is_admin": self.is_admin,
            "email_verified": self.email_verified,
            "free_video_quota_granted": self.free_video_quota_granted,
            "free_videos_used": self.free_videos_used,
        }


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    code_hash = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False, default="Clé API")
    key_prefix = Column(String(16), nullable=False)  # shown in UI, e.g. "nck_ab12"
    hashed_key = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    revoked = Column(String(5), nullable=False, default="false")

    user = relationship("User", back_populates="api_keys")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked": self.revoked == "true"
        }

class Channel(Base):
    __tablename__ = "channels"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    name = Column(String(255), nullable=False)
    # Creator-provided (or YouTube-synced) summary of what the channel is about —
    # feeds automatic niche detection and gives the auto-generation agent context
    # beyond just the name/niche label.
    description = Column(Text, nullable=True)
    niche = Column(String(255), nullable=False, default="General")
    # "narration" (default — the existing script+voiceover+scene-images
    # pipeline) or "music": the channel's content IS the music itself (AI-
    # generated tracks, no script/voiceover at all), assembled with a much
    # lighter pipeline — see MusicChannel product plan: style/example-titles
    # config lives in music_channel_config, the render is a loop-or-
    # compilation of tracks behind 0-N images + an audio-spectrum visual.
    # Deliberately its own column rather than overloading `niche` or
    # automation fields, since it changes which wizard steps and which
    # worker pipeline apply, not just a content-style knob within one pipeline.
    content_type = Column(String(20), nullable=False, default="narration")
    # Only meaningful when content_type == "music". {"style_prompt": str,
    # "reference_track_path": str|null, "title_examples": str (one per line,
    # same free-text pattern as topic_examples), "edit_mode": "loop"|"compilation",
    # "image_count": int (0-N — creator's choice, no fixed montage complexity),
    # "target_duration_minutes": int}
    music_channel_config = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Pause switch: a creator who wants to stop production without losing the
    # channel's config/history/videos (unlike deleting it) sets this False.
    # Blocks manual submissions, on-demand "Nouvelle vidéo", and the daily
    # automation pass alike — see is_active checks in videos.py/channels.py
    # and the daily automation loop in queue_runner.py.
    is_active = Column(Boolean, nullable=False, default=True)

    # Whether auto-generated videos (automation_mode "auto", which has no
    # per-video submission form to ask) get real Izivoice speech-to-text for
    # subtitle timing (accurate, billable) or the free synthetic fallback
    # (approximate, evenly spread over the audio's duration). Manual
    # submissions get their own per-video choice instead (Video.transcribe_audio);
    # this is only the default new auto-mode videos are queued with.
    transcribe_audio_default = Column(Boolean, nullable=False, default=True)

    # JSON configurations
    subtitle_style = Column(JSON, nullable=False, default=dict)
    branding = Column(JSON, nullable=False, default=dict)
    music_preference = Column(JSON, nullable=False, default=dict)
    image_style = Column(JSON, nullable=False, default=dict)
    effects_config = Column(JSON, nullable=False, default=dict)
    # Optional, separate from image_style: a reference image + derived style_prompt
    # used only for the YouTube thumbnail background, since creators often want a
    # thumbnail look (e.g. a specific character/composition) distinct from the
    # video's own body-image style. {"reference_image_path": str, "style_prompt": str}
    thumbnail_style = Column(JSON, nullable=True)

    # Full-auto daily pipeline: "manual" (default, user submits each video)
    # or "auto" (Claude picks a fresh topic + writes the script itself once a
    # day, at a randomized time in the configured window, no human input).
    automation_mode = Column(String(20), nullable=False, default="manual")
    automation_style_prompt = Column(Text, nullable=True)  # optional extra creative direction for Claude
    # Example topics/titles the creator considers "on-brand" for this channel —
    # either their own best-performing past videos, or titles copied from a
    # channel they want to emulate. Without this, topic selection was pure
    # freestyle (niche label + "don't repeat these old titles"), which reads
    # as random/generic rather than matching a specific angle or style. Free
    # text, one topic/title per line; see script_writer._pick_topic.
    topic_examples = Column(Text, nullable=True)
    # When set, topic selection is allowed to use Claude's live web-search
    # tool to ground new topics in actual recent events/trends instead of
    # inventing from the model's training data alone — meant for news/current-
    # events-style channels where "what's happening right now" is the point.
    use_web_trends = Column(Boolean, nullable=False, default=False)
    # How many videos the daily pipeline generates per day (automation_mode
    # "auto" only) — each gets its own randomized slot inside the window,
    # spread evenly so they don't all land back-to-back.
    videos_per_day = Column(Integer, nullable=False, default=1)
    # Daily window (in the channel's own timezone) the auto-mode agent is
    # allowed to generate/publish within — every creator picks their own,
    # instead of a single window imposed on everyone.
    automation_window_start_hour = Column(Integer, nullable=False, default=7)
    automation_window_end_hour = Column(Integer, nullable=False, default=11)
    # Which weekdays the daily pipeline is allowed to run on — a list of ints
    # 0=Monday..6=Sunday. Null/empty means every day. Lets creators who post
    # once a week, three times a week, weekdays only, etc. use full automation
    # too, instead of it only supporting "N videos every single day".
    active_days = Column(JSON, nullable=True)
    # Local hour (0-23) the daily pipeline may start WRITING the script for
    # this channel — lets a creator pin generation to a known time (e.g. to
    # verify automation actually fires) instead of it happening as soon as
    # the worker gets to it. Null = as soon as possible, no gating (default).
    script_generation_hour = Column(Integer, nullable=True)
    # Minute (0-59) within script_generation_hour — lets a creator pin the
    # trigger to an exact minute, not just the hour, when testing whether
    # automation fires. Ignored while script_generation_hour is null.
    script_generation_minute = Column(Integer, nullable=False, default=0)
    # Second (0-59) within script_generation_minute — stored for display
    # fidelity (the wizard's HH:MM:SS field), but NOT enforced by the worker:
    # the daily automation check only runs every ~10 min, so a specific
    # second isn't something it can actually honor.
    script_generation_second = Column(Integer, nullable=False, default=0)
    # Which weekdays script generation itself is allowed to run — separate
    # from active_days above, which only gates the publish schedule. A list
    # of ints 0=Monday..6=Sunday; null/empty means every day.
    script_generation_days = Column(JSON, nullable=True)
    last_auto_run_date = Column(String(10), nullable=True)  # "YYYY-MM-DD" in the channel's automation timezone
    # How many auto videos have already been generated on last_auto_run_date —
    # reset to 0 whenever the local date rolls over. Lets videos_per_day > 1
    # work without a timezone-aware DB query to count "today"'s videos.
    auto_videos_generated_today = Column(Integer, nullable=False, default=0)
    # IANA timezone (e.g. "Europe/Paris") used to compute the channel's local
    # day/hour for both the daily auto-generation window and the scheduled
    # publish hour below. Auto-detected client-side at channel creation —
    # never hardcoded to one region for every creator.
    timezone = Column(String(64), nullable=False, default="Africa/Douala")
    # Configurable shape of the auto-generated script: language, parts (each
    # with a word count + what it must cover), formatting rules, CTA style.
    # See src/pipeline/script_writer.py DEFAULT_SCRIPT_STRUCTURE for the shape
    # and fallback used when this is null.
    script_structure = Column(JSON, nullable=True)
    voice_id = Column(String(255), nullable=True)
    voice_name = Column(String(255), nullable=True)
    voice_settings = Column(JSON, nullable=True)

    # How a *finished* video actually reaches YouTube — independent of
    # automation_mode (which only governs whether the script/topic itself is
    # picked automatically). Always the creator's choice:
    # "auto": publish immediately once the render finishes.
    # "scheduled": wait for a fixed daily time (publish_schedule_hour, in the
    #   channel's own `timezone`), publish_schedule_day_offset days after the
    #   render finishes.
    # "manual" (default): never auto-publish — the creator downloads and
    #   posts it themselves, or publishes on demand from NicheCut.
    publish_mode = Column(String(20), nullable=False, default="manual")
    youtube_made_for_kids = Column(Boolean, nullable=False, default=False)
    youtube_default_description = Column(Text, nullable=True)
    youtube_default_tags = Column(JSON, nullable=False, default=list)
    youtube_category_id = Column(String(10), nullable=False, default="22")
    youtube_privacy_status = Column(String(20), nullable=False, default="public")
    youtube_contains_synthetic_media = Column(Boolean, nullable=False, default=True)
    youtube_license = Column(String(20), nullable=False, default="youtube")
    youtube_notify_subscribers = Column(Boolean, nullable=False, default=True)
    youtube_embeddable = Column(Boolean, nullable=False, default=True)
    youtube_public_stats_viewable = Column(Boolean, nullable=False, default=True)
    # "fixed": always publish_schedule_hour, exactly. "range" (default): a
    # randomized time inside automation_window_start/end_hour — those columns
    # always have a non-null default (7/11), so without this flag the worker
    # can't tell "fixed" and "range" apart and silently ignores whichever
    # hour the creator actually meant.
    publish_time_mode = Column(String(20), nullable=False, default="range")
    publish_schedule_hour = Column(Integer, nullable=False, default=8)  # 0-23, channel's own timezone
    publish_schedule_day_offset = Column(Integer, nullable=False, default=1)  # 0 = same day, 1 = next day

    # YouTube connection (per channel) — OAuth2 refresh token lets the worker
    # publish auto-generated videos with zero human input. access_token +
    # token_expiry are a short-lived cache; refresh_token is what's actually
    # durable and gets exchanged for a fresh access_token as needed.
    youtube_channel_id = Column(String(64), nullable=True)
    youtube_channel_title = Column(String(255), nullable=True)
    youtube_channel_handle = Column(String(255), nullable=True)  # e.g. "@somechannel"
    youtube_channel_thumbnail_url = Column(String(1024), nullable=True)
    youtube_access_token = Column(Text, nullable=True)
    youtube_refresh_token = Column(Text, nullable=True)
    youtube_token_expiry = Column(DateTime, nullable=True)
    youtube_connected_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="channels")
    videos = relationship("Video", back_populates="channel", cascade="all, delete-orphan")

    def to_dict(self):
        # A channel can be saved (and reopened later to finish configuring)
        # before it's actually ready to render a video — completion tracks
        # the one thing a render genuinely can't proceed without: a visual
        # source with real content behind it. Identity/niche are always set
        # (they default), so they don't gate this. The voice does NOT gate
        # this either — generate_voiceover() auto-picks a default voice_id
        # when the channel hasn't set one (see voiceover.py's
        # _get_default_voice_id), so a channel is fully render-ready as
        # soon as its visuals are, whether or not the creator ever opened
        # the Voix Off step.
        image_style = self.image_style or {}
        from src.pipeline.images import resolve_enabled_image_sources
        enabled_sources = resolve_enabled_image_sources(image_style)
        # style_prompt is optional for AI generation (images.py just skips
        # appending it if blank — never required to actually generate) —
        # requiring it here left plenty of real, working, AI-only channels
        # permanently stuck at "50% configured" even though they were
        # rendering real videos successfully the whole time.
        visuals_ready = bool(
            "ai_generated" in enabled_sources
            or ("library" in enabled_sources and (image_style.get("library_image_count") or 0) > 0)
            # "community" borrows another channel's already-approved library —
            # nothing of its own to check here; validate_channel_visual_source
            # (videos.py) is the real gate confirming the niche actually has
            # an approved folder before a render is allowed to start.
            or "community" in enabled_sources
        )
        completion_percent = 100 if visuals_ready else 50
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "description": self.description,
            "niche": self.niche,
            "content_type": self.content_type or "narration",
            "music_channel_config": self.music_channel_config,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "subtitle_style": self.subtitle_style,
            "transcribe_audio_default": self.transcribe_audio_default if self.transcribe_audio_default is not None else True,
            "branding": self.branding,
            "music_preference": self.music_preference,
            "image_style": self.image_style,
            "thumbnail_style": self.thumbnail_style,
            "effects_config": self.effects_config,
            "completion_percent": completion_percent,
            "is_render_ready": visuals_ready,
            "automation_mode": self.automation_mode or "manual",
            "automation_style_prompt": self.automation_style_prompt,
            "topic_examples": self.topic_examples,
            "use_web_trends": bool(self.use_web_trends),
            "videos_per_day": self.videos_per_day or 1,
            "automation_window_start_hour": self.automation_window_start_hour if self.automation_window_start_hour is not None else 7,
            "automation_window_end_hour": self.automation_window_end_hour if self.automation_window_end_hour is not None else 11,
            "active_days": self.active_days,
            "script_generation_hour": self.script_generation_hour,
            "script_generation_minute": self.script_generation_minute or 0,
            "script_generation_second": self.script_generation_second or 0,
            "script_generation_days": self.script_generation_days,
            "last_auto_run_date": self.last_auto_run_date,
            "timezone": self.timezone or "Africa/Douala",
            "script_structure": self.script_structure,
            "voice_id": self.voice_id,
            "voice_name": self.voice_name,
            "voice_settings": self.voice_settings or {"speed": 0.845, "stability": 0.8, "similarity_boost": 0.9, "style": 0.0},
            "publish_mode": self.publish_mode or "manual",
            "youtube_made_for_kids": bool(self.youtube_made_for_kids),
            "youtube_default_description": self.youtube_default_description,
            "youtube_default_tags": self.youtube_default_tags or [],
            "youtube_category_id": self.youtube_category_id or "22",
            "youtube_privacy_status": self.youtube_privacy_status or "public",
            "youtube_contains_synthetic_media": bool(self.youtube_contains_synthetic_media),
            "youtube_license": self.youtube_license or "youtube",
            "youtube_notify_subscribers": bool(self.youtube_notify_subscribers),
            "youtube_embeddable": bool(self.youtube_embeddable),
            "youtube_public_stats_viewable": bool(self.youtube_public_stats_viewable),
            "publish_time_mode": self.publish_time_mode or "range",
            "publish_schedule_hour": self.publish_schedule_hour,
            "publish_schedule_day_offset": self.publish_schedule_day_offset,
            "youtube_connected": bool(self.youtube_refresh_token),
            "youtube_channel_title": self.youtube_channel_title,
            "youtube_channel_handle": self.youtube_channel_handle,
            "youtube_channel_thumbnail_url": self.youtube_channel_thumbnail_url,
            "video_count": len(self.videos) if self.videos else 0
        }

class Folder(Base):
    __tablename__ = "folders"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    name = Column(String(255), nullable=False)
    # Nested folders, file-explorer style — null means a top-level folder.
    parent_id = Column(String(36), ForeignKey("folders.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    videos = relationship("Video", back_populates="folder")
    children = relationship("Folder", backref=backref("parent", remote_side=[id]))

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "video_count": len(self.videos) if self.videos else 0
        }


class Video(Base):
    __tablename__ = "videos"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36), ForeignKey("channels.id"), nullable=False)
    folder_id = Column(String(36), ForeignKey("folders.id"), nullable=True)
    input_type = Column(String(50), nullable=False, default="text")
    # Keeps the creator-facing production path explicit after the render has
    # finished: generated by KappGen, script supplied, or audio supplied.
    creation_source = Column(String(24), nullable=False, default="script")
    title = Column(String(255), nullable=True)  # set for auto-generated videos; used as the YouTube upload title
    script_text = Column(Text, nullable=True, default="")
    audio_input_path = Column(String(512), nullable=True)
    status = Column(String(50), nullable=False, default=VideoStatus.QUEUED.value)

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    output_path = Column(String(512), nullable=True)
    # "local" (default — output_path is STORAGE_PATH-relative) or "r2"
    # (output_path is a full public URL on Cloudflare R2). See
    # src/utils/r2_storage.py for the hybrid-storage decision; frontend's
    # getVideoUrl() already passes full URLs through unchanged, so nothing
    # else needs to know which backend a given video landed on.
    storage_backend = Column(String(10), nullable=False, default="local")
    output_size_bytes = Column(Integer, nullable=True)  # output.mp4 size — feeds current_r2_usage_bytes()
    # Opt-in per-video: skip the default retention purge entirely and prefer
    # uploading to R2 instead of local disk (see _finalize_output_storage /
    # purge_old_videos_and_uploads, queue_runner.py). Meant to be a paid
    # feature ("garde tes vidéos plus longtemps") — no credit/subscription
    # gate wired up yet, billing comes later; this only sets up the mechanism.
    extended_retention = Column(Boolean, nullable=False, default=False)
    # The first 48h are included. Extra storage is purchased per day and has
    # an explicit expiry; it is never an unlimited free retention flag.
    retention_until = Column(DateTime, nullable=True)
    source_assets_path = Column(String(512), nullable=True)
    error_message = Column(Text, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    estimated_duration_seconds = Column(Float, nullable=True)
    purged_at = Column(DateTime, nullable=True)
    # Set once the "ta vidéo va être supprimée" email has gone out for this
    # video — prevents re-sending it on every purge-loop tick between the
    # warning and the actual deletion (see warn_expiring_videos, queue_runner.py).
    expiry_warning_sent_at = Column(DateTime, nullable=True)
    restart_count = Column(Integer, nullable=False, default=0)
    # Admin override that jumps a queued video ahead of the normal FIFO render
    # order (see queue_runner.py's
    # process_single_queued_video and admin.py's _queued_video_positions,
    # both of which sort by this first). 0 = no override; a higher value
    # wins over a lower one, so bumping several videos still orders them by
    # when each was prioritized (see admin_set_video_priority).
    admin_priority = Column(Integer, nullable=False, default=0)
    progress_stage = Column(String(255), nullable=True)
    progress_percent = Column(Integer, nullable=False, default=0)
    is_reassembly = Column(Boolean, nullable=False, default=False)
    edit_assets_purged_at = Column(DateTime, nullable=True)
    # Only meaningful for input_type="audio": whether to spend Izivoice STT
    # credits transcribing the upload for accurate subtitles, or skip it (free,
    # approximate title-based captions instead). Opt-out toggle in the wizard.
    transcribe_audio = Column(Boolean, nullable=False, default=True)
    # Explicit evidence supplied at upload time. It does not prove ownership,
    # but prevents third-party audio from entering the automated pipeline
    # without the creator acknowledging the rights requirement.
    audio_rights_confirmed = Column(Boolean, nullable=False, default=False)
    audio_source_type = Column(String(32), nullable=True)
    voice_id = Column(String(255), nullable=True)
    # Set alongside is_reassembly=True + status=QUEUED by the Studio editor's
    # scene endpoints; tells the worker which lightweight edit function to run
    # instead of the full pipeline. JSON: {"type": "image"|"subtitle_text"|"audio",
    # "scene_index": int, "text": str|null}. Cleared once the worker picks it up.
    pending_edit = Column(JSON, nullable=True)
    # Stack of reversible Studio edits (most recent last), capped at 20:
    # [{"type": "image"|"subtitle_text"|"logo", ...}]. Popped and applied in
    # reverse by POST /videos/{id}/undo. Deliberately doesn't cover
    # "audio" (voice regen) edits — those re-time the whole video and aren't
    # cheaply reversible the way a local file swap or a branding value is.
    edit_history = Column(JSON, nullable=True)
    # Set once the worker successfully auto-publishes this video to the
    # channel's connected YouTube account (auto-mode channels only).
    youtube_video_id = Column(String(32), nullable=True)
    youtube_published_at = Column(DateTime, nullable=True)
    youtube_publish_error = Column(Text, nullable=True)
    # Set the first time the creator downloads the rendered file (see
    # GET /{video_id}/download) — the retention purge only auto-deletes a
    # video's local file once it's been either downloaded or published to
    # YouTube, so a creator who hasn't gotten to it yet doesn't lose their
    # only copy just because VIDEO_RETENTION_HOURS elapsed.
    downloaded_at = Column(DateTime, nullable=True)
    # AI-proposed YouTube description, generated as soon as the render finishes
    # (alongside `title`, reused for this) so the creator can review/edit both
    # before publishing instead of only seeing them at the moment of upload.
    youtube_description = Column(Text, nullable=True)
    # Short (2-7 word) caption baked into the thumbnail image itself — kept
    # separate from `title` (the full YouTube title) because the title is
    # often too long/verbose to render legibly on a 1280x720 thumbnail.
    thumbnail_text = Column(String(255), nullable=True)
    # Set while a "Régénérer la miniature" request is running in the
    # background (see /videos/{id}/thumbnail/regenerate) — the AI background
    # generation call can take well over a minute, longer than Cloudflare's
    # edge proxy will hold an HTTP request open, so the endpoint starts the
    # job and returns immediately instead of blocking; the frontend polls
    # this flag rather than awaiting one long response.
    thumbnail_regenerating = Column(Boolean, nullable=False, default=False)
    # Bumped every time thumbnail.jpg is (re)written — the actual file is
    # overwritten in place at a fixed path, so nothing about the video row
    # itself (finished_at, output_path, ...) changes when it's regenerated.
    # The frontend used finished_at as its cache-busting query param before
    # this existed, which meant a page refresh right after regenerating
    # reused the exact same image URL as before the regen — the browser (or
    # an intermediate CDN cache) would then keep serving the stale cached
    # thumbnail even though the file on disk was already the new one, which
    # read as "it didn't actually save". This field changes on every
    # regeneration so the URL always changes too.
    thumbnail_updated_at = Column(DateTime, nullable=True)
    # Set for recurring automatic/scheduled publication — the worker leaves
    # this video alone until the next weekly slot in the channel's timezone.
    scheduled_publish_at = Column(DateTime, nullable=True)
    # Human approval gate used only when the compliance result is orange.
    # Green is allowed automatically at its slot; red remains blocked.
    approved_for_publish = Column(Boolean, nullable=False, default=False)
    youtube_compliance_report = Column(JSON, nullable=True)
    youtube_compliance_history = Column(JSON, nullable=True)
    youtube_compliance_reviewed_at = Column(DateTime, nullable=True)
    youtube_compliance_reviewed_by = Column(String(36), nullable=True)
    script_compliance_overridden = Column(Boolean, nullable=False, default=False)
    script_compliance_overridden_at = Column(DateTime, nullable=True)
    script_compliance_overridden_by = Column(String(36), nullable=True)
    publication_compliance_overridden = Column(Boolean, nullable=False, default=False)
    publication_compliance_overridden_at = Column(DateTime, nullable=True)
    publication_compliance_overridden_by = Column(String(36), nullable=True)

    channel = relationship("Channel", back_populates="videos")
    folder = relationship("Folder", back_populates="videos")

    def to_dict(self):
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "channel_name": self.channel.name if self.channel else None,
            "folder_id": self.folder_id,
            "input_type": self.input_type,
            # input_type remains the trustworthy fallback for videos created
            # before creation_source existed. In particular, an imported audio
            # must never render the generic script path in the progress UI.
            "creation_source": "audio" if self.input_type == "audio" else (self.creation_source or "script"),
            "title": self.title,
            "script_text": self.script_text,
            "audio_input_path": self.audio_input_path,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "output_path": self.output_path,
            "storage_backend": self.storage_backend or "local",
            "extended_retention": self.extended_retention,
            "retention_until": self.retention_until.isoformat() if self.retention_until else None,
            "source_assets_path": self.source_assets_path,
            "error_message": self.error_message,
            "duration_seconds": self.duration_seconds,
            "progress_stage": self.progress_stage,
            "progress_percent": self.progress_percent or 0,
            "purged_at": self.purged_at.isoformat() if self.purged_at else None,
            "expiry_warning_sent_at": self.expiry_warning_sent_at.isoformat() if self.expiry_warning_sent_at else None,
            "editable": bool(self.status == VideoStatus.DONE.value and self.output_path and not self.edit_assets_purged_at),
            "transcribe_audio": self.transcribe_audio,
            "audio_rights_confirmed": self.audio_rights_confirmed,
            "audio_source_type": self.audio_source_type,
            "voice_id": self.voice_id,
            "youtube_video_id": self.youtube_video_id,
            "youtube_published_at": self.youtube_published_at.isoformat() if self.youtube_published_at else None,
            "youtube_publish_error": self.youtube_publish_error,
            "youtube_description": self.youtube_description,
            "thumbnail_text": self.thumbnail_text,
            "thumbnail_regenerating": self.thumbnail_regenerating,
            "thumbnail_updated_at": self.thumbnail_updated_at.isoformat() if self.thumbnail_updated_at else None,
            "scheduled_publish_at": self.scheduled_publish_at.isoformat() if self.scheduled_publish_at else None,
            "approved_for_publish": self.approved_for_publish,
            "youtube_compliance_report": self.youtube_compliance_report,
            "youtube_compliance_history": self.youtube_compliance_history or [],
            "youtube_compliance_reviewed_at": self.youtube_compliance_reviewed_at.isoformat() if self.youtube_compliance_reviewed_at else None,
            "youtube_compliance_reviewed_by": self.youtube_compliance_reviewed_by,
            "script_compliance_overridden": self.script_compliance_overridden,
            "script_compliance_overridden_at": self.script_compliance_overridden_at.isoformat() if self.script_compliance_overridden_at else None,
            "script_compliance_overridden_by": self.script_compliance_overridden_by,
            "publication_compliance_overridden": self.publication_compliance_overridden,
            "publication_compliance_overridden_at": self.publication_compliance_overridden_at.isoformat() if self.publication_compliance_overridden_at else None,
            "publication_compliance_overridden_by": self.publication_compliance_overridden_by,
            "downloaded_at": self.downloaded_at.isoformat() if self.downloaded_at else None,
            "can_undo": bool(self.edit_history),
            "admin_priority": self.admin_priority,
        }


class Plan(Base):
    """Subscription tiers — deliberately not hardcoded in code: the admin
    dashboard creates/edits these, since pricing is the operator's call."""
    __tablename__ = "plans"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    price_fcfa = Column(Integer, nullable=False)
    duration_days = Column(Integer, nullable=False, default=30)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Credit-pack plans (the current model, mirroring Izivoice's own pricing):
    # paying grants `credits` credits, valid for `duration_days`, instead of
    # unlimited access for `duration_days` — a null value marks a legacy
    # subscription-style plan, kept only for historical Order/Subscription rows.
    credits = Column(Integer, nullable=True)
    # Shown struck-through next to price_fcfa on the pricing cards (matches
    # Izivoice's "was X, now Y" promo styling) — purely cosmetic, never used
    # for any charge/credit math.
    original_price_fcfa = Column(Integer, nullable=True)

    # Hybrid subscription tiers (credits=None, i.e. the Subscription branch of
    # _activate_subscription): how many videos this plan includes per cycle
    # (null = unlimited), whether AI features (auto-script, transcription, AI
    # images/voice) are usable on this plan at all, and how many bonus
    # credits are granted on each successful renewal to cover that AI usage —
    # see grant_subscription_cycle_credits in src/utils/billing.py. Unused by
    # credit-pack plans (credits is set): those already sell exactly what
    # they say on the tin, no separate quota/gate needed.
    video_quota_per_cycle = Column(Integer, nullable=True)
    # Superseded by the three granular flags below (transcription/images/
    # script gate independently now, matching the pricing cards' per-row
    # checklist) — column kept only so existing rows/migrations don't need a
    # backfill; nothing reads it any more.
    ai_features_enabled = Column(Boolean, nullable=False, default=True)
    ai_transcription_enabled = Column(Boolean, nullable=False, default=True)
    ai_images_enabled = Column(Boolean, nullable=False, default=True)
    ai_script_enabled = Column(Boolean, nullable=False, default=True)
    autopublish_enabled = Column(Boolean, nullable=False, default=True)
    monthly_credit_grant = Column(Integer, nullable=True)
    # Plan caps shown on the pricing cards — null means unlimited on both.
    max_channels = Column(Integer, nullable=True)
    max_video_duration_seconds = Column(Integer, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "price_fcfa": self.price_fcfa,
            "original_price_fcfa": self.original_price_fcfa,
            "duration_days": self.duration_days,
            "credits": self.credits,
            "video_quota_per_cycle": self.video_quota_per_cycle,
            "ai_transcription_enabled": self.ai_transcription_enabled,
            "ai_images_enabled": self.ai_images_enabled,
            "ai_script_enabled": self.ai_script_enabled,
            "autopublish_enabled": self.autopublish_enabled,
            "monthly_credit_grant": self.monthly_credit_grant,
            "max_channels": self.max_channels,
            "max_video_duration_seconds": self.max_video_duration_seconds,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Subscription(Base):
    """A user's paid (or admin-comped) access window. status="active" and
    expires_at in the future is what user_can_render()/user_has_active_subscription()
    (src/utils/billing.py) check — a user can have several rows over time
    (renewals, admin grants), only the latest active/unexpired one matters."""
    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(String(36), ForeignKey("plans.id"), nullable=True)  # null when admin-granted without a plan
    status = Column(String(20), nullable=False, default="active")  # "active" | "expired" | "cancelled"
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    # Set when an admin comps this from the dashboard instead of a real payment.
    granted_by_admin_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    note = Column(Text, nullable=True)

    user = relationship("User", back_populates="subscriptions", foreign_keys=[user_id])
    plan = relationship("Plan")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "plan_id": self.plan_id,
            "plan_name": self.plan.name if self.plan else None,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "granted_by_admin_id": self.granted_by_admin_id,
            "note": self.note,
        }


class Order(Base):
    """One payment attempt through Maketou or Tara Money. status flips
    pending->success exactly once (atomic claim in billing.py) to prevent a
    webhook + a poll + the reverify cron from double-crediting the same
    order if they race."""
    __tablename__ = "orders"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(String(36), ForeignKey("plans.id"), nullable=False)
    provider = Column(String(20), nullable=False)  # "maketou" | "tarapay"
    provider_ref = Column(String(255), nullable=True)  # Maketou cart id / Tara paymentId
    amount_fcfa = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="pending")  # "pending" | "success" | "failed" | "flagged_underpaid"
    # Same validity-tier system as Izivoice's own credit packs (see
    # src/utils/billing.py CREDIT_CYCLE_MARKUPS/CREDIT_CYCLE_DAYS): the base
    # plan price is per-month, and this multiplies the price/duration for a
    # longer commitment instead of selling separate plan rows per duration.
    billing_cycle = Column(String(20), nullable=False, default="monthly")  # monthly|quarterly|semiannual|yearly|lifetime
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    plan = relationship("Plan")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "plan_id": self.plan_id,
            "provider": self.provider,
            "provider_ref": self.provider_ref,
            "amount_fcfa": self.amount_fcfa,
            "billing_cycle": self.billing_cycle,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ApiUsageLog(Base):
    """One row per external API call that costs money (Anthropic/fal.ai/OpenAI
    text generation, Izivoice voice, fal.ai image generation, ...) — powers
    the admin "Coûts" page (see src/utils/cost_tracking.py for the pricing
    table and src/api/routes/admin.py for the aggregation endpoint). Written
    via a short-lived session inside log_usage() so instrumented call sites
    never need to thread a db session through just to log a cost — this
    means a logging failure can never fail the actual generation."""
    __tablename__ = "api_usage_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider = Column(String(30), nullable=False, index=True)  # "anthropic" | "fal_text" | "openai" | "izivoice_tts" | "izivoice_stt" | "fal_image"
    operation = Column(String(50), nullable=False)  # "script" | "voiceover" | "transcription" | "image" | ...
    quantity = Column(Float, nullable=False, default=0)  # tokens, characters, seconds, or image count depending on provider
    unit = Column(String(20), nullable=False, default="unit")  # "tokens" | "characters" | "seconds" | "images"
    cost_usd = Column(Float, nullable=False, default=0)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    channel_id = Column(String(36), ForeignKey("channels.id"), nullable=True, index=True)
    video_id = Column(String(36), ForeignKey("videos.id"), nullable=True, index=True)
    meta = Column(JSON, nullable=True)  # free-form extra detail (model name, provider fallback used, etc.)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AppSetting(Base):
    """Generic global key/value flags the admin panel toggles at runtime, with
    no redeploy needed — started for per-generator image-provider switches
    (e.g. "thumbnail_provider_mode": force thumbnails free-only vs. allow the
    paid fallback chain), but deliberately generic so future admin-controlled
    toggles can reuse this instead of each needing its own column/table."""
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HuggingFaceAccount(Base):
    """A pool of API keys for image-generation providers (Hugging Face,
    fal.ai, Izivoice), rotated through so one account/key running dry
    (quota, credits) doesn't stall generation — see the `provider` column
    and src/pipeline/images.py's provider-generic rotation helper. Table/
    class names predate the fal/Izivoice generalization (originally
    Hugging-Face-only); kept as-is rather than a risky rename on a table
    that already holds production data. Admin-managed (add/remove/see live
    status per provider) so new keys can be added over time without a
    redeploy — see src/api/routes/admin.py's image-provider-keys routes."""
    __tablename__ = "huggingface_accounts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # "huggingface" | "fal" | "izivoice" — which image-generation provider
    # this key belongs to. Defaults to "huggingface" for rows created before
    # this column existed.
    provider = Column(String(20), nullable=False, default="huggingface")
    token = Column(String(255), nullable=False, unique=True)
    label = Column(String(255), nullable=True)
    # "active" (last use succeeded or never tried), "quota_exhausted" (401/402/429
    # from the provider), "invalid" (any other hard failure) — purely informational,
    # rotation still tries every row regardless of status (a quota can refill
    # monthly), this just drives the admin dashboard's live status column.
    status = Column(String(20), nullable=False, default="active")
    last_used_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "provider": self.provider,
            "token_preview": f"{self.token[:8]}...{self.token[-4:]}" if len(self.token) > 12 else self.token,
            "label": self.label,
            "status": self.status,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "last_error": self.last_error,
            "is_enabled": self.is_enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class VoiceCloneJob(Base):
    """A voice-cloning request, processed by the worker (not the API process)
    and tracked here instead of in an in-memory dict — the API container gets
    redeployed far more often than the worker (routine API/frontend changes
    never touch it, see entrypoint.sh's ROLE split), and an in-memory job
    tied to the API process was getting silently wiped mid-clone by any
    redeploy, leaving the creator's "Clonage…" button stuck forever with no
    way to ever resolve. The uploaded sample is written to shared storage
    (audio_path, on the volume both API and worker containers mount) since
    the two run in different containers and can't just pass bytes in memory."""
    __tablename__ = "voice_clone_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36), ForeignKey("channels.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    audio_path = Column(String(1024), nullable=False)  # relative to STORAGE_PATH; deleted once processed
    status = Column(String(20), nullable=False, default="pending")  # "pending" | "processing" | "done" | "error"
    voice_id = Column(String(255), nullable=True)
    gender = Column(String(20), nullable=False, default="neutral")  # "male" | "female" | "neutral"
    preview_url = Column(String(1024), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "job_id": self.id,
            "status": self.status,
            "voice_id": self.voice_id,
            "name": self.name,
            "gender": self.gender,
            "preview_url": self.preview_url,
            "detail": self.error_message,
        }


class Voice(Base):
    """Catalog of synthetic AI voices imported into Easy Voice."""
    __tablename__ = "voices"

    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))  # voice_id
    name = Column(String(255), nullable=False, index=True)
    language = Column(String(10), nullable=False, default="fr", index=True)
    gender = Column(String(20), nullable=False, default="neutral", index=True)  # "male" | "female" | "neutral"
    category = Column(String(100), nullable=True, index=True)
    preview_url = Column(String(1024), nullable=True)
    tags = Column(JSON, nullable=True)  # list of tags e.g. ["soft", "storytelling"]
    provider = Column(String(50), nullable=False, default="easyvoice")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "voice_id": self.id,
            "name": self.name,
            "language": self.language,
            "gender": self.gender,
            "category": self.category,
            "preview_url": self.preview_url,
            "tags": self.tags or [],
            "provider": self.provider,
            "is_active": self.is_active,
        }


class CommunityLibraryFolder(Base):
    """A channel's image library, opted into the community-sharing program by
    its owner at upload time (Channel.image_style.share_with_community) —
    never shared by default. One row per channel; `status` doubles as the
    admin-facing badge (pending/approved/flagged) AND the inclusion filter
    for that niche's "master" library: a niche's collaborative library is
    simply the union of every `approved` folder in that niche, read live off
    disk (STORAGE_PATH/channels/{channel_id}/library) — no file copying, so
    "merging" two folders is just approving both into the same niche."""
    __tablename__ = "community_library_folders"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36), ForeignKey("channels.id"), nullable=False, unique=True, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    niche = Column(String(255), nullable=False, index=True)
    image_count = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="pending")  # "pending" | "approved" | "flagged"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    channel = relationship("Channel")
    user = relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "channel_name": self.channel.name if self.channel else None,
            "user_id": self.user_id,
            "user_email": self.user.email if self.user else None,
            "niche": self.niche,
            "image_count": self.image_count,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CommunityLibraryImagePlacement(Base):
    """Admin-only virtual classification for individual collaborative images.
    The source file remains in its channel library (channel_id + filename is
    always its real, physical location — never changes); only where it's
    displayed/pooled changes. `niche` reassigns which niche's pool the image
    feeds. `target_channel_id`, when set, additionally folds it into a
    SPECIFIC other channel's folder when browsing that niche in the admin
    library (used by "Fusionner avec…" — merging channel A into channel B
    shows A's images under B's folder, without moving any files), instead of
    it showing under its own origin channel."""
    __tablename__ = "community_library_image_placements"
    __table_args__ = (UniqueConstraint("channel_id", "filename", name="uq_community_image_channel_filename"),)

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36), ForeignKey("channels.id"), nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    niche = Column(String(255), nullable=False, index=True)
    target_channel_id = Column(String(36), ForeignKey("channels.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CreditPot(Base):
    """One purchased credit pack, ported from Izivoice's own credit_pots
    model: a balance isn't one number on the user row, it's the sum of every
    still-valid (non-expired, non-empty) pot — so a promo pack's validity
    window is enforced for free instead of needing separate expiry logic."""
    __tablename__ = "credit_pots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Integer, nullable=False)  # remaining balance in this pot
    original_amount = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "amount": self.amount,
            "original_amount": self.original_amount,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CreditTransaction(Base):
    """Audit trail of every credit movement — positive for a purchase,
    negative for a debited API call — independent of CreditPot's own
    mutable balances, so history survives even after a pot expires/empties."""
    __tablename__ = "credit_transactions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # Nullable: only set on debits made with a video already in hand (e.g. the
    # base render fee) — the per-video cost recap falls back to a time-window
    # match against the video's render window for older/untagged debits.
    video_id = Column(String(36), nullable=True, index=True)
    amount = Column(Integer, nullable=False)
    transaction_type = Column(String(30), nullable=False)  # "purchase" | "debit" | "admin_grant" | "refund"
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "video_id": self.video_id,
            "amount": self.amount,
            "transaction_type": self.transaction_type,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ApiCreditPot(Base):
    """Credits purchased specifically for external API usage.

    Kept in separate tables from the dashboard wallet so API consumption can
    never spend the creator's regular interface credits.
    """
    __tablename__ = "api_credit_pots"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    original_amount = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ApiCreditTransaction(Base):
    """Audit trail for API-wallet purchases, debits, refunds and adjustments."""
    __tablename__ = "api_credit_transactions"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    transaction_type = Column(String(30), nullable=False)
    description = Column(Text, nullable=True)
    request_id = Column(String(80), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
