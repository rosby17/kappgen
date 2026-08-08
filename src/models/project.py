from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime
import uuid

class VideoStatus(str, Enum):
    QUEUED = "queued"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"

class InputType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"

class SubtitleStyle(BaseModel):
    font: str = "Arial"
    size: int = 44
    color: str = "&H00FFFFFF"           # ASS Hex color format (&HAA00BBGGRR or &H00FFFFFF white)
    outline_color: str = "&H00000000"   # Black outline
    outline_width: int = 3
    position: str = "bottom"            # bottom, center, top
    karaoke: bool = True                # Enable word-by-word highlight

class BrandingConfig(BaseModel):
    logo_path: Optional[str] = None
    channel_name_text: Optional[str] = None

class MusicPreference(BaseModel):
    enabled: bool = True
    track_id_or_style: str = "ambient"
    volume: float = 0.15                # Background volume level (0.0 - 1.0)

class ImageStyle(BaseModel):
    source: str = "library"             # "library" | "ai_generated"
    style_prompt: str = "cinematic, dramatic lighting, high detail, masterpiece"
    library_path: Optional[str] = None
    library_image_count: int = 0

class EffectsConfig(BaseModel):
    grain: bool = True
    color_grade: str = "warm"           # "warm", "vintage", "dramatic", "none"
    zoom_min_pct: float = 1.0
    zoom_max_pct: float = 1.15

class ChannelCreate(BaseModel):
    name: str
    niche: str = "General"
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    music_preference: MusicPreference = Field(default_factory=MusicPreference)
    image_style: ImageStyle = Field(default_factory=ImageStyle)
    effects_config: EffectsConfig = Field(default_factory=EffectsConfig)

class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    niche: Optional[str] = None
    subtitle_style: Optional[SubtitleStyle] = None
    branding: Optional[BrandingConfig] = None
    music_preference: Optional[MusicPreference] = None
    image_style: Optional[ImageStyle] = None
    effects_config: Optional[EffectsConfig] = None

class VideoCreate(BaseModel):
    channel_id: str
    input_type: str = "text"             # "text" | "audio"
    script_text: Optional[str] = ""
    audio_input_path: Optional[str] = None

class BatchVideoCreate(BaseModel):
    channel_id: str
    scripts: List[str]

class UserCreate(BaseModel):
    email: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class ChangePasswordPayload(BaseModel):
    user_id: str
    old_password: str
    new_password: str

class ResetPasswordPayload(BaseModel):
    email: str
    new_password: str

class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    created_at: Optional[str] = None
