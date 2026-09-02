"""Generate publication-ready YouTube metadata and a branded thumbnail."""
import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY, STORAGE_PATH, ASSETS_PATH
from src.pipeline.ai_text import generate_text
from src.pipeline.subtitles import _resolve_font_file
from src.utils.ffmpeg_runner import run_ffmpeg
from src.utils.logger import logger

# Anton (bundled in assets/fonts, guaranteed present regardless of container
# font packages) is the always-available fallback. Every video used to get
# this exact font — the literal "police figée" a creator flagged: no matter
# the niche, the headline always looked the same. Real family names below
# resolve through fontconfig (_resolve_font_file, same mechanism the subtitle
# burn-in already relies on) so this reuses fonts already verified present in
# the container instead of guessing file paths — this list is a curated
# subset of frontend App.jsx's SUBTITLE_FONTS "Display/Impact", "Rond, doux"
# and "Ludique" groups: bold enough to read as a poster headline, with
# genuinely different moods so a niche's thumbnail concept can actually pick
# one that fits, instead of every channel converging on the same look again.
_THUMBNAIL_FALLBACK_FONT = str(ASSETS_PATH / "fonts" / "Anton-Regular.ttf")
THUMBNAIL_FONT_FAMILIES = [
    "Anton",             # ultra-bold condensed impact — the previous universal default
    "Bebas Neue",        # tall condensed classic, documentary/factual
    "League Spartan",    # bold geometric, business/tech/finance
    "Yanone Kaffeesatz",  # punchy condensed alternative
    "Comfortaa",         # soft rounded, wellness/self-help/parenting
    "Dosis",             # soft rounded, gentle spirituality/mindfulness
    "Lobster Two",       # warm expressive, lifestyle/personal story
    "Kaushan Script",    # handwritten script, emotional/faith/poetry
    "Roboto Slab",       # authoritative serif-slab, true crime/history/documentary
    "Sora",               # clean geometric, tech/startup/finance
]


def _thumbnail_font_path(font_family: str) -> str:
    """Resolves a THUMBNAIL_FONT_FAMILIES entry to a real font file via
    fontconfig — falls back to the bundled Anton if the family is unset,
    not in the curated list, or fontconfig can't resolve it (e.g. a stale
    thumbnail_style saved before this font list existed/changed)."""
    if font_family not in THUMBNAIL_FONT_FAMILIES:
        return _THUMBNAIL_FALLBACK_FONT
    resolved = _resolve_font_file(font_family)
    return resolved if Path(resolved).exists() else _THUMBNAIL_FALLBACK_FONT


# Gold/yellow is still the safe default — the one color virtually every
# high-CTR thumbnail in this style uses for its "punch" line — but a
# concept's own accent_hex (from propose_thumbnail_concept) now takes
# priority when the channel actually has one, instead of every channel
# converging on the same near-gold hue regardless of niche/mood.
_THUMBNAIL_ACCENT_COLORS = ["#ffd400", "#f7c600", "#ffcc33"]
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


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
    # A blank/near-empty script_text (e.g. a manual "text" submission with
    # nothing pasted in, or an edge case upstream) must never reach the
    # prompt below — sent an empty "Script: " block, Claude reasonably
    # answers as if talking to the user ("no script was provided, please
    # paste it..."), and since that's still well-formed JSON, generate_text's
    # own error handling never catches it: it sails through as if it were a
    # real title/description. Caught exactly this in production (a video
    # published with the title "Script manquant : impossible de générer la
    # publication" — literally Claude's refusal, verbatim). The safe
    # fallback below never has this failure mode.
    if len((video.script_text or "").strip()) < 20:
        logger.warning(f"YouTube metadata generation skipped for video {getattr(video, 'id', '?')}: script_text is blank/too short, using safe fallback.")
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


# Script/rounded-soft families are drawn and kerned for mixed case — forcing
# ALL CAPS on "Kaushan Script" or "Lobster Two" reads as broken, not bold,
# unlike the condensed/geometric "impact" families this used to hardcode to
# (Anton) which are explicitly designed as all-caps display faces.
_THUMBNAIL_MIXED_CASE_FAMILIES = {"Comfortaa", "Dosis", "Lobster Two", "Kaushan Script"}


def _pick_thumbnail_variant(seed: str, thumbnail_style: dict = None):
    """Resolves the font/accent/text-position for one thumbnail render.
    thumbnail_style (from propose_thumbnail_concept, saved on the channel)
    takes priority when present — a niche-specific choice beats a random one.
    Falls back to a deterministic hash of `seed` (so the same video still
    looks the same across re-renders, and different videos on a channel with
    no concept yet still vary a little) rather than always the same font."""
    thumbnail_style = thumbnail_style or {}
    digest = hashlib.sha256((seed or "").encode("utf-8")).hexdigest()

    font_family = thumbnail_style.get("font_family")
    if font_family not in THUMBNAIL_FONT_FAMILIES:
        font_family = THUMBNAIL_FONT_FAMILIES[int(digest[:8], 16) % len(THUMBNAIL_FONT_FAMILIES)]
    font_file = _thumbnail_font_path(font_family)

    accent_hex = thumbnail_style.get("accent_hex")
    accent_color = accent_hex if accent_hex and _HEX_COLOR_RE.match(accent_hex) else (
        _THUMBNAIL_ACCENT_COLORS[int(digest[8:16], 16) % len(_THUMBNAIL_ACCENT_COLORS)]
    )

    text_position = thumbnail_style.get("text_position")
    if text_position not in ("top", "center", "bottom"):
        text_position = "center"

    return font_file, font_family, accent_color, text_position


THUMBNAIL_CONCEPT_INSTRUCTION = """You are a YouTube thumbnail art director. A creator is starting a new "{niche}" channel and needs a distinctive, reusable visual identity for their thumbnails — not a single one-off image, but a locked-in STYLE that every future video's thumbnail will follow, the same way real successful channels in this niche have one instantly-recognizable look (e.g. a senior-health channel using a warm flat-illustration mascot instead of stock medical photos; a true-crime channel using a dark grainy photo-collage look; a finance channel using bold chart-and-cash iconography).

Study what actually works for "{niche}" specifically on YouTube today, then invent ONE concrete, opinionated concept for THIS channel — do not default to generic "cinematic dramatic lighting, high detail" descriptors, and do not reach for the same handful of tropes (candles/books for spirituality, gavels for law, etc.) unless you have a genuinely fresh angle on them. Be specific and visual, as if briefing an illustrator.
{avoid_clause}
Recent/example video titles from this channel, for context on tone and subject matter:
{titles}

The headline text must be one of these exact font families (a real font installed on the render server — pick the one whose mood actually matches this concept, don't default to the first one): {font_choices}.

Respond with ONLY this JSON object, no other text:
{{
  "concept_name": "a short 3-5 word label for this style, e.g. 'Mascot flat-illustration, warm muted palette'",
  "rationale": "1-2 sentences on why this concept fits the niche and will read well as a recognizable, repeatable thumbnail identity",
  "style_prompt": "a single dense, comma-separated image-generation prompt (no full sentences) describing the reusable visual identity: illustration/photo style, recurring subject or character (if any) and its exact look, color palette (2-3 named colors), mood, composition rules, and a concrete framing/composition instruction (e.g. tight close-up filling the frame, subject off-center with negative space for text, dramatic low angle) — avoid generic filler like 'cinematic, high detail, dramatic lighting'; be as specific as a real thumbnail designer's brief. This will be fed into an image generator for EVERY future thumbnail on this channel alongside that specific video's own topic, so describe the character/style/palette/layout/composition as fixed, but explicitly state that the character's pose, gesture, and action must change each time to match that video's specific topic (give 3-4 concrete example actions spanning different topics in this niche) — never lock in one single frozen pose/action/prop as if it were part of the identity, or every thumbnail will look like the same photo with different text pasted on.",
  "text_style": "one short phrase on how the bold headline text should look/behave to match this concept, e.g. 'thick rounded sans-serif in cream and terracotta banners' or 'condensed red/white slab caps like a news chyron'",
  "font_family": "exactly one of the font families listed above",
  "accent_hex": "a single #RRGGBB hex color for the headline's highlighted punch-word — must actually fit this concept's palette, not always gold/yellow",
  "text_position": "one of: top, center, bottom — wherever the headline reads best against this concept's composition"
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
    font_choices = ", ".join(THUMBNAIL_FONT_FAMILIES)
    instruction = THUMBNAIL_CONCEPT_INSTRUCTION.format(niche=niche or "general", avoid_clause=avoid_clause, titles=titles_block, font_choices=font_choices)
    raw = generate_text(instruction, max_tokens=800, model="claude-sonnet-5", operation="thumbnail_concept")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    data = json.loads(text)
    for key in ("concept_name", "rationale", "style_prompt", "text_style"):
        if not data.get(key):
            raise ValueError(f"Thumbnail concept response missing '{key}'")
    # font_family/accent_hex/text_position are new, best-effort — an
    # unrecognized font or malformed hex just falls back to the existing
    # hash-based variation in _pick_thumbnail_variant rather than failing the
    # whole concept generation over one bad enum value.
    if data.get("font_family") not in THUMBNAIL_FONT_FAMILIES:
        data["font_family"] = None
    if not (isinstance(data.get("accent_hex"), str) and _HEX_COLOR_RE.match(data["accent_hex"])):
        data["accent_hex"] = None
    if data.get("text_position") not in ("top", "center", "bottom"):
        data["text_position"] = None
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
    #
    # The old suffix here ("cinematic, high detail, dramatic lighting,
    # eye-catching") was generic boilerplate stacked on top of an already
    # detailed style_prompt (see propose_thumbnail_concept) — those buzzwords
    # are exactly what makes different niches' AI backgrounds converge on the
    # same generic look. When a real concept exists, its own composition
    # instruction (now part of style_prompt) is trusted instead; the
    # boilerplate only kicks in as a bare-minimum floor for channels that
    # never generated one (style_prompt empty/legacy).
    composition_floor = "" if thumbnail_style_prompt else "high detail, striking composition, "
    prompt = (
        f"YouTube thumbnail background, {text}, {niche} niche, {style_prompt}, "
        f"{composition_floor}16:9. "
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

    # Admin-controlled provider priority (src/utils/app_settings.py) — an
    # order containing only "huggingface" keeps thumbnails free (no credit
    # ever touched, no paid provider ever called, same guarantee as the
    # per-scene body images); adding "fal"/"izivoice" to it is the admin's
    # explicit opt-in to spend money on thumbnails, in the order chosen.
    from src.utils.app_settings import thumbnail_provider_order
    provider_order = thumbnail_provider_order()
    allow_paid_fallback = any(p in ("fal", "izivoice") for p in provider_order)

    # Billed like the per-scene AI images: debited BEFORE calling out to the
    # provider, so an insufficient balance never places the real (paid) call
    # — it just falls through to generate_thumbnail's own video-frame-grab
    # fallback below, same as any other AI-generation failure. Skipped
    # entirely in free-only mode since no paid call can ever happen.
    if channel.user_id and allow_paid_fallback:
        from src.utils.billing import debit_izivoice_usage_by_user_id
        from src.utils.billing import THUMBNAIL_CREDITS
        if not debit_izivoice_usage_by_user_id(channel.user_id, THUMBNAIL_CREDITS, "ai_thumbnail_generation"):
            raise RuntimeError(f"Insufficient KappGen credit balance for AI thumbnail generation (user {channel.user_id}).")

    # Unlike the bulk per-scene image generation (many images, needs to fail fast to
    # avoid stalling the whole render), this is the single standalone call for the
    # thumbnail — the one image viewers judge the video by — so it's worth waiting
    # longer for it rather than falling back to a plain video-frame grab.
    with httpx.Client(timeout=120.0) as client:
        generate_thumbnail_image(prompt, ai_path, client, reference_image_paths=reference_paths, provider_order=provider_order)
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

    thumbnail_style = (channel.thumbnail_style or {}) if channel is not None else {}
    font_file, font_family, accent_color, text_position = _pick_thumbnail_variant(text, thumbnail_style)
    accent_rgb = tuple(int(accent_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    use_upper = font_family not in _THUMBNAIL_MIXED_CASE_FAMILIES

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

    words = (text.upper() if use_upper else text).split()
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
    if text_position == "top":
        top = 50
    elif text_position == "bottom":
        top = max(40, 720 - text_block_height - 70)
    else:
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
