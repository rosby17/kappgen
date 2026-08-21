"""Generate publication-ready YouTube metadata and a branded thumbnail."""
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from src.config import ANTHROPIC_API_KEY, STORAGE_PATH
from src.utils.ffmpeg_runner import run_ffmpeg
from src.utils.logger import logger


def _fallback_metadata(video, channel) -> dict:
    raw_title = (video.title or video.script_text or "Nouvelle vidéo").strip().splitlines()[0]
    title = re.sub(r"\s+", " ", raw_title)[:100]
    niche = (channel.niche or "").strip()
    description = (
        f"{title}\n\n"
        f"Une vidéo originale de {channel.name}, créée avec KappGen.\n\n"
        f"Abonne-toi à la chaîne pour découvrir les prochaines vidéos."
    )
    tags = [tag for tag in [niche, channel.name, "KappGen"] if tag]
    return {"title": title, "description": description, "tags": tags, "thumbnail_text": title[:55]}


def generate_metadata(video, channel) -> dict:
    fallback = _fallback_metadata(video, channel)
    if not ANTHROPIC_API_KEY:
        return fallback
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        script = (video.script_text or "")[:12000]
        prompt = f"""Tu es l'Agent éditorial KappGen. Prépare la publication YouTube de cette vidéo.
Chaîne: {channel.name}. Niche: {channel.niche}. Langue du script à conserver.
Le contenu doit être original, fidèle au script, sans clickbait trompeur et conforme aux règles YouTube.
Script: {script}

Réponds uniquement en JSON valide avec: title (max 100 caractères), description (max 5000),
tags (liste de 5 à 12 expressions pertinentes), thumbnail_text (2 à 7 mots, fidèle au sujet)."""
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        data = json.loads(text)
        title = str(data.get("title") or fallback["title"]).strip()[:100]
        description = str(data.get("description") or fallback["description"]).strip()[:5000]
        tags = [str(tag).strip() for tag in data.get("tags", []) if str(tag).strip()][:12]
        if tags:
            description += "\n\n" + " ".join("#" + re.sub(r"[^\w]", "", tag) for tag in tags[:5])
        return {
            "title": title,
            "description": description[:5000],
            "tags": tags,
            "thumbnail_text": str(data.get("thumbnail_text") or title).strip()[:70],
        }
    except Exception as exc:
        logger.warning(f"YouTube metadata generation failed, using safe fallback: {exc}")
        return fallback


def _font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _generate_ai_thumbnail_background(text: str, channel, destination: Path) -> Path:
    """Generates the thumbnail's background image — via fal.ai's gpt-image-2
    (falling back to Izivoice if fal.ai fails or its credits run out) —
    instead of just cropping a frame out of the finished video, for a
    purpose-made, eye-catching background that actually represents the
    video's subject."""
    from src.pipeline.images import generate_thumbnail_image
    import httpx

    # A dedicated thumbnail reference image (channel.thumbnail_style) takes priority
    # over the video's own body-image style, since creators often want a distinct
    # thumbnail look (e.g. a consistent character/composition) that a generic
    # per-scene style prompt wouldn't capture.
    thumbnail_style = channel.thumbnail_style or {}
    thumbnail_style_prompt = (thumbnail_style.get("style_prompt") or "").strip()
    style_prompt = thumbnail_style_prompt or ((channel.image_style or {}).get("style_prompt") or "").strip()
    niche = (channel.niche or "").strip()
    prompt = f"YouTube thumbnail background, {text}, {niche} niche, {style_prompt}, cinematic, high detail, dramatic lighting, eye-catching, no text, no watermark, 16:9"
    ai_path = destination.with_suffix(".ai.jpg")

    # Feed the creator's own uploaded thumbnail references straight into the
    # image model as conditioning images, not just as text (via style_prompt
    # above) — a text description alone routinely drifts from the reference's
    # actual look (character, palette, composition), which is what creators
    # were complaining about ("aucune ressemblance").
    reference_paths = [
        STORAGE_PATH / rel_path
        for rel_path in (thumbnail_style.get("reference_image_paths") or [])
    ]
    reference_paths = [p for p in reference_paths if p.exists()] or None

    # Unlike the bulk per-scene image generation (many images, needs to fail fast to
    # avoid stalling the whole render), this is the single standalone call for the
    # thumbnail — the one image viewers judge the video by — so it's worth waiting
    # longer for it rather than falling back to a plain video-frame grab.
    with httpx.Client(timeout=120.0) as client:
        generate_thumbnail_image(prompt, ai_path, client, reference_image_paths=reference_paths)
    return ai_path


def generate_thumbnail(video_path: Path, destination: Path, text: str, channel=None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame_path = destination.with_suffix(".frame.jpg")
    image = None
    if channel is not None:
        try:
            ai_path = _generate_ai_thumbnail_background(text, channel, destination)
            image = Image.open(ai_path).convert("RGB")
        except Exception as exc:
            logger.warning(f"AI thumbnail background generation failed, falling back to a video frame: {exc}")
    if image is None:
        try:
            # ffmpeg's `thumbnail` filter scores ~100 frames and picks the most
            # representative one, instead of grabbing a fixed timestamp — most of
            # these videos fade in from black over their first couple of seconds,
            # so a hardcoded "-ss 2s" grab reliably produced a near-black thumbnail
            # whenever the AI background above also failed.
            # Capped to the first 60s so this doesn't decode a full 1h video just to
            # score candidate frames — there's plenty of representative footage early on.
            run_ffmpeg(["ffmpeg", "-y", "-i", str(video_path), "-t", "60", "-frames:v", "1", "-vf", "thumbnail,scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720", str(frame_path)])
            image = Image.open(frame_path).convert("RGB")
        except Exception:
            image = Image.new("RGB", (1280, 720), "#07111f")
    image = image.resize((1280, 720)) if image.size != (1280, 720) else image
    image = ImageEnhance.Contrast(image).enhance(1.12)
    image = ImageEnhance.Color(image).enhance(1.1)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    words, lines, current = text.upper().split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > 18 and current:
            lines.append(current); current = word
        else:
            current = candidate
    if current: lines.append(current)
    lines = lines[:3]

    # Poster-style layout: a big bold title banked top-left over the AI visual
    # (like a real thumbnail's headline), instead of a heavy bar across the
    # bottom third that hides most of the generated image. Only a soft
    # top-down gradient sits behind the text for legibility — the visual
    # itself stays the star of the thumbnail.
    font = _font(108 if len(lines) < 3 else 88)
    line_height = 122 if len(lines) < 3 else 100
    text_block_height = line_height * len(lines)
    gradient_height = min(720, text_block_height + 170)
    for row in range(gradient_height):
        alpha = int(220 * (1 - row / gradient_height) ** 1.6)
        draw.line([(0, row), (1280, row)], fill=(2, 8, 18, alpha))

    y = 56
    for index, line in enumerate(lines):
        color = "#5edcff" if index == len(lines) - 1 else "white"
        draw.text((60, y), line, font=font, fill=color, stroke_width=5, stroke_fill="#020812")
        y += line_height

    # Thin brand accent bar along the bottom edge instead of a solid block.
    draw.rectangle((0, 706, 1280, 720), fill=(0, 194, 255, 235))

    result = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    result.save(destination, "JPEG", quality=90, optimize=True)
    frame_path.unlink(missing_ok=True)
    destination.with_suffix(".ai.jpg").unlink(missing_ok=True)
    return destination
