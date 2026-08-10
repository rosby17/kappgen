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
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "channel_count": len(self.channels) if self.channels else 0
        }


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
    niche = Column(String(255), nullable=False, default="General")
    created_at = Column(DateTime, default=datetime.utcnow)

    # JSON configurations
    subtitle_style = Column(JSON, nullable=False, default=dict)
    branding = Column(JSON, nullable=False, default=dict)
    music_preference = Column(JSON, nullable=False, default=dict)
    image_style = Column(JSON, nullable=False, default=dict)
    effects_config = Column(JSON, nullable=False, default=dict)

    # Relationships
    user = relationship("User", back_populates="channels")
    videos = relationship("Video", back_populates="channel", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "niche": self.niche,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "subtitle_style": self.subtitle_style,
            "branding": self.branding,
            "music_preference": self.music_preference,
            "image_style": self.image_style,
            "effects_config": self.effects_config,
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

    channel = relationship("Channel", back_populates="videos")
    folder = relationship("Folder", back_populates="videos")

    def to_dict(self):
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "channel_name": self.channel.name if self.channel else None,
            "folder_id": self.folder_id,
            "input_type": self.input_type,
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
        }
