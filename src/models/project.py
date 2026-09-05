from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime
import uuid

class VideoStatus(str, Enum):
    QUEUED = "queued"
    RENDERING = "rendering"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    # Set by the creator via POST /{video_id}/cancel while a video is still
    # queued or rendering — checked between pipeline stages (see
    # queue_runner.py's update_progress) so an in-flight render stops at the
    # next stage boundary instead of finishing into a result nobody wanted.
    CANCELLED = "cancelled"

class InputType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"

class SubtitleStyle(BaseModel):
    enabled: bool = True                # burn subtitles into the render at all
    font: str = "Arial"
    size: int = 44
    color: str = "#FFD700"              # accent/highlight color (hex or ASS format)
    base_color: str = "#FFFFFF"         # non-highlighted word color
    outline_color: str = "&H00000000"   # Black outline
    outline_width: int = 3
    position: str = "bottom"            # bottom, center, top
    align: str = "center"               # left, center, right
    karaoke: bool = True                # legacy flag, kept for old channels — superseded by highlight_mode
    highlight_mode: str = "word"        # "word" (per-word), "line" (whole chunk), "none"
    words_per_line: int = 6
    subtitle_mode: str = "dynamic"      # "dynamic" pronunciation sync | "paragraph" long persistent blocks
    paragraph_duration_seconds: int = 45
    paragraph_max_words: int = 90
    paragraph_words_per_line: int = 10
    text_case: str = "none"             # "none", "upper", "lower", "capitalize"
    bold: bool = True
    italic: bool = False
    letter_spacing: int = 0
    opacity: float = 100                # 0-100
    rotation: int = 0                   # degrees
    x_offset: int = 0                   # px nudge at 1920 width
    y_offset: int = 140                 # 64% on the vertical position control
    box_color: str = "transparent"      # background rectangle color, or "transparent"
    box_padding: int = 10
    shadow: bool = False
    shadow_color: str = "#000000"
    shadow_distance: int = 3

class OverlayItem(BaseModel):
    """One extra PNG sticker burned into every render of this channel — e.g. a
    "Subscribe" button or a bell icon, the kind of thing creators paste onto
    their videos by hand. Distinct from the single channel logo (still its
    own logo_path/logo_enabled/logo_corner/logo_size_percent below) since a
    channel can want several of these stacked in different corners at once."""
    id: str
    image_path: str             # storage-relative, e.g. "channels/<id>/overlays/<overlay_id>.png"
    enabled: bool = True
    corner: str = "top-right"   # legacy 4-preset fallback, only used when x_percent/y_percent are absent (old saved channels)
    x_percent: Optional[float] = None  # 0-100, free placement — where the image's own top-left sits inside the inset frame; None = derive from `corner`
    y_percent: Optional[float] = None  # same, vertical axis
    size_percent: float = 10    # width as % of the 1920px-wide render frame
    opacity: float = 1.0        # 0-1
    shape: str = "rectangle"    # "rectangle" | "rounded" | "circle" — actually masked at render time, see assembler.py:apply_overlay_shape_mask

class BrandingConfig(BaseModel):
    logo_path: Optional[str] = None
    logo_enabled: bool = True           # burn the channel logo into the render at all
    logo_corner: str = "top-right"      # legacy 4-preset fallback, only used when logo_x_percent/logo_y_percent are absent
    logo_x_percent: Optional[float] = None  # 0-100, free placement — see OverlayItem.x_percent
    logo_y_percent: Optional[float] = None
    logo_size_percent: float = 5        # width as % of the 1920px-wide render frame (≈100px, matches the old fixed size)
    logo_shape: str = "rectangle"       # "rectangle" | "rounded" | "circle" — same masking as overlays
    channel_name_text: Optional[str] = None
    overlays: List[OverlayItem] = Field(default_factory=list)

class MusicPreference(BaseModel):
    enabled: bool = True
    mode: str = "library"               # "library" (user's own tracks) | "ai_generate" (Izivoice)
    track_id_or_style: str = "ambient"  # legacy, unused by new modes — kept for old channels
    tracks: List[str] = Field(default_factory=list)  # storage-relative paths to uploaded tracks; one is picked at random per render
    ai_prompt: Optional[str] = None     # optional override prompt for AI generation; defaults to the channel niche
    volume: float = 0.10                # Background volume level (0.0 - 1.0)
    auto_ducking: bool = False
    ducking_amount: float = 0.70
    fade_in_seconds: float = 2.0
    fade_out_seconds: float = 3.0
    soundgoodizer_enabled: bool = False
    soundgoodizer_amount: float = 0.35
    reverb_enabled: bool = False
    reverb_amount: float = 0.15
    maximus_enabled: bool = False
    maximus_amount: float = 0.40

class ImageStyle(BaseModel):
    source: str = "library"             # legacy single-choice mirror of sources[0], kept for old channels/back-compat
    # Any combination of "ai_generated" | "library" | "community" — a
    # creator can now check all three at once (own upload + AI generation +
    # the niche's shared pool, tried in that priority order — see
    # IMAGE_SOURCE_PRIORITY/resolve_enabled_image_sources in images.py).
    # Pydantic silently drops any field not declared here on every save, so
    # this list existing only on the frontend meant every real save reset a
    # 2-3-way combination straight back down to whatever `source` alone held.
    sources: List[str] = Field(default_factory=lambda: ["library"])
    style_prompt: str = "cinematic, dramatic lighting, high detail, masterpiece"
    library_path: Optional[str] = None
    library_image_count: int = 0
    broll_path: Optional[str] = None
    broll_count: int = 0
    # How the creator wants the montage to consume their uploaded media.
    # Kept separate from `sources`, which controls AI/community image pools.
    media_mode: str = "images"  # images | videos | mixed
    # When True, each B-roll scene grabs a random slice of its source clip
    # instead of playing it continuously start-to-finish (orchestrator.py) —
    # for re-cutting footage that's already been published elsewhere, where
    # a straight replay would read as a rehash. False (default) keeps the
    # continuous "pillar clip" behavior (e.g. a single talking-head avatar
    # recording meant to play through naturally).
    broll_shuffle: bool = False
    # Opt-in only, set at upload time — this channel's own library becomes
    # eligible for admin curation into its niche's shared community library
    # (see CommunityLibraryFolder). Never shared without this being true.
    share_with_community: bool = False
    # How many distinct AI images to generate per video before recycling —
    # "auto" (max_unique_images=None) lets the pipeline pick a sensible count
    # from the video's own length; "manual" pins it to a specific number.
    image_count_mode: Optional[str] = None
    max_unique_images: Optional[int] = None

class EffectsConfig(BaseModel):
    enabled: bool = True                # master on/off for color grade + overlay effects together
    grain: bool = True                  # legacy flag, kept for old channels — superseded by overlay_effects
    overlay_effect: str = "grain"       # legacy single-choice field, kept for old channels — superseded by overlay_effects
    overlay_effects: List[str] = Field(default_factory=lambda: ["grain"])  # any combination of "grain", "white_noise", "vignette"
    color_grade: str = "none"           # optional: "warm", "vintage", "dramatic", "none"
    grain_intensity: int = 50           # 0-100, scales the noise/grain amount
    vignette_intensity: int = 50        # 0-100, scales how dark the vignette edges get
    particle_intensity: int = 50        # 0-100, shared strength for atmospheric/particle effects
    # Fine controls for the animated particle overlays. These live in the
    # channel JSON config, so old channels keep their established result.
    particle_density: int = 50          # 0-100, adds subtle foreground/depth layers
    particle_size: int = 50             # 0-100, small/far to large/near particles
    particle_speed: int = 50            # 0-100, calm drift to faster movement
    particle_dispersion: int = 50       # 0-100, compact to wide distribution
    particle_direction: str = "auto"    # auto | up | down | left | right
    effect_settings: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    zoom_min_pct: float = 1.0
    zoom_max_pct: float = 1.15
    watermark_enabled: bool = True       # large centered official logo at low opacity — free-tier default

class ChannelCreate(BaseModel):
    name: str
    description: Optional[str] = None
    niche: str = "General"
    content_type: str = "narration"  # "narration" | "music"
    music_channel_config: Optional[dict] = None
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    music_preference: MusicPreference = Field(default_factory=MusicPreference)
    image_style: ImageStyle = Field(default_factory=ImageStyle)
    effects_config: EffectsConfig = Field(default_factory=EffectsConfig)
    automation_mode: str = "manual"  # "manual" | "auto"
    automation_style_prompt: Optional[str] = None
    topic_examples: Optional[str] = None  # example titles/topics, one per line — the creator's own or a channel to emulate
    use_web_trends: bool = False  # let topic selection use live web search (news/trend-driven niches)
    youtube_topic_sources: Optional[str] = None  # YouTube channel/video URLs used as topic research sources
    videos_per_day: int = 1
    automation_window_start_hour: int = 7
    automation_window_end_hour: int = 11
    active_days: Optional[List[int]] = None  # 0=Monday..6=Sunday; None/empty = every day
    script_generation_hour: Optional[int] = None  # local hour (0-23) script writing may start; None = as soon as possible
    script_generation_minute: int = 0  # local minute (0-59) within script_generation_hour
    script_generation_second: int = 0  # local second (0-59) — display only, not enforced by the worker
    script_generation_days: Optional[List[int]] = None  # 0=Monday..6=Sunday; None/empty = every day
    script_structure: Optional[dict] = None
    voice_id: Optional[str] = None
    voice_name: Optional[str] = None
    voice_settings: Optional[dict] = None
    publish_mode: str = "manual"  # "auto" | "scheduled" | "manual"
    youtube_made_for_kids: bool = False
    youtube_default_description: Optional[str] = None
    youtube_default_tags: List[str] = Field(default_factory=list)
    youtube_category_id: str = "22"
    youtube_privacy_status: str = "public"
    youtube_contains_synthetic_media: bool = True
    youtube_license: str = "youtube"
    youtube_notify_subscribers: bool = True
    youtube_embeddable: bool = True
    youtube_public_stats_viewable: bool = True
    publish_time_mode: str = "range"  # "fixed" (publish_schedule_hour) | "range" (automation_window_*)
    publish_schedule_hour: int = 8
    publish_schedule_day_offset: int = 1
    timezone: str = "Africa/Douala"  # IANA name, auto-detected client-side
    transcribe_audio_default: bool = True  # auto-mode videos only; manual submissions choose per-video
    thumbnail_style: Optional[dict] = None  # {"style_prompt": str, "reference_image_paths": [str]}
    sfx_enabled: bool = True  # whether the sound-effect matching pass runs at all for this channel's renders

class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    niche: Optional[str] = None
    content_type: Optional[str] = None
    music_channel_config: Optional[dict] = None
    subtitle_style: Optional[SubtitleStyle] = None
    branding: Optional[BrandingConfig] = None
    music_preference: Optional[MusicPreference] = None
    image_style: Optional[ImageStyle] = None
    effects_config: Optional[EffectsConfig] = None
    automation_mode: Optional[str] = None
    automation_style_prompt: Optional[str] = None
    topic_examples: Optional[str] = None
    use_web_trends: Optional[bool] = None
    youtube_topic_sources: Optional[str] = None
    videos_per_day: Optional[int] = None
    automation_window_start_hour: Optional[int] = None
    automation_window_end_hour: Optional[int] = None
    active_days: Optional[List[int]] = None
    script_generation_hour: Optional[int] = None
    script_generation_minute: Optional[int] = None
    script_generation_second: Optional[int] = None
    script_generation_days: Optional[List[int]] = None
    script_structure: Optional[dict] = None
    voice_id: Optional[str] = None
    voice_name: Optional[str] = None
    voice_settings: Optional[dict] = None
    publish_mode: Optional[str] = None
    youtube_made_for_kids: Optional[bool] = None
    youtube_default_description: Optional[str] = None
    youtube_default_tags: Optional[List[str]] = None
    youtube_category_id: Optional[str] = None
    youtube_privacy_status: Optional[str] = None
    youtube_contains_synthetic_media: Optional[bool] = None
    youtube_license: Optional[str] = None
    youtube_notify_subscribers: Optional[bool] = None
    youtube_embeddable: Optional[bool] = None
    youtube_public_stats_viewable: Optional[bool] = None
    publish_time_mode: Optional[str] = None
    publish_schedule_hour: Optional[int] = None
    publish_schedule_day_offset: Optional[int] = None
    timezone: Optional[str] = None
    transcribe_audio_default: Optional[bool] = None
    is_active: Optional[bool] = None
    thumbnail_style: Optional[dict] = None  # {"style_prompt": str, "reference_image_paths": [str]}
    sfx_enabled: Optional[bool] = None

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

class ForgotPasswordPayload(BaseModel):
    email: str

class ChangePasswordPayload(BaseModel):
    user_id: str
    old_password: str
    new_password: str

class ResetPasswordPayload(BaseModel):
    email: str
    code: str
    new_password: str

class IzivoiceConnectionPayload(BaseModel):
    user_id: str
    api_key: str

class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    created_at: Optional[str] = None
