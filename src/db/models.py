import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Float, Integer, Boolean
from sqlalchemy.orm import relationship
from src.db.session import Base
from src.models.project import VideoStatus

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False, default="Créateur NicheCut")
    hashed_password = Column(String(255), nullable=False)
    picture_url = Column(String(1024), nullable=True)
    phone = Column(String(50), nullable=True)
    auth_provider = Column(String(50), nullable=False, default="password")  # "password" | "google"
    izivoice_api_key_encrypted = Column(Text, nullable=True)
    izivoice_key_prefix = Column(String(20), nullable=True)
    izivoice_connected_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    channels = relationship("Channel", back_populates="user", cascade="all, delete-orphan")
    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "picture_url": self.picture_url,
            "phone": self.phone,
            "auth_provider": self.auth_provider,
            "izivoice_connected": bool(self.izivoice_api_key_encrypted),
            "izivoice_key_prefix": self.izivoice_key_prefix,
            "izivoice_connected_at": self.izivoice_connected_at.isoformat() if self.izivoice_connected_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "channel_count": len(self.channels) if self.channels else 0
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
    created_at = Column(DateTime, default=datetime.utcnow)

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
        # the two things a render genuinely needs: a voice, and a visual
        # source with real content behind it. Identity/niche are always set
        # (they default), so they don't gate this.
        image_style = self.image_style or {}
        visuals_ready = bool(
            (image_style.get("source") == "ai_generated" and image_style.get("style_prompt"))
            or (image_style.get("source") in ("library", "hybrid") and (image_style.get("library_image_count") or 0) > 0)
        )
        voice_ready = bool(self.voice_id)
        completion_percent = 50 + (25 if voice_ready else 0) + (25 if visuals_ready else 0)
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "description": self.description,
            "niche": self.niche,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "subtitle_style": self.subtitle_style,
            "branding": self.branding,
            "music_preference": self.music_preference,
            "image_style": self.image_style,
            "thumbnail_style": self.thumbnail_style,
            "effects_config": self.effects_config,
            "completion_percent": completion_percent,
            "is_render_ready": voice_ready and visuals_ready,
            "automation_mode": self.automation_mode or "manual",
            "automation_style_prompt": self.automation_style_prompt,
            "videos_per_day": self.videos_per_day or 1,
            "automation_window_start_hour": self.automation_window_start_hour if self.automation_window_start_hour is not None else 7,
            "automation_window_end_hour": self.automation_window_end_hour if self.automation_window_end_hour is not None else 11,
            "active_days": self.active_days,
            "last_auto_run_date": self.last_auto_run_date,
            "timezone": self.timezone or "Africa/Douala",
            "script_structure": self.script_structure,
            "voice_id": self.voice_id,
            "voice_name": self.voice_name,
            "voice_settings": self.voice_settings or {"speed": 0.845, "stability": 0.8, "similarity_boost": 0.9, "style": 0.0},
            "publish_mode": self.publish_mode or "manual",
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
    created_at = Column(DateTime, default=datetime.utcnow)

    videos = relationship("Video", back_populates="folder")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "video_count": len(self.videos) if self.videos else 0
        }


class Video(Base):
    __tablename__ = "videos"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36), ForeignKey("channels.id"), nullable=False)
    folder_id = Column(String(36), ForeignKey("folders.id"), nullable=True)
    input_type = Column(String(50), nullable=False, default="text")
    title = Column(String(255), nullable=True)  # set for auto-generated videos; used as the YouTube upload title
    script_text = Column(Text, nullable=True, default="")
    audio_input_path = Column(String(512), nullable=True)
    status = Column(String(50), nullable=False, default=VideoStatus.QUEUED.value)

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    output_path = Column(String(512), nullable=True)
    source_assets_path = Column(String(512), nullable=True)
    error_message = Column(Text, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    estimated_duration_seconds = Column(Float, nullable=True)
    purged_at = Column(DateTime, nullable=True)
    restart_count = Column(Integer, nullable=False, default=0)
    progress_stage = Column(String(255), nullable=True)
    progress_percent = Column(Integer, nullable=False, default=0)
    is_reassembly = Column(Boolean, nullable=False, default=False)
    edit_assets_purged_at = Column(DateTime, nullable=True)
    # Only meaningful for input_type="audio": whether to spend Izivoice STT
    # credits transcribing the upload for accurate subtitles, or skip it (free,
    # approximate title-based captions instead). Opt-out toggle in the wizard.
    transcribe_audio = Column(Boolean, nullable=False, default=True)
    voice_id = Column(String(255), nullable=True)
    # Set alongside is_reassembly=True + status=QUEUED by the Studio editor's
    # scene endpoints; tells the worker which lightweight edit function to run
    # instead of the full pipeline. JSON: {"type": "image"|"subtitle_text"|"audio",
    # "scene_index": int, "text": str|null}. Cleared once the worker picks it up.
    pending_edit = Column(JSON, nullable=True)
    # Set once the worker successfully auto-publishes this video to the
    # channel's connected YouTube account (auto-mode channels only).
    youtube_video_id = Column(String(32), nullable=True)
    youtube_published_at = Column(DateTime, nullable=True)
    youtube_publish_error = Column(Text, nullable=True)
    # AI-proposed YouTube description, generated as soon as the render finishes
    # (alongside `title`, reused for this) so the creator can review/edit both
    # before publishing instead of only seeing them at the moment of upload.
    youtube_description = Column(Text, nullable=True)
    # Set when the channel's publish_mode is "scheduled" — the worker leaves
    # this video alone until this time, then publishes it automatically.
    scheduled_publish_at = Column(DateTime, nullable=True)
    # Human approval gate for publish_mode "scheduled": the render finishes
    # ahead of the publish date specifically so the creator has time to watch
    # and approve it — run_scheduled_publishes() never fires unless this is
    # True, no matter how long scheduled_publish_at has passed. Irrelevant for
    # publish_mode "auto" (publishes immediately, no gate) or "manual".
    approved_for_publish = Column(Boolean, nullable=False, default=False)

    channel = relationship("Channel", back_populates="videos")
    folder = relationship("Folder", back_populates="videos")

    def to_dict(self):
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "channel_name": self.channel.name if self.channel else None,
            "folder_id": self.folder_id,
            "input_type": self.input_type,
            "title": self.title,
            "script_text": self.script_text,
            "audio_input_path": self.audio_input_path,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "output_path": self.output_path,
            "source_assets_path": self.source_assets_path,
            "error_message": self.error_message,
            "duration_seconds": self.duration_seconds,
            "progress_stage": self.progress_stage,
            "progress_percent": self.progress_percent or 0,
            "purged_at": self.purged_at.isoformat() if self.purged_at else None,
            "editable": bool(self.status == VideoStatus.DONE.value and self.output_path and not self.edit_assets_purged_at),
            "transcribe_audio": self.transcribe_audio,
            "voice_id": self.voice_id,
            "youtube_video_id": self.youtube_video_id,
            "youtube_published_at": self.youtube_published_at.isoformat() if self.youtube_published_at else None,
            "youtube_publish_error": self.youtube_publish_error,
            "youtube_description": self.youtube_description,
            "scheduled_publish_at": self.scheduled_publish_at.isoformat() if self.scheduled_publish_at else None,
            "approved_for_publish": self.approved_for_publish,
        }
