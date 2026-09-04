import math
import os
import subprocess
import re
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg
from src.config import STORAGE_PATH, ASSETS_PATH


def apply_overlay_shape_mask(source_path: Path, shape: str, temp_dir: Path, cache_key: str) -> Path:
    """Returns a path to use as the actual overlay input: for shape="rectangle"
    (the default — a logo or a "Subscribe" banner is rarely meant to be
    cropped), just the original source_path, unchanged. For "circle"/"rounded"
    it pre-masks the image with Pillow into a temp PNG (transparent outside
    the shape) *before* ffmpeg ever sees it — simpler and more portable than
    building an equivalent ffmpeg geq/alphamerge filter graph, and this
    codebase already leans on Pillow for image work elsewhere (image_pool.py).
    The mask is applied at the source's native resolution, then ffmpeg's
    normal scale/overlay filters run on the masked result exactly like any
    other overlay image."""
    if shape not in ("circle", "rounded"):
        return source_path
    from PIL import Image, ImageDraw

    img = Image.open(source_path).convert("RGBA")
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    if shape == "circle":
        # A circle only reads sensibly on a square-ish image — fit it inside
        # the shorter side so it never clips the source's own content, rather
        # than stretching an ellipse across a wide/tall sticker.
        side = min(w, h)
        cx, cy = w / 2, h / 2
        draw.ellipse([cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2], fill=255)
    else:  # rounded
        radius = round(min(w, h) * 0.18)
        draw.rounded_rectangle([0, 0, w, h], radius=radius, fill=255)

    # Combine with the source's own alpha (a transparent PNG sticker should
    # stay transparent outside its artwork even inside the mask shape) rather
    # than overwriting it outright.
    alpha = Image.composite(img.split()[3], Image.new("L", (w, h), 0), mask)
    img.putalpha(alpha)

    out_path = temp_dir / f"overlay_mask_{cache_key}_{shape}.png"
    img.save(out_path, "PNG")
    return out_path

WATERMARK_PATH = ASSETS_PATH / "branding" / "watermark.png"

# Looping particle overlay clips (see scripts/generate_particle_assets.py).
# Rendered once on a black background; composited with blend=all_mode=screen
# in _apply_particles_step, which is a no-op on black so only the bright
# particle pixels show through onto the real video underneath.
PARTICLES_DIR = ASSETS_PATH / "particles"
PARTICLE_ASSETS = {
    "stars": PARTICLES_DIR / "stars.mp4",
    "dust": PARTICLES_DIR / "dust.mp4",
    "snow": PARTICLES_DIR / "snow.mp4",
    "rain": PARTICLES_DIR / "rain.mp4",
    "sparks": PARTICLES_DIR / "sparks.mp4",
}

# Every layer is a looping video decoder. Cap the composition so a creator can
# stack effects without recreating the memory pressure of a multi-input render.
MAX_PARTICLE_LAYERS = 6


def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Read a creator setting safely, including configs saved by older clients."""
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _build_particle_layers(effects: Dict[str, Any], active_particles: List[str]) -> tuple:
    """Build a restrained multi-depth particle composition.

    Older configs default to a single layer per effect, preserving their visual
    result. Extra, offset layers only begin above the density midpoint.
    """
    density = _bounded_number(effects.get("particle_density", 50), 50, 0, 100)
    copies = 1 if density <= 50 else 2 if density <= 82 else 3
    # Give each chosen effect its base layer first. Extra depth is then shared
    # fairly, so enabling stars + dust + snow never makes the later selections
    # disappear merely because an earlier effect consumed the layer budget.
    layers = [{"id": particle_id, "depth": 0, "copies": copies} for particle_id in active_particles]
    layers = layers[:MAX_PARTICLE_LAYERS]
    for depth in range(1, copies):
        for particle_id in active_particles:
            if len(layers) >= MAX_PARTICLE_LAYERS:
                actual_counts = {pid: sum(item["id"] == pid for item in layers) for pid in active_particles}
                for item in layers:
                    item["copies"] = actual_counts[item["id"]]
                return layers, density
            layers.append({"id": particle_id, "depth": depth, "copies": copies})
    actual_counts = {pid: sum(item["id"] == pid for item in layers) for pid in active_particles}
    for item in layers:
        item["copies"] = actual_counts[item["id"]]
    return layers, density

# Gentle dissolves only — wipes/slides/circle-opens read as abrupt "cuts with
# a gimmick" rather than a smooth blend between scenes.
XFADE_TRANSITIONS = ["fade", "fadeblack", "dissolve"]
XFADE_DURATION = 1.2  # seconds of overlap between consecutive clips — soft, unhurried blend

PRESET_MARGIN_PERCENT = 6

FRAME_ASPECT = 16 / 9  # the render frame is always 1920x1080

def _preset_xy(corner: str, size_percent: float) -> tuple:
    """The 4 legacy corner presets, margin-safe (never flush to an edge)
    regardless of the image's size — same values the frontend's presetXY in
    App.jsx computes for its quick-position buttons, so an old channel that
    only ever had `corner` renders identically to how it always looked.

    size_percent is a % of the frame's WIDTH; assuming a roughly square
    logo/sticker, its height in pixels equals that many % of width, which is
    a *larger* percentage of the frame's shorter (16:9) HEIGHT — the bottom
    margin has to account for that or a "bottom" preset clips the image
    against the frame's bottom edge instead of sitting cleanly inside it."""
    right_x = 100 - PRESET_MARGIN_PERCENT - size_percent
    bottom_y = 100 - PRESET_MARGIN_PERCENT - size_percent * FRAME_ASPECT
    return {
        "top-left": (PRESET_MARGIN_PERCENT, PRESET_MARGIN_PERCENT),
        "top-right": (right_x, PRESET_MARGIN_PERCENT),
        "bottom-left": (PRESET_MARGIN_PERCENT, bottom_y),
        "bottom-right": (right_x, bottom_y),
    }.get(corner, (right_x, PRESET_MARGIN_PERCENT))

def resolve_overlay_percent(item: dict, size_percent: float) -> tuple:
    """x_percent/y_percent (free placement) if set, else derived from the
    legacy 4-preset `corner` field — every overlay/logo ends up with a
    concrete (x, y) in 0-100 regardless of which one it was actually saved
    with, so the rest of the pipeline only has to deal with one shape."""
    x = item.get("x_percent")
    y = item.get("y_percent")
    if x is not None and y is not None:
        return x, y
    return _preset_xy(item.get("corner"), size_percent)

def _overlay_xy_expr(x_percent: float, y_percent: float) -> tuple:
    """ffmpeg overlay x/y expressions placing the overlay's own top-left
    directly at x_percent/y_percent of the frame — no implicit margin, so a
    value of 0 or 100 (or, if someone deliberately drags a slider past that,
    <0 / >100) can push the image flush to an edge or partly off-frame,
    matching the frontend's direct-mapping preview math (overlayPositionStyle
    in App.jsx) exactly. Any safety margin only comes from *how a value was
    chosen* (the 4 quick-position presets bake one in, see _preset_xy above)
    — never clamped back in here once a creator has set a value."""
    x_expr = f"(W*{x_percent}/100)"
    y_expr = f"(H*{y_percent}/100)"
    return x_expr, y_expr

def check_ffmpeg_filter(filter_name: str) -> bool:
    """Checks if a specific filter is supported by system ffmpeg."""
    res = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True)
    if res.returncode != 0:
        logger.warning(f"Unable to inspect FFmpeg filters: {res.stderr.strip()}")
        return False
    # Match the filter-name column, not an arbitrary occurrence in a filter
    # description. A substring match can report a false positive and make the
    # pipeline skip its Pillow fallback even though FFmpeg cannot burn ASS.
    return re.search(rf"^\s*[.A-Z|]{{3}}\s+{re.escape(filter_name)}\s", res.stdout, re.MULTILINE) is not None


def _ass_has_dialogue(subtitle_ass_path: Path) -> bool:
    """Return whether an ASS file contains at least one renderable event."""
    try:
        return any(
            line.lstrip().startswith("Dialogue:")
            for line in subtitle_ass_path.read_text(encoding="utf-8-sig").splitlines()
        )
    except (OSError, UnicodeError):
        return False

def assemble_final_video(
    clip_paths: List[Path],
    audio_path: Path,
    subtitle_ass_path: Path,
    output_path: Path,
    effects_config: Optional[Dict[str, Any]] = None,
    branding_config: Optional[Dict[str, Any]] = None,
    clip_durations: Optional[List[float]] = None,
    subtitle_style: Optional[Dict[str, Any]] = None,
    scene_energy: Optional[List[float]] = None,
    editing_profile: Optional[Dict[str, Any]] = None,
    subtitles_preburned: bool = False,
) -> Path:
    """
    Joins motion clips (crossfading between them when durations are known so
    scene changes feel dynamic rather than hard-cut), applies color grading/
    grain, burns subtitles, places the channel logo and any extra sticker
    overlays (creator-configurable corner + size each), and multiplexes audio.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    effects = effects_config or {}
    branding = branding_config or {}
    sub_style = subtitle_style or {}

    video_filters = []

    # Color grading + overlay effects are both gated behind effects_config.enabled —
    # a client can turn the whole "effects" layer off without losing their tuned
    # color grade / intensity settings underneath (they just don't apply for now).
    effects_enabled = effects.get("enabled", True)

    # Every grade below is a real, tested ffmpeg filter chain (eq/colorbalance/
    # colorchannelmixer/hue) — no placeholder options that don't actually change
    # the render.
    color_mode = effects.get("color_grade", "none") if effects_enabled else "none"
    if color_mode == "warm":
        video_filters.append("eq=gamma=1.05:saturation=1.15")
    elif color_mode == "vintage":
        video_filters.append("colorbalance=rs=0.1:gs=-0.05:bs=-0.1,eq=saturation=0.85")
    elif color_mode == "dramatic":
        video_filters.append("eq=contrast=1.2:saturation=0.9")
    elif color_mode == "cool":
        video_filters.append("colorbalance=rs=-0.08:gs=0.02:bs=0.15,eq=saturation=1.05")
    elif color_mode == "noir":
        video_filters.append("hue=s=0,eq=contrast=1.25:brightness=-0.02")
    elif color_mode == "sepia":
        video_filters.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131:0")
    elif color_mode == "vibrant":
        video_filters.append("eq=saturation=1.5:contrast=1.1")
    elif color_mode == "faded":
        video_filters.append("eq=contrast=0.82:brightness=0.05:saturation=0.85")
    elif color_mode == "cinematic":
        # Teal shadows / orange highlights — the classic blockbuster grade.
        video_filters.append("colorbalance=rs=0.15:bs=-0.15:rh=-0.05:bh=0.1,eq=contrast=1.1:saturation=1.05")

    # Overlay effects — textures layered on top of the whole video, distinct from
    # the color grade above. A channel can combine any number of them at once.
    # "overlay_effects" is the current field (a list); falls back to the old
    # single-choice "overlay_effect" string, and further back to the original
    # boolean "grain" flag, for channels saved before either existed.
    # grain_intensity/vignette_intensity (0-100, default 50) scale how strong
    # each one is.
    overlay_effects = effects.get("overlay_effects")
    if overlay_effects is None:
        legacy = effects.get("overlay_effect")
        if legacy is None:
            legacy = "grain" if effects.get("grain", True) else "none"
        overlay_effects = {
            "none": [], "grain": ["grain"], "white_noise": ["white_noise"],
            "vignette": ["vignette"], "grain_vignette": ["grain", "vignette"],
        }.get(legacy, [])
    if not effects_enabled:
        overlay_effects = []

    grain_frac = max(0, min(100, effects.get("grain_intensity", 50))) / 100
    vignette_frac = max(0, min(100, effects.get("vignette_intensity", 50))) / 100
    particle_frac = _bounded_number(effects.get("particle_intensity", 50), 50, 0, 100) / 100
    active_particles = [pid for pid in ("stars", "dust", "snow", "rain", "sparks") if pid in overlay_effects and PARTICLE_ASSETS[pid].exists()]
    particle_layers, _particle_density = _build_particle_layers(effects, active_particles)
    particle_size = _bounded_number(effects.get("particle_size", 50), 50, 0, 100)
    particle_speed = _bounded_number(effects.get("particle_speed", 50), 50, 0, 100)
    particle_dispersion = _bounded_number(effects.get("particle_dispersion", 50), 50, 0, 100)
    particle_direction = effects.get("particle_direction", "auto")
    if particle_direction not in {"auto", "up", "down", "left", "right"}:
        particle_direction = "auto"

    # alls ranges chosen so 50% lands near the old fixed defaults (8 / 22)
    grain_alls = round(2 + grain_frac * 28)
    white_noise_alls = round(4 + grain_frac * 46)
    # ffmpeg's vignette "angle": smaller = stronger. PI/4 was the old fixed
    # default (~50%); sweep from a barely-there PI/2.2 up to a heavy PI/9.
    vignette_angle = (math.pi / 2.2) - (math.pi / 2.2 - math.pi / 9) * vignette_frac

    # grain and white_noise both drive ffmpeg's "noise" filter — applying both
    # would just double up the same texture, so grain wins if both are picked.
    if "grain" in overlay_effects:
        video_filters.append(f"noise=alls={grain_alls}:allf=t+u")
    elif "white_noise" in overlay_effects:
        video_filters.append(f"noise=alls={white_noise_alls}:allf=t+u")
    if "vignette" in overlay_effects:
        video_filters.append(f"vignette={vignette_angle:.4f}")

    # Extra textures — each a real, independent, linearly-chainable ffmpeg
    # filter (no fake/no-op options): fixed-strength since these are meant as
    # quick stylistic toggles rather than sliders like grain/vignette above.
    if "chromatic_aberration" in overlay_effects:
        # Shifts the red/blue channels apart horizontally — the classic lens/VHS fringe look.
        video_filters.append("rgbashift=rh=3:bh=-3")
    if "old_film" in overlay_effects:
        # Composite: heavy grain + tight vignette + desaturation, like scanned archival footage.
        video_filters.append("noise=alls=22:allf=t+u,vignette=0.35,eq=saturation=0.7:contrast=1.05")
    if "flicker" in overlay_effects:
        # Subtle time-varying brightness pulse — projector/old-film flicker.
        video_filters.append("eq=eval=frame:brightness='0.035*sin(2*PI*t*3)'")
    if "soft_focus" in overlay_effects:
        # Gentle blur only (no blend) — a cheap, real "dreamy" filmic softness.
        video_filters.append("gblur=sigma=1.4")
    if "sharpen" in overlay_effects:
        # Crisper "HD clarity" look — the opposite end of the texture spectrum from grain.
        video_filters.append("unsharp=5:5:0.8:5:5:0.0")

    # Atmospheric and genre effects. These use native FFmpeg filters only, so
    # they remain available on every renderer without downloading overlay
    # footage. Strength follows the shared particle/atmosphere slider.
    particle_noise = round(3 + particle_frac * 25)
    if "black_noise" in overlay_effects:
        video_filters.append(f"noise=alls={round(5 + particle_frac * 35)}:allf=t+u,eq=brightness={-(0.015 + particle_frac * 0.045):.3f}")
    # stars/dust/snow/rain/sparks used to be abstract noise+brightness tweaks
    # here — they never actually looked like their name, just a vague film
    # grain. They're now real animated overlay clips (assets/particles/*.mp4,
    # see generate_particle_assets.py) composited in _apply_particles_step
    # below with a screen blend, same trick the wizard's CSS preview already
    # uses. fog stays a pure blur/brightness filter — a haze genuinely is a
    # blur, not discrete particles, so the old approach already suited it.
    if "fog" in overlay_effects:
        video_filters.append(f"gblur=sigma={0.4 + particle_frac * 1.2:.2f},eq=brightness={0.025 + particle_frac * 0.045:.3f}:contrast={1.0 - particle_frac * 0.12:.3f}:saturation=0.88")
    if "light_leak" in overlay_effects:
        video_filters.append(f"colorbalance=rh={0.05 + particle_frac * 0.18:.3f}:gh={0.02 + particle_frac * 0.06:.3f}:bs=-0.04,eq=brightness={particle_frac * 0.025:.3f}:saturation=1.08")
    if "dream_glow" in overlay_effects:
        video_filters.append(f"gblur=sigma={0.6 + particle_frac * 1.8:.2f},eq=brightness={0.015 + particle_frac * 0.035:.3f}:saturation=1.05")
    if "horror" in overlay_effects:
        video_filters.append("colorbalance=rs=0.10:gs=-0.10:bs=-0.08,eq=contrast=1.22:brightness=-0.06:saturation=0.72,vignette=0.32")
    if "vhs_glitch" in overlay_effects:
        video_filters.append(f"rgbashift=rh={round(2 + particle_frac * 5)}:bh=-{round(2 + particle_frac * 5)},noise=alls={max(6, particle_noise // 2)}:allf=t+u,eq=contrast=1.08:saturation=0.9")
    if "film_scratches" in overlay_effects:
        video_filters.append(f"noise=alls={round(10 + particle_frac * 22)}:allf=t,eq=saturation=0.65:contrast=1.08")

    # Check if FFmpeg build has libass 'subtitles' filter, and whether the client
    # wants subtitles burned in at all (subtitle_style.enabled, default True).
    # Kept as its own filter string (not appended into video_filters) so it can
    # be positioned independently in the compositing order below — e.g. a
    # creator who wants the watermark or logo painted *under* the subtitles
    # instead of over them.
    subtitles_enabled = sub_style.get("enabled", True)
    if subtitles_enabled and not subtitles_preburned and not _ass_has_dialogue(subtitle_ass_path):
        raise RuntimeError(
            f"Sous-titres activés, mais le fichier ASS est absent ou ne contient aucune ligne: {subtitle_ass_path}"
        )

    # `ass` is purpose-built for ASS input. Keep `subtitles` as a compatible
    # fallback for FFmpeg builds exposing only that libass entry point.
    subtitle_filter_name = None
    if subtitles_enabled and not subtitles_preburned:
        if check_ffmpeg_filter("ass"):
            subtitle_filter_name = "ass"
        elif check_ffmpeg_filter("subtitles"):
            subtitle_filter_name = "subtitles"
    subtitles_filter_str = None
    if subtitle_filter_name:
        ass_path_escaped = str(subtitle_ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        subtitles_filter_str = f"{subtitle_filter_name}=filename='{ass_path_escaped}'"
    elif subtitles_enabled and not subtitles_preburned:
        raise RuntimeError(
            "Sous-titres activés, mais ce serveur FFmpeg ne possède ni le filtre 'ass' ni le filtre 'subtitles' (libass). "
            "Rendu interrompu pour éviter de produire silencieusement un MP4 sans sous-titres."
        )

    effects_vf_string = ",".join(video_filters) if video_filters else None

    # Every image overlay burned into the render — the channel logo plus any
    # extra creator-added stickers (e.g. a "Subscribe" button or bell icon,
    # the kind of thing creators used to paste on by hand) — collected into
    # one list so they all share the same corner/size/opacity compositing
    # logic instead of the old logo-only hardcoded 100x100 top-right block.
    # logo_path/overlay image_path are stored storage-relative
    # ("channels/<id>/logo.png") — must be resolved against STORAGE_PATH, not
    # treated as relative to the process's cwd (which silently made has_logo
    # False for every channel, however the logo was set).
    image_overlays = []  # each: {"path": Path, "corner": str, "size_percent": float, "opacity": float}

    logo_path_str = branding.get("logo_path")
    logo_full_path = (STORAGE_PATH / logo_path_str) if logo_path_str else None
    has_logo = bool(branding.get("logo_enabled", True) and logo_full_path and logo_full_path.exists())
    if has_logo:
        logo_shape = branding.get("logo_shape") or "rectangle"
        # 14% roughly matches a real YouTube channel bug/watermark's on-screen
        # size — the old 9% default read as a barely-visible sticker, nothing
        # like what channels actually brand their videos with.
        logo_size = branding.get("logo_size_percent") or 14
        logo_x, logo_y = resolve_overlay_percent({"x_percent": branding.get("logo_x_percent"), "y_percent": branding.get("logo_y_percent"), "corner": branding.get("logo_corner")}, logo_size)
        image_overlays.append({
            "path": apply_overlay_shape_mask(logo_full_path, logo_shape, temp_dir, "logo"),
            "x_percent": logo_x,
            "y_percent": logo_y,
            "size_percent": branding.get("logo_size_percent") or 14,
            "opacity": 1.0,
        })

    for item in (branding.get("overlays") or []):
        if not item.get("enabled", True):
            continue
        item_path_str = item.get("image_path")
        if not item_path_str:
            continue
        item_full_path = STORAGE_PATH / item_path_str
        if not item_full_path.exists():
            continue
        item_shape = item.get("shape") or "rectangle"
        item_size = item.get("size_percent") or 10
        item_x, item_y = resolve_overlay_percent(item, item_size)
        image_overlays.append({
            "path": apply_overlay_shape_mask(item_full_path, item_shape, temp_dir, item.get("id") or item_path_str),
            "x_percent": item_x,
            "y_percent": item_y,
            "size_percent": item.get("size_percent") or 10,
            "opacity": item.get("opacity") if item.get("opacity") is not None else 1.0,
        })

    # Free-tier NicheCut watermark. The official horizontal logo is deliberately
    # large and centered: a corner mark can be removed with a trivial crop or
    # covered by another logo. Paid plans disable it through watermark_enabled.
    has_watermark = bool(effects.get("watermark_enabled", True) and WATERMARK_PATH.exists())

    # Crossfade chain needs each clip's real duration to compute cumulative
    # xfade offsets, and opens every clip as a simultaneous ffmpeg input to
    # build the filter graph — on a memory-constrained shared box that OOM-
    # kills the process for long videos with many scenes (confirmed in
    # production: a 40+ clip render was killed by the Linux OOM killer).
    # Cap it to short/medium videos; long ones fall back to a plain hard-cut
    # concat, which streams clips sequentially instead of holding them all
    # open in memory at once.
    # 58 simultaneous 1080p inputs were enough to kill FFmpeg under the
    # worker's 4 GiB container limit before it could emit an actual filter
    # error. Keep dissolves for short videos, but protect long renders by
    # streaming their clips through the concat demuxer instead.
    MAX_CLIPS_FOR_XFADE = 20
    use_xfade = (
        clip_durations is not None
        and len(clip_durations) == len(clip_paths)
        and 2 <= len(clip_paths) <= MAX_CLIPS_FOR_XFADE
        and all(d > XFADE_DURATION * 2.2 for d in clip_durations)
    )

    concat_list_file = temp_dir / "clips_concat.txt"
    # Without this, the concat demuxer can leave PTS discontinuities between
    # clips that make ffmpeg write a wrong (too-short) duration into the
    # output's moov atom — browsers then report the wrong video.duration,
    # which breaks seeking/skip controls near the end of the real content.
    cmd = ["ffmpeg", "-y", "-fflags", "+genpts"]

    if use_xfade:
        for clip in clip_paths:
            cmd.extend(["-i", str(clip.resolve())])
        n = len(clip_paths)
        chain = []
        cumulative = clip_durations[0]
        prev_label = "0:v"
        for i in range(1, n):
            # Calm narration gets an unhurried dissolve; energetic passages
            # receive a tighter fade that lands closer to the spoken accent.
            from src.pipeline.editing_direction import transition_for_energy
            energy = scene_energy[i] if scene_energy and i < len(scene_energy) else 0.5
            profile = editing_profile or {
                "transition_min": 0.55, "transition_max": 1.25,
                "transitions": ("dissolve", "fade"),
            }
            transition, transition_duration = transition_for_energy(profile, energy)
            offset = cumulative - transition_duration
            out_label = f"vx{i}" if i < n - 1 else "v_joined"
            chain.append(
                f"[{prev_label}][{i}:v]xfade=transition={transition}:duration={transition_duration:.3f}:offset={offset:.3f}[{out_label}]"
            )
            cumulative += clip_durations[i] - transition_duration
            prev_label = out_label
        video_chain = ";".join(chain)
    else:
        with open(concat_list_file, "w", encoding="utf-8") as f:
            for clip in clip_paths:
                clean_path = str(clip.resolve()).replace("'", "'\\''")
                f.write(f"file '{clean_path}'\n")
        cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_list_file)])

    for ov in image_overlays:
        cmd.extend(["-i", str(ov["path"].resolve())])
    for layer in particle_layers:
        # -stream_loop -1 repeats the (few-second) clip indefinitely; -shortest
        # on the final output mux is what actually stops it once the real
        # video/audio end, so it never has to be trimmed to an exact duration.
        cmd.extend(["-stream_loop", "-1", "-i", str(PARTICLE_ASSETS[layer["id"]].resolve())])
    if has_watermark:
        cmd.extend(["-i", str(WATERMARK_PATH.resolve())])

    cmd.extend(["-i", str(audio_path.resolve())])
    audio_input_index = (len(clip_paths) if use_xfade else 1) + len(image_overlays) + len(particle_layers) + (1 if has_watermark else 0)

    filter_parts = [video_chain] if use_xfade else []
    base_label = "v_joined" if use_xfade else "0:v"
    overlay_base_index = len(clip_paths) if use_xfade else 1
    particles_base_index = overlay_base_index + len(image_overlays)
    watermark_index = particles_base_index + len(particle_layers)

    def _apply_effects_step():
        nonlocal base_label
        if effects_vf_string:
            filter_parts.append(f"[{base_label}]{effects_vf_string}[v_eff]")
            base_label = "v_eff"
        # Layered, slightly offset particle fields create a natural sense of
        # depth instead of a single, obvious stock texture over the frame.
        size_factor = 0.76 + (particle_size / 100) * 0.62
        speed_factor = 0.55 + (particle_speed / 100) * 1.45
        spread_factor = particle_dispersion / 100
        direction_filter = {
            "auto": "", "down": "", "up": ",vflip",
            "left": ",transpose=1", "right": ",transpose=2",
        }[particle_direction]
        for i, layer in enumerate(particle_layers):
            idx = particles_base_index + i
            scaled_label = f"part{i}"
            depth = layer["depth"]
            layer_scale = size_factor * (0.88 + depth * 0.16)
            scaled_w = max(64, round(1920 * layer_scale))
            scaled_h = max(64, round(1080 * layer_scale))
            if layer_scale >= 1:
                extra_x = max(0, scaled_w - 1920)
                extra_y = max(0, scaled_h - 1080)
                x = round(extra_x * ((depth + 1) / (layer["copies"] + 1)) * spread_factor)
                y = round(extra_y * ((layer["copies"] - depth) / (layer["copies"] + 1)) * spread_factor)
                framing = f"crop=1920:1080:{x}:{y}"
            else:
                framing = "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black"
            filter_parts.append(
                f"[{idx}:v]setpts=PTS/{speed_factor:.3f}{direction_filter},"
                f"scale={scaled_w}:{scaled_h},{framing}[{scaled_label}]"
            )
            out_label = f"v_part{i}"
            # One layer retains the previous strength. Added depth layers are
            # attenuated so density adds atmosphere rather than a white veil.
            layer_opacity = particle_frac / (1 if layer["copies"] == 1 else layer["copies"] ** 0.68)
            filter_parts.append(f"[{base_label}][{scaled_label}]blend=all_mode=screen:all_opacity={layer_opacity:.3f}[{out_label}]")
            base_label = out_label

    def _apply_subtitles_step():
        nonlocal base_label
        if subtitles_filter_str:
            filter_parts.append(f"[{base_label}]{subtitles_filter_str}[v_sub]")
            base_label = "v_sub"

    def _apply_logo_step():
        nonlocal base_label
        for i, ov in enumerate(image_overlays):
            idx = overlay_base_index + i
            # Aspect-preserving scale by the configured % of the 1920px-wide
            # frame, not the old forced 100x100 square crop — arbitrary stickers
            # (a "Subscribe" button, a bell icon) aren't square like a logo often is.
            size_px = max(24, round(1920 * (ov["size_percent"] / 100)))
            label = f"ov{i}"
            scale_filter = f"[{idx}:v]scale={size_px}:-1"
            if ov["opacity"] < 1.0:
                scale_filter += f",format=rgba,colorchannelmixer=aa={ov['opacity']:.2f}"
            scale_filter += f"[{label}]"
            filter_parts.append(scale_filter)
            x_expr, y_expr = _overlay_xy_expr(ov["x_percent"], ov["y_percent"])
            out_label = f"v_ov{i}"
            filter_parts.append(f"[{base_label}][{label}]overlay={x_expr}:{y_expr}[{out_label}]")
            base_label = out_label

    def _apply_watermark_step():
        nonlocal base_label
        if has_watermark:
            # Roughly 47% of a 1920px frame. Opacity raised from 0.14 to 0.22 so it
            # actually reads as sitting on top of the subtitles instead of getting
            # visually lost behind their bold, high-contrast text.
            filter_parts.append(f"[{watermark_index}:v]scale=900:-1,format=rgba,colorchannelmixer=aa=0.22[wm]")
            filter_parts.append(f"[{base_label}][wm]overlay=(W-w)/2:(H-h)/2[v_wm]")
            base_label = "v_wm"

    # Compositing order: creators can drag-reorder "Calques" (effets, sous-titres,
    # logo/incrustations, filigrane) in the pipeline wizard's preview — that
    # choice is persisted as effects_config.layer_order (the full 7-id wizard
    # list; only these 4 actually paint anything here, so it's filtered down to
    # them, keeping whatever relative order the creator chose). The default
    # (['effects', 'subtitles', 'logo', 'watermark']) matches this function's
    # original hardcoded order exactly, so a channel that never touched
    # reordering renders pixel-identical to before this feature existed.
    DEFAULT_LAYER_ORDER = ["effects", "subtitles", "logo", "watermark"]
    STEP_FNS = {
        "effects": _apply_effects_step,
        "subtitles": _apply_subtitles_step,
        "logo": _apply_logo_step,
        "watermark": _apply_watermark_step,
    }
    saved_order = [lid for lid in (effects.get("layer_order") or []) if lid in STEP_FNS]
    layer_order = saved_order if set(saved_order) == set(DEFAULT_LAYER_ORDER) else DEFAULT_LAYER_ORDER
    for layer_id in layer_order:
        STEP_FNS[layer_id]()

    if filter_parts:
        cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", f"[{base_label}]", "-map", f"{audio_input_index}:a"])
    else:
        cmd.extend(["-vf", "null", "-map", "0:v", "-map", "1:a"])

    # Encode to a temp file first and atomically rename into place only on success.
    # Prevents a killed/crashed process (e.g. server restart mid-render) from leaving
    # a truncated MP4 (missing moov atom) sitting at output_path with status "done".
    temp_output_path = output_path.with_name(f".{output_path.stem}.part.mp4")

    cmd.extend([
        "-c:v", "libx264",
        # xfade can negotiate yuv444p internally; force the widely supported
        # 4:2:0 delivery format at the encoder boundary instead of producing
        # a much heavier High 4:4:4 output.
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        # Capped average bitrate instead of plain CRF: CRF's output size is
        # content-dependent and unpredictable — the grain/noise overlay filter
        # in particular adds high-frequency detail that's expensive to encode,
        # which is how a 30min render ended up at 11.6GB under CRF alone.
        # 4200k video + 128k audio ≈ 4.33 Mbps → a 30min video lands around
        # ~975MB, comfortably under a 1GB budget regardless of content.
        "-b:v", "4200k",
        "-maxrate", "4600k",
        "-bufsize", "9200k",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(temp_output_path)
    ])

    logger.info("Assembling final MP4 video with FFmpeg...")
    try:
        run_ffmpeg(cmd)
        os.replace(temp_output_path, output_path)
    finally:
        if temp_output_path.exists():
            temp_output_path.unlink()

    if concat_list_file.exists():
        concat_list_file.unlink(missing_ok=True)

    logger.info(f"Final video successfully generated: {output_path}")
    return output_path
