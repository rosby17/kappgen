"""Bounded-memory, branded Facecam composition using FFmpeg and Pillow."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.config import STORAGE_PATH
from src.pipeline.facecam_project import caption_segments


def run(args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, timeout=3600)


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            check=True, capture_output=True, text=True, timeout=30)
    data = json.loads(result.stdout)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return int(video["width"]), int(video["height"]), float(data["format"]["duration"])


def output_size(source_size, format):
    return {"vertical": (1080, 1920), "square": (1080, 1080), "landscape": (1920, 1080)}.get(
        format, tuple(max(2, int(n) // 2 * 2) for n in source_size))


def fit_filter(size):
    w, h = size
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"


def card_image(path, text, size, settings):
    w, h = size
    from src.pipeline.subtitles import _resolve_font_file
    font_path = _resolve_font_file(settings["font_family"])
    font_size = max(18, round(min(w, h) * (0.062 if settings["card_style"] == "bold" else 0.046)))
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Fit the entire approved text. Reduce type before adding more lines.
    while True:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except OSError:
            font = ImageFont.load_default(size=font_size)
        lines, line = [], ""
        for word in text.split():
            if line and draw.textlength(line + " " + word, font=font) > w * .78:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            lines.append(line)
        if (len(lines) <= 4 and all(draw.textlength(s, font=font) <= w * .8 for s in lines)) or font_size <= 12:
            break
        font_size -= 2
    padding = max(12, round(w * .035))
    height = max(font_size * 2, len(lines) * round(font_size * 1.35)) + padding * 2
    left, top, right = round(w * .07), round(h * .13), round(w * .93)
    accent = settings["accent_color"]
    # A library preset changes the card composition while the channel accent
    # remains the visual cue that makes the creator's work recognisable.
    preset = settings.get("editing_style", "kappgen")
    treatments = {
        "vox": ("paper", "editorial"), "kallaway": ("midnight", "bold"),
        "keynote": ("clean", "minimal"), "atlas": ("documentary", "editorial"),
        "terminal": ("terminal", "bold"), "data": ("data", "editorial"),
        "optimist": ("optimist", "minimal"), "kappgen": ("kappgen", settings["card_style"]),
    }
    visual, card_treatment = treatments.get(preset, ("kappgen", settings["card_style"]))
    surface = {
        "paper": (239, 233, 220, 244), "midnight": (10, 14, 25, 240),
        "clean": (244, 246, 250, 244), "documentary": (22, 19, 14, 244),
        "terminal": (9, 23, 19, 244), "data": (241, 238, 230, 244),
        "optimist": (248, 245, 239, 244), "kappgen": (12, 17, 25, 235),
    }[visual]
    dark_text = visual in {"paper", "clean", "data", "optimist"}
    bold = card_treatment == "bold"
    editorial = card_treatment == "editorial"
    draw.rounded_rectangle((left, top, right, top + height), radius=6 if editorial else 22,
                           fill=accent if bold else surface)
    if not bold:
        draw.rectangle((left, top, left + 5, top + height), fill=accent)
    draw.multiline_text((left + padding, top + padding), "\n".join(lines), font=font,
                        fill="#071018" if bold or dark_text else "white", spacing=round(font_size * .35))
    template_labels = {
        "split": "DÉMONSTRATION", "before-after": "AVANT / APRÈS",
        "tutorial": "ÉTAPE CLÉ", "facecam": "",
    }
    label = template_labels.get(settings.get("format_template", "facecam"), "")
    if label:
        try:
            badge_font = ImageFont.truetype(font_path, max(11, round(font_size * .28)))
        except OSError:
            badge_font = ImageFont.load_default(size=max(11, round(font_size * .28)))
        badge_y = top - max(20, round(font_size * .52))
        draw.rounded_rectangle((left, badge_y, left + draw.textlength(label, font=badge_font) + padding * 2, top - 5), radius=5, fill=accent)
        draw.text((left + padding, badge_y + 4), label, font=badge_font, fill="#071018")
    image.save(path)
    return path


def overlay_clip(base, asset, output, start, duration, size, is_card=False):
    still = asset.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    inputs = ["-i", str(base), *( ["-loop", "1"] if still else ["-stream_loop", "-1"]), "-i", str(asset)]
    fade = min(.25, duration / 3)
    transform = ("format=rgba" if is_card else fit_filter(size) + ",format=rgba")
    graph = (f"[1:v]{transform},trim=duration={duration},setpts=PTS-STARTPTS,"
             f"fade=t=in:st=0:d={fade}:alpha=1,fade=t=out:st={duration-fade}:d={fade}:alpha=1,"
             f"setpts=PTS+{start}/TB[over];"
             f"[0:v][over]overlay=eof_action=pass:repeatlast=0:enable='between(t,{start},{start+duration})'[v]")
    run([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0", "-c:v", "libx264",
         "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", str(output)])
    return output


def captions_ass(path, words, size, settings):
    w, h = size
    color = settings["accent_color"].lstrip("#")
    color = f"&H00{color[4:6]}{color[2:4]}{color[0:2]}"
    align = {"bottom": 2, "center": 5, "top": 8}[settings["caption_position"]]
    size_px = max(18, round(min(w, h) * .046))
    def stamp(value):
        cs = round(value * 100)
        return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Default,{settings['font_family']},{size_px},{color},&H00FFFFFF,&H00100B08,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,{align},{round(w*.08)},{round(w*.08)},{round(h*.12)},1\n\n"
              "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    for segment in caption_segments(words, settings["words_per_line"]):
        # Strip ASS control characters from user/transcriber text.
        text = segment["text"].replace("\\", " ").replace("{", "").replace("}", "").replace("\n", " ")
        header += f"Dialogue: 0,{stamp(segment['start'])},{stamp(segment['end'])},Default,,0,0,0,,{text}\n"
    path.write_text(header, encoding="utf-8")
    return path


def finish(base, output, words, size, settings, branding, workdir):
    filters = []
    if settings["captions"]:
        ass = captions_ass(workdir / "captions.ass", words, size, settings)
        escaped = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")
        filters.append(f"ass=filename='{escaped}'")
    inputs = ["-i", str(base)]
    graph = f"[0:v]{','.join(filters) if filters else 'null'}[captioned]"
    label = "captioned"
    logo = STORAGE_PATH / branding["logo_path"] if branding.get("logo_path") else None
    if logo and logo.is_file() and branding.get("logo_enabled", True):
        inputs += ["-i", str(logo)]
        percent = min(30, max(3, float(branding.get("logo_size_percent") or 12)))
        margin = round(size[0] * .035)
        corner = branding.get("logo_corner", "top_right")
        x = str(margin) if "left" in corner else f"W-w-{margin}"
        y = f"H-h-{margin}" if "bottom" in corner else str(margin)
        graph += f";[1:v]scale={round(size[0]*percent/100)}:-1[logo];[captioned][logo]overlay={x}:{y}[branded]"
        label = "branded"
    master = settings["quality"] == "master"
    run([*inputs, "-filter_complex", graph, "-map", f"[{label}]", "-map", "0:a:0",
         "-c:v", "libx264", "-preset", "medium" if master else "fast", "-crf", "18" if master else "24",
         "-pix_fmt", "yuv420p", "-af", "loudnorm=I=-14:TP=-1:LRA=11" if master else "anull",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(output)])
    return output
