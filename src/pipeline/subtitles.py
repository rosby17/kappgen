import math
from pathlib import Path
from typing import Dict, Any, List
from PIL import Image, ImageDraw, ImageFont
from src.utils.logger import logger

def to_ass_color(color: str, default: str = "&H00FFFFFF") -> str:
    """
    Converts a web hex color ("#RRGGBB" or "#RGB", as produced by an HTML
    <input type="color">) to ASS's &HAABBGGRR format. Values already in ASS
    format (starting with &H) pass through unchanged.
    """
    if not color:
        return default
    color = color.strip()
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
                return f"&H00{b}{g}{r}".upper()
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

def generate_ass_subtitles(
    transcript_info: Dict[str, Any],
    style_config: Dict[str, Any],
    output_ass_path: Path
) -> Path:
    """
    Generates an Advanced SubStation Alpha (.ass) subtitle file supporting word-by-word karaoke formatting.
    """
    font_name = style_config.get("font", "Arial")
    font_size = style_config.get("size", 44)
    primary_color = to_ass_color(style_config.get("color"), "&H00FFFFFF")  # White
    outline_color = to_ass_color(style_config.get("outline_color"), "&H00000000")  # Black
    outline_width = style_config.get("outline_width", 3)
    position = str(style_config.get("position", "bottom")).strip().lower()
    karaoke = style_config.get("karaoke", True)

    alignment = 2  # Bottom center
    margin_v = 50
    if position == "top":
        alignment = 8
        margin_v = 50
    elif position == "center":
        alignment = 5
        margin_v = 0

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary_color},&H0000FFFF,{outline_color},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_width},0,{alignment},20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    words = transcript_info.get("words", [])
    events = []

    if words and karaoke:
        chunk_size = max(1, int(style_config.get("words_per_line") or 6))
        for i in range(0, len(words), chunk_size):
            chunk = words[i:i + chunk_size]
            line_start = chunk[0]["start"]
            line_end = chunk[-1]["end"]
            
            k_text_parts = []
            for w in chunk:
                dur_cs = int(round((w["end"] - w["start"]) * 100))
                if dur_cs < 1:
                    dur_cs = 10
                word_clean = w["word"].replace("{", "").replace("}", "")
                k_text_parts.append(f"{{\\k{dur_cs}}}{word_clean}")
                
            line_text = " ".join(k_text_parts)
            start_str = format_ass_time(line_start)
            end_str = format_ass_time(line_end)
            events.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{line_text}")
            
    else:
        text = transcript_info.get("text", "")
        duration = transcript_info.get("duration", 5.0)
        sentences = [s.strip() for s in text.replace(".", ".|").replace("!", "!|").replace("?", "?|").split("|") if s.strip()]
        if not sentences:
            sentences = [text]
            
        time_per_sentence = duration / len(sentences)
        for i, s in enumerate(sentences):
            start_time = i * time_per_sentence
            end_time = (i + 1) * time_per_sentence
            events.append(f"Dialogue: 0,{format_ass_time(start_time)},{format_ass_time(end_time)},Default,,0,0,0,,{s}")

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
        font = ImageFont.truetype("Helvetica", font_size)
    except IOError:
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
