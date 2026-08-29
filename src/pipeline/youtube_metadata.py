"""Generate publication-ready YouTube metadata and a branded thumbnail."""
import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY, STORAGE_PATH, ASSETS_PATH
from src.pipeline.ai_text import generate_text
from src.utils.ffmpeg_runner import run_ffmpeg
from src.utils.logger import logger

# Anton (bundled in assets/fonts) is the condensed, ultra-bold display font
# real "faceless channel" YouTube thumbnails use (the reference style
# creators pointed us at) — DejaVu Sans/Serif Bold read as generic document
# fonts by comparison, not a poster headline. Kept as a list (one entry) so
# _pick_thumbnail_variant's hash-based selection code doesn't need special
# casing if a second display font is added later.
_THUMBNAIL_FONT_FILES = [
    str(ASSETS_PATH / "fonts" / "Anton-Regular.ttf"),
]
# Gold/yellow is the one color virtually every high-CTR thumbnail in this
# style uses for its "punch" line — swapped from the previous multi-hue
# palette (cyan/pink/green read as generic app-UI accents, not a thumbnail
# highlight color). Kept as a list of near-gold shades so regenerating still
# varies slightly between videos without ever picking an off-brand hue.
_THUMBNAIL_ACCENT_COLORS = ["#ffd400", "#f7c600", "#ffcc33"]


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
    if not (ANTHROPIC_API_KEY or FAL_API_KEY or OPENAI_API_KEY):
        return fallback
    try:
        script = (video.script_text or "")[:12000]
        prompt = f"""Tu es l'Agent éditorial KappGen. Prépare la publication YouTube de cette vidéo.
Chaîne: {channel.name}. Niche: {channel.niche}. Langue du script à conserver.
Le contenu doit être original, fidèle au script, sans clickbait trompeur et conforme aux règles YouTube.
Script: {script}

Réponds uniquement en JSON valide avec: title (max 100 caractères), description (max 5000, SANS hashtags à la fin — ils sont ajoutés automatiquement à partir du champ tags, ne les duplique pas dans le texte),
tags (liste de 5 à 12 expressions pertinentes), thumbnail_text (2 à 7 mots, fidèle au sujet)."""
        text = generate_text(prompt, max_tokens=1200, operation='youtube_metadata')
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        data = json.loads(text)
        title = str(data.get("title") or fallback["title"]).strip()[:100]
        description = str(data.get("description") or fallback["description"]).strip()[:5000]
        # Belt-and-suspenders: even with the prompt instruction above, Claude
        # sometimes still tacks on its own hashtag line — strip any trailing
        # line(s) made up entirely of hashtags so the block we append next
        # (built from the separate `tags` field) never ends up duplicated.
        description_lines = description.splitlines()
        while description_lines and re.fullmatch(r"(#\S+\s*)+", description_lines[-1].strip()):
            description_lines.pop()
        description = "\n".join(description_lines).rstrip()
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


def _font(size: int, font_file: str = None):
    candidates = [
        font_file,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _pick_thumbnail_variant(seed: str):
    """Deterministic pick from the font/color palettes above, based on a hash
    of `seed` — same video always looks the same, different videos vary."""
    digest = hashlib.sha256((seed or "").encode("utf-8")).hexdigest()
    font_file = _THUMBNAIL_FONT_FILES[int(digest[:8], 16) % len(_THUMBNAIL_FONT_FILES)]
    accent_color = _THUMBNAIL_ACCENT_COLORS[int(digest[8:16], 16) % len(_THUMBNAIL_ACCENT_COLORS)]
    return font_file, accent_color


THUMBNAIL_CONCEPT_INSTRUCTION = """You are a YouTube thumbnail art director. A creator is starting a new "{niche}" channel and needs a distinctive, reusable visual identity for their thumbnails — not a single one-off image, but a locked-in STYLE that every future video's thumbnail will follow, the same way real successful channels in this niche have one instantly-recognizable look (e.g. a senior-health channel using a warm flat-illustration mascot instead of stock medical photos; a true-crime channel using a dark grainy photo-collage look; a finance channel using bold chart-and-cash iconography).

Study what actually works for "{niche}" specifically on YouTube today, then invent ONE concrete, opinionated concept for THIS channel — do not default to generic "cinematic dramatic lighting, high detail" descriptors, and do not reach for the same handful of tropes (candles/books for spirituality, gavels for law, etc.) unless you have a genuinely fresh angle on them. Be specific and visual, as if briefing an illustrator.
{avoid_clause}
Recent/example video titles from this channel, for context on tone and subject matter:
{titles}

Respond with ONLY this JSON object, no other text:
{{
  "concept_name": "a short 3-5 word label for this style, e.g. 'Mascot flat-illustration, warm muted palette'",
  "rationale": "1-2 sentences on why this concept fits the niche and will read well as a recognizable, repeatable thumbnail identity",
  "style_prompt": "a single dense, comma-separated image-generation prompt (no full sentences) describing the reusable visual identity: illustration/photo style, recurring subject or character (if any) and its exact look, color palette (2-3 named colors), mood, composition rules. This will be fed into an image generator for every future thumbnail on this channel, so it must fully capture the identity on its own.",
  "text_style": "one short phrase on how the bold headline text should look/behave to match this concept, e.g. 'thick rounded sans-serif in cream and terracotta banners' or 'condensed red/white slab caps like a news chyron'"
}}"""


def propose_thumbnail_concept(niche: str, sample_titles: list, rejected_concepts: list = None) -> dict:
    """Asks Claude to invent one concrete, niche-appropriate thumbnail identity
    (style + recurring subject/character + palette) — the creative brief a
    real thumbnail designer would come up with for this specific niche,
    instead of a single generic template reused for every channel regardless
    of topic. Pass `rejected_concepts` (concept_name/style_prompt strings the
    creator has already declined) on a "propose another style" request so the
    next one is meaningfully different rather than a color-palette shuffle of
    the same idea."""
    avoid_clause = ""
    if rejected_concepts:
        listed = "\n".join(f"- {c}" for c in rejected_concepts)
        avoid_clause = (
            f"\nThe creator already rejected these concepts — propose something genuinely "
            f"different in approach (not just a different color palette on the same idea):\n{listed}\n"
        )
    titles_block = "\n".join(f"- {t}" for t in (sample_titles or [])) or "(no videos yet — infer from the niche alone)"
    instruction = THUMBNAIL_CONCEPT_INSTRUCTION.format(niche=niche or "general", avoid_clause=avoid_clause, titles=titles_block)
    raw = generate_text(instruction, max_tokens=800, model="claude-sonnet-5", operation="thumbnail_concept")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    data = json.loads(text)
    for key in ("concept_name", "rationale", "style_prompt", "text_style"):
        if not data.get(key):
            raise ValueError(f"Thumbnail concept response missing '{key}'")
    return data


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
    # "no text" alone is routinely ignored by image models on scenes with
    # books/scrolls/signs (a lit candle + open book prompt, for instance,
    # tends to render actual lettering on the page) — our own headline text
    # then gets drawn on top of that, producing a visibly duplicated title.
    # Repeating and escalating the instruction cuts this down noticeably.
    prompt = (
        f"YouTube thumbnail background, {text}, {niche} niche, {style_prompt}, "
        f"cinematic, high detail, dramatic lighting, eye-catching, 16:9. "
        f"Absolutely no text, no letters, no words, no titles, no captions, no typography, "
        f"no writing of any kind anywhere in the image, no watermark — pure photographic/artistic scene only."
    )
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

    # Billed like the per-scene AI images: debited BEFORE calling out to the
    # provider, so an insufficient balance never places the real (paid) call
    # — it just falls through to generate_thumbnail's own video-frame-grab
    # fallback below, same as any other AI-generation failure.
    if channel.user_id:
        from src.utils.billing import debit_izivoice_usage_by_user_id
        from src.utils.billing import THUMBNAIL_CREDITS
        if not debit_izivoice_usage_by_user_id(channel.user_id, THUMBNAIL_CREDITS, "ai_thumbnail_generation"):
            raise RuntimeError(f"Insufficient KappGen credit balance for AI thumbnail generation (user {channel.user_id}).")

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

    font_file, accent_color = _pick_thumbnail_variant(text)
    accent_rgb = tuple(int(accent_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    # Reference-quality "faceless channel" thumbnails (the style creators
    # pointed us at) wrap by actual rendered pixel width, not a fixed
    # character count — Anton is condensed enough that a char-count heuristic
    # either wrapped far too early or overflowed the frame depending on which
    # letters were involved. Wrap against MAX_TEXT_WIDTH at the largest font
    # size that still fits in <=4 lines, shrinking one step at a time.
    MAX_TEXT_WIDTH = 1180
    LEFT_MARGIN = 50

    def _wrap(words, font):
        lines, current = [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if font.getlength(candidate) > MAX_TEXT_WIDTH and current:
                lines.append(current); current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    words = text.upper().split()
    for size in (140, 122, 104, 88, 74):
        font = _font(size, font_file)
        lines = _wrap(words, font)
        if len(lines) <= 4:
            break
    # thumbnail_text is meant to already be short (2-7 words); this only
    # bites when it isn't (e.g. an older video that fell back to its full
    # title before that field existed). Ellipsize instead of silently
    # cutting the sentence mid-word, which read as a broken/incomplete
    # headline ("LE JOUR OÙ TU ARRÊTES DE / DEMANDER LA").
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip() + "…"

    line_height = int(size * 1.08)
    stroke_width = max(4, size // 16)

    # Gold on the last line (the "payoff" word/phrase) plus the single
    # shortest earlier line, if any — mirrors how these thumbnails usually
    # spotlight one short punch-word mid-headline (e.g. "JESUS", "GOD") in
    # addition to the closing line, rather than gold on every line or only
    # ever the last one.
    highlight_indices = {len(lines) - 1}
    if len(lines) > 2:
        earlier = lines[:-1]
        shortest_idx = min(range(len(earlier)), key=lambda i: len(earlier[i]))
        highlight_indices.add(shortest_idx)

    text_block_height = line_height * len(lines)
    top = max(40, (720 - text_block_height) // 2 - 20)

    # A soft, shallow gradient strictly behind the text block only (not the
    # whole frame) — thick black stroke on the text itself already carries
    # most of the legibility, so this just softens whatever's directly
    # behind it instead of dimming the visual the way a full scrim did.
    pad = 24
    gradient_top = max(0, top - pad)
    gradient_bottom = min(720, top + text_block_height + pad)
    for row in range(gradient_top, gradient_bottom):
        rel = (row - gradient_top) / max(1, gradient_bottom - gradient_top)
        alpha = int(120 * (1 - abs(rel - 0.5) * 1.6))
        draw.line([(0, row), (1280, row)], fill=(2, 8, 18, max(0, alpha)))

    y = top
    for index, line in enumerate(lines):
        color = accent_color if index in highlight_indices else "white"
        draw.text((LEFT_MARGIN, y), line, font=font, fill=color, stroke_width=stroke_width, stroke_fill="#000000")
        y += line_height

    # Small diamond-dash divider under the headline, like the reference
    # style's decorative rule beneath the closing line — purely cosmetic
    # polish, not brand-critical, so it stays a subtle gold accent rather
    # than a full-width bar.
    divider_y = top + text_block_height + 14
    divider_width = min(360, int(font.getlength(lines[-1])) or 360)
    draw.line([(LEFT_MARGIN, divider_y), (LEFT_MARGIN + divider_width, divider_y)], fill=accent_rgb + (255,), width=3)
    for dx in (-14, divider_width + 14):
        cx = LEFT_MARGIN + dx
        r = 7
        draw.polygon([(cx, divider_y - r), (cx + r, divider_y), (cx, divider_y + r), (cx - r, divider_y)], fill=accent_rgb + (255,))

    result = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    result.save(destination, "JPEG", quality=90, optimize=True)
    frame_path.unlink(missing_ok=True)
    destination.with_suffix(".ai.jpg").unlink(missing_ok=True)
    return destination
