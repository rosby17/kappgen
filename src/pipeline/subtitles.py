import math
import subprocess
from pathlib import Path
from typing import Dict, Any, List
from PIL import Image, ImageDraw, ImageFont
from src.utils.logger import logger

_DEJAVU_BOLD_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def _resolve_font_file(font_name: str) -> str:
    """
    PIL's ImageFont.truetype() needs an actual font FILE path, not a family
    name — unlike libass (used by the ASS/subtitles ffmpeg filter), it can't
    look a name up via fontconfig on its own. Ask fontconfig (`fc-match`,
    always available alongside the many `fonts-*` packages this image
    installs) to resolve the client's configured family to the real .ttf/.otf
    file. Falls back to DejaVu Sans Bold (bundled via fonts-dejavu-core) if
    fontconfig can't be reached or returns nothing usable — better than a
    silent switch to PIL's tiny built-in bitmap font, which ignores the
    configured size/position entirely.
    """
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{file}", font_name or "sans-serif"],
            capture_output=True, text=True, timeout=5
        )
        path = result.stdout.strip()
        if path and Path(path).exists():
            return path
    except Exception as e:
        logger.warning(f"fc-match failed resolving font '{font_name}': {e}")
    return _DEJAVU_BOLD_FALLBACK

def to_ass_color(color: str, default: str = "&H00FFFFFF", opacity: float = 100) -> str:
    """
    Converts a web hex color ("#RRGGBB" or "#RGB", as produced by an HTML
    <input type="color">) to ASS's &HAABBGGRR format. Values already in ASS
    format (starting with &H) pass through unchanged (opacity is ignored in
    that case — it's already baked into the AA byte).
    opacity is 0-100; ASS alpha is inverted (00 = opaque, FF = fully clear).
    "transparent" (what the "Aucune couleur" picker sends) must resolve to
    fully-clear here, not fall through to `default` — `default` is meant for
    a genuinely missing/invalid value, but str "transparent" was landing on
    that same branch and coming out as opaque black, so picking "Aucune" for
    the subtitle outline silently kept rendering a solid black stroke.
    """
    if not color:
        return default
    color = color.strip()
    if color.lower() == "transparent":
        return "&HFF000000"
    if color.upper().startswith("&H"):
        return color
    if color.startswith("#"):
        hex_part = color[1:]
        if len(hex_part) == 3:
            hex_part = "".join(c * 2 for c in hex_part)
        if len(hex_part) == 6:
            try:
                r, g, b = hex_part[0:2], hex_part[2:4], hex_part[4:6]
                int(r + g + b, 16)  # validates it's real hex
                alpha = int(round((100 - max(0, min(100, opacity))) / 100 * 255))
                return f"&H{alpha:02X}{b}{g}{r}".upper()
            except ValueError:
                pass
    return default


def format_ass_time(seconds: float) -> str:
    """Formats float seconds to ASS timestamp format (H:MM:SS.cs)."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"

def apply_text_case(text: str, case_mode: str) -> str:
    if case_mode == "upper":
        return text.upper()
    if case_mode == "lower":
        return text.lower()
    if case_mode == "capitalize":
        return " ".join(w[:1].upper() + w[1:] for w in text.split(" ") if w)
    return text


def _paragraph_chunks(words: List[Dict[str, Any]], max_seconds: int, max_words: int) -> List[List[Dict[str, Any]]]:
    """Group timed words into persistent blocks, respecting both user limits."""
    chunks = []
    current = []
    for word in words:
        if current and (
            len(current) >= max_words
            or float(word.get("end", 0)) - float(current[0].get("start", 0)) > max_seconds
        ):
            chunks.append(current)
            current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks


def _wrap_paragraph(words: List[str], words_per_line: int) -> str:
    return r"\N".join(
        " ".join(words[i:i + words_per_line])
        for i in range(0, len(words), words_per_line)
    )


def _measure_text_block(lines: List[str], font_path: str, font_size: int, bold: bool, letter_spacing: float) -> tuple:
    """Pixel size (width, height) of a possibly multi-line subtitle chunk, used
    to size the background box so it hugs the actual text instead of a fixed
    guess. Approximate on two fronts PIL can't match libass exactly on — no
    letter-spacing support (added back manually per line) and no bold variant
    of the resolved font file (compensated with a small width multiplier) —
    fine for a box that only needs to comfortably contain the text, not pixel-
    perfect kerning."""
    try:
        font = ImageFont.truetype(font_path, font_size)
    except (IOError, OSError):
        font = ImageFont.truetype(_DEJAVU_BOLD_FALLBACK, font_size)
    dummy_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bold_factor = 1.06 if bold else 1.0
    max_w = 0.0
    for line in lines:
        bbox = dummy_draw.textbbox((0, 0), line or " ", font=font)
        w = (bbox[2] - bbox[0]) * bold_factor + max(0, len(line) - 1) * letter_spacing
        max_w = max(max_w, w)
    ascent, descent = font.getmetrics()
    line_h = (ascent + descent) * 1.15
    return max_w, line_h * max(1, len(lines))


def _rounded_rect_ass_path(w: float, h: float, radius: float) -> str:
    """ASS vector-drawing (\\p1) path for a w×h rectangle with corner radius
    `radius`, corners approximated with cubic beziers (kappa ≈ 0.5523, the
    standard constant for a bezier quarter-circle). radius<=0 draws a plain
    sharp-cornered rectangle."""
    r = max(0, min(radius, w / 2, h / 2))
    if r < 1:
        return f"m 0 0 l {w:.0f} 0 l {w:.0f} {h:.0f} l 0 {h:.0f}"
    k = r * 0.5523
    return (
        f"m {r:.0f} 0 "
        f"l {w - r:.0f} 0 "
        f"b {w - r + k:.0f} 0 {w:.0f} {r - k:.0f} {w:.0f} {r:.0f} "
        f"l {w:.0f} {h - r:.0f} "
        f"b {w:.0f} {h - r + k:.0f} {w - r + k:.0f} {h:.0f} {w - r:.0f} {h:.0f} "
        f"l {r:.0f} {h:.0f} "
        f"b {r - k:.0f} {h:.0f} 0 {h - r + k:.0f} 0 {h - r:.0f} "
        f"l 0 {r:.0f} "
        f"b 0 {r - k:.0f} {r - k:.0f} 0 {r:.0f} 0"
    )


def _split_ass_alpha_color(ass_color: str) -> tuple:
    """"&HAABBGGRR" -> ("AA", "BBGGRR") for use in inline \\1a/\\1c override
    tags, which take alpha and color as two separate values (unlike the
    combined form used in the [V4+ Styles] table)."""
    hexpart = ass_color[2:] if ass_color.upper().startswith("&H") else "00000000"
    hexpart = hexpart.rstrip("&")
    return hexpart[0:2] or "00", hexpart[2:8] or "000000"


def generate_ass_subtitles(
    transcript_info: Dict[str, Any],
    style_config: Dict[str, Any],
    output_ass_path: Path
) -> Path:
    """
    Generates an Advanced SubStation Alpha (.ass) subtitle file.

    highlight_mode controls how the accent color is used:
      - "word": each word is colored with the accent only while it's being
        spoken (one Dialogue event per word, full chunk text, only the
        active word carries a color override) — the classic karaoke look.
      - "line": the whole chunk is displayed in the accent color the entire
        time it's on screen, no per-word timing needed.
      - "none": plain base-color text, no accent used at all.
    Falls back to the old boolean "karaoke" flag ("word" vs "line") for
    channels saved before highlight_mode existed.
    """
    font_name = style_config.get("font", "Arial")
    font_size = style_config.get("size", 44)
    opacity = style_config.get("opacity", 100)
    base_color_hex = style_config.get("base_color") or "#FFFFFF"
    accent_color_hex = style_config.get("color") or "#FFD700"
    base_color = to_ass_color(base_color_hex, "&H00FFFFFF", opacity)
    accent_color = to_ass_color(accent_color_hex, "&H0000D7FF", opacity)
    outline_color = to_ass_color(style_config.get("outline_color"), "&H00000000", opacity)  # Black
    outline_width = style_config.get("outline_width", 3)
    position = str(style_config.get("position", "bottom")).strip().lower()
    align_h = str(style_config.get("align") or "center").strip().lower()
    text_case = str(style_config.get("text_case") or "none").strip().lower()
    bold = -1 if style_config.get("bold") else 0
    italic = -1 if style_config.get("italic") else 0
    letter_spacing = style_config.get("letter_spacing", 0)
    rotation = style_config.get("rotation", 0)
    x_offset = style_config.get("x_offset", 0)  # extra horizontal nudge, in px at 1920 width
    y_offset = style_config.get("y_offset", 0)  # extra vertical nudge, in px at 1080 height

    has_shadow = bool(style_config.get("shadow"))
    shadow_color_hex = style_config.get("shadow_color") or "#000000"
    shadow_distance = style_config.get("shadow_distance", 3) if has_shadow else 0

    subtitle_mode = style_config.get("subtitle_mode", "dynamic")
    highlight_mode = style_config.get("highlight_mode")
    if highlight_mode not in ("word", "line", "none"):
        highlight_mode = "word" if style_config.get("karaoke", True) else "line"

    # A background box behind the text is drawn ourselves as a separate ASS
    # vector shape (\p1), not via BorderStyle 3 (libass's built-in "opaque
    # box" mode) — BorderStyle 3 can only produce a sharp rectangle sized to
    # text+uniform padding, with no independent width/height and no rounded
    # corners. Drawing it manually lets width, height (via independent
    # x/y padding) and corner radius all be set separately, and keeps it
    # fully decoupled from the text's own outline/shadow styling instead of
    # replacing them.
    box_color_raw = str(style_config.get("box_color") or "").strip()
    has_box = bool(box_color_raw) and box_color_raw.lower() != "transparent"
    legacy_padding = max(0, int(style_config.get("box_padding", 10)))
    box_padding_x = max(0, int(style_config.get("box_padding_x", legacy_padding)))
    box_padding_y = max(0, int(style_config.get("box_padding_y", legacy_padding)))
    # 0 = square corners — matches every box ever actually rendered before
    # this option existed (BorderStyle 3 never had rounding), even though the
    # editor's live preview drew a rounded corner via CSS the real render
    # couldn't produce.
    box_radius = max(0, int(style_config.get("box_radius", 0)))

    # Alignment is a single ASS numpad value (1-9) combining vertical position
    # (bottom/center/top row) with horizontal alignment (left/center/right column):
    # bottom row = 1,2,3 (base 0); middle row = 4,5,6 (base 3); top row = 7,8,9
    # (base 6). The middle row's base was wrongly 4 instead of 3, which shifted
    # every "center" position one column right (e.g. center+center rendered as
    # middle-right instead of true middle-center).
    row = {"bottom": 0, "center": 3, "top": 6}.get(position, 0)
    col = {"left": 1, "center": 2, "right": 3}.get(align_h, 2)
    alignment = row + col
    # Keep lower subtitles inside YouTube's safe area so player controls do
    # not cover them. Top subtitles also retain a comfortable screen margin.
    margin_v = 150 if position == "bottom" else (80 if position == "top" else 0)

    border_style = 1
    outline_value = outline_width
    back_color = to_ass_color(shadow_color_hex, "&H80000000", opacity) if has_shadow else "&H80000000"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{base_color},{accent_color},{outline_color},{back_color},{bold},{italic},0,0,100,100,{letter_spacing},0,{border_style},{outline_value},{shadow_distance},{alignment},20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # \pos() pins the line at an exact point instead of relying on margins.
    # Normally only engaged for a manual x/y nudge — but a background box
    # needs it unconditionally: the box is a *separate* dialogue event from
    # the text, and the only way to guarantee both anchor to the exact same
    # point (so the box stays centered under text of any width) is to give
    # them an identical explicit \pos + the same style Alignment corner, and
    # let libass anchor each one's own bounding box (text's auto-measured;
    # the box's the drawn shape) to that corner the same way.
    pos_tag = ""
    if x_offset or y_offset or has_box:
        px = 960 + (col - 2) * 860 + x_offset
        py = {0: 900, 3: 540, 6: 100}[row] + y_offset
        pos_tag = f"\\pos({px},{py})"
    rotate_tag = f"\\frz{rotation}" if rotation else ""
    prefix_tags = pos_tag + rotate_tag
    text_layer = 1 if has_box else 0  # box sits on layer 0, text always drawn on top of it

    font_path = _resolve_font_file(font_name)
    box_alpha_hex, box_bgr_hex = _split_ass_alpha_color(to_ass_color(box_color_raw, "&H80000000", opacity)) if has_box else (None, None)

    def _box_event(start_str: str, end_str: str, lines: List[str]) -> str:
        text_w, text_h = _measure_text_block(lines, font_path, font_size, bool(style_config.get("bold")), letter_spacing)
        box_w = text_w + box_padding_x * 2
        box_h = text_h + box_padding_y * 2
        path = _rounded_rect_ass_path(box_w, box_h, box_radius)
        tags = f"{{{prefix_tags}\\1a&H{box_alpha_hex}&\\1c&H{box_bgr_hex}&\\bord0\\shad0\\p1}}"
        return f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{tags}{path}{{\\p0}}"

    words = transcript_info.get("words", [])
    events = []

    if words and subtitle_mode == "paragraph":
        max_seconds = max(5, min(120, int(style_config.get("paragraph_duration_seconds") or 45)))
        max_words = max(10, min(250, int(style_config.get("paragraph_max_words") or 90)))
        line_words = max(3, min(20, int(style_config.get("paragraph_words_per_line") or 10)))
        for chunk in _paragraph_chunks(words, max_seconds, max_words):
            cleaned = [apply_text_case(w["word"].replace("{", "").replace("}", ""), text_case) for w in chunk]
            paragraph_text = _wrap_paragraph(cleaned, line_words)
            start_str = format_ass_time(chunk[0]["start"])
            end_str = format_ass_time(chunk[-1]["end"])
            if has_box:
                plain_lines = [" ".join(cleaned[i:i + line_words]) for i in range(0, len(cleaned), line_words)]
                events.append(_box_event(start_str, end_str, plain_lines))
            events.append(
                f"Dialogue: {text_layer},{start_str},{end_str},Default,,0,0,0,,"
                f"{{{prefix_tags}\\c{base_color}}}{paragraph_text}"
            )
    elif words:
        chunk_size = max(1, int(style_config.get("words_per_line") or 6))
        for i in range(0, len(words), chunk_size):
            chunk = words[i:i + chunk_size]
            line_start = chunk[0]["start"]
            line_end = chunk[-1]["end"]
            cleaned = [apply_text_case(w["word"].replace("{", "").replace("}", ""), text_case) for w in chunk]

            if has_box:
                events.append(_box_event(format_ass_time(line_start), format_ass_time(line_end), [" ".join(cleaned)]))

            if highlight_mode == "word":
                # One event per word: the same full chunk text, but only the
                # currently-spoken word gets the accent color override.
                for idx, w in enumerate(chunk):
                    parts = []
                    for j, word_text in enumerate(cleaned):
                        if j == idx:
                            parts.append(f"{{\\c{accent_color}}}{word_text}{{\\c{base_color}}}")
                        else:
                            parts.append(word_text)
                    line_text = " ".join(parts)
                    start_str = format_ass_time(w["start"])
                    # Extend to the NEXT word's start (or the chunk's end for the
                    # last word) instead of this word's own end — natural pauses
                    # between spoken words left a gap with no active dialogue
                    # event, which made the whole subtitle line flicker off and
                    # back on between every word.
                    next_start = chunk[idx + 1]["start"] if idx + 1 < len(chunk) else line_end
                    end_str = format_ass_time(max(next_start, w["end"]))
                    events.append(f"Dialogue: {text_layer},{start_str},{end_str},Default,,0,0,0,,{{{prefix_tags}}}{line_text}")
            else:
                # "line" shows the accent color for the whole chunk; "none"
                # just uses the neutral base color — both are a single event.
                text_color = accent_color if highlight_mode == "line" else base_color
                line_text = f"{{{prefix_tags}\\c{text_color}}}" + " ".join(cleaned)
                start_str = format_ass_time(line_start)
                end_str = format_ass_time(line_end)
                events.append(f"Dialogue: {text_layer},{start_str},{end_str},Default,,0,0,0,,{line_text}")

    else:
        text = apply_text_case(transcript_info.get("text", ""), text_case)
        duration = transcript_info.get("duration", 5.0)
        sentences = [s.strip() for s in text.replace(".", ".|").replace("!", "!|").replace("?", "?|").split("|") if s.strip()]
        if not sentences:
            sentences = [text]

        text_color = accent_color if highlight_mode == "line" else base_color
        time_per_sentence = duration / len(sentences)
        for i, s in enumerate(sentences):
            start_time = i * time_per_sentence
            end_time = (i + 1) * time_per_sentence
            start_str = format_ass_time(start_time)
            end_str = format_ass_time(end_time)
            if has_box:
                events.append(_box_event(start_str, end_str, [s]))
            events.append(f"Dialogue: {text_layer},{start_str},{end_str},Default,,0,0,0,,{{{prefix_tags}\\c{text_color}}}{s}")

    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")

    logger.info(f"Generated ASS subtitle file with {len(events)} dialogue lines: {output_ass_path}")
    return output_ass_path

def overlay_subtitles_on_image(
    input_image_path: Path,
    output_image_path: Path,
    text: str,
    style_config: Dict[str, Any]
) -> Path:
    """
    Renders styled subtitle text directly onto a 1920x1080 image using Pillow.
    Ensures visual burn-in even when FFmpeg lacks libass/subtitles filter.
    """
    img = Image.open(input_image_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    width, height = img.size
    
    font_size = style_config.get("size", 48)
    position = str(style_config.get("position", "bottom")).strip().lower()
    
    try:
        font = ImageFont.truetype(_resolve_font_file(style_config.get("font")), font_size)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype(_DEJAVU_BOLD_FALLBACK, font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (width - text_w) // 2
    if position == "top":
        y = 60
    elif position == "center":
        y = (height - text_h) // 2
    else: # bottom
        y = height - 120

    # Draw dark translucent background bar behind subtitle for high readability
    padding = 16
    draw.rectangle(
        [x - padding, y - padding, x + text_w + padding, y + text_h + padding],
        fill=(0, 0, 0, 160)
    )

    # Draw outline
    outline_color = (0, 0, 0, 255)
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)

    # Draw text
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    output_image_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(output_image_path)
    return output_image_path
