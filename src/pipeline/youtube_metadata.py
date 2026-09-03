"""Generate publication-ready YouTube metadata and a branded thumbnail."""
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageFilter

from src.config import STORAGE_PATH, ASSETS_PATH
from src.pipeline.ai_text import generate_text, any_text_provider_configured
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

# A thumbnail headline is not the video's full title. Image models render a
# few large words reliably; long copy is what led to cropped fragments such
# as “EMPÊC” and “DÉGIVRE T” in otherwise good artwork.
_THUMBNAIL_HEADLINE_MAX_WORDS = 5
_THUMBNAIL_HEADLINE_MAX_CHARS = 38
_TRAILING_CONNECTORS = {"à", "au", "aux", "avec", "dans", "de", "des", "du", "en", "et", "le", "la", "les", "pour", "sur", "un", "une", "vers"}


def clean_thumbnail_headline(value: str) -> str:
    """Keep only a short sequence of complete, natural-looking words.

    A shortened fallback is preferable to truncating a word half way through:
    it keeps the image clean and gives GPT Image 2 a headline it can fit.
    """
    headline = re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip(" \"'“”«»")
    headline = re.sub(r"[|•·…]+", " ", headline)
    kept = []
    for word in headline.split():
        candidate = " ".join([*kept, word])
        if len(kept) >= _THUMBNAIL_HEADLINE_MAX_WORDS or len(candidate) > _THUMBNAIL_HEADLINE_MAX_CHARS:
            break
        kept.append(word)
    while len(kept) > 1 and kept[-1].casefold().strip(".,:;!?") in _TRAILING_CONNECTORS:
        kept.pop()
    return " ".join(kept) or "NOUVELLE VIDÉO"


def _headline_fits_image(value: str) -> bool:
    words = str(value or "").strip().split()
    return 2 <= len(words) <= _THUMBNAIL_HEADLINE_MAX_WORDS and len(" ".join(words)) <= _THUMBNAIL_HEADLINE_MAX_CHARS


def generate_contextual_thumbnail_headline(script: str, title: str, niche: str, draft: str = "") -> str:
    """Write the *hook* for the image, not a shortened YouTube title.

    The metadata model's JSON answer is useful for title/description, but a
    thumbnail needs a distinct editorial decision. Giving that decision its
    own small pass stops a long title's opening words becoming the headline.
    """
    if not any_text_provider_configured():
        return clean_thumbnail_headline(draft or title)
    prompt = f"""Tu es directeur éditorial de miniatures YouTube.
Lis le sujet et le script, puis écris UNE accroche visuelle autonome dans la langue du script.

Sujet de la vidéo : {title}
Niche : {niche or 'générale'}
Script : {(script or '')[:7000]}

Règles impératives :
- 2 à 5 mots, 38 caractères maximum ;
- des mots entiers uniquement, sans points de suspension ni ponctuation ;
- exprime le bénéfice, le résultat, le danger, la surprise ou le conflit le plus fort du contenu ;
- ne reprends PAS simplement le début du titre et ne résume pas tout le titre ;
- la phrase doit être compréhensible seule et donner envie de cliquer.

Exemple : pour une vidéo sur une feuille d'aluminium dans le congélateur qui évite la buée, préfère « FINI LA BUÉE » à « UNE BANDE DE PAPIER ALUMINIUM ».
Réponds uniquement par l'accroche, sans guillemets ni explication."""
    try:
        candidate = generate_text(prompt, max_tokens=60, operation="thumbnail_headline", preferred_provider="gemini", free_only=True).strip()
        candidate = re.sub(r"^['\"“”«»]+|['\"“”«»]+$", "", candidate).strip()
        candidate = re.sub(r"\s+", " ", candidate)
        if _headline_fits_image(candidate):
            return candidate
        logger.warning("Thumbnail headline pass returned an invalid length; using compact safe fallback.")
    except Exception as exc:
        logger.warning(f"Contextual thumbnail headline generation failed: {exc}")
    return clean_thumbnail_headline(draft or title)


def _fallback_metadata(video, channel) -> dict:
    raw_title = (video.title or video.script_text or "Nouvelle vidéo").strip().splitlines()[0]
    title = re.sub(r"\s+", " ", raw_title)[:100]
    niche = (channel.niche or "").strip()
    # No KappGen mention here: this text is published on the creator's own
    # channel, under their own brand — the tool that produced the video has no
    # business signing it.
    description = _rich_fallback_description(video, channel, title)
    tags = [tag for tag in [niche, channel.name] if tag]
    return {"title": title, "description": description, "tags": tags, "thumbnail_text": clean_thumbnail_headline(title)}


def _timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _chapter_anchors(duration_seconds: float) -> list:
    """Return fixed, truthful timestamps for the metadata model to title.

    YouTube chapters need a 0:00 entry and work best with useful editorial
    sections rather than one timestamp per visual scene. The model is allowed
    to name these anchors, but never to invent their timing.
    """
    duration = max(0, int(duration_seconds or 0))
    if duration < 60:
        return [0]
    chapter_count = max(3, min(9, round(duration / 150) + 1))
    step = duration / chapter_count
    return [round(i * step) for i in range(chapter_count)]


def _script_excerpt(script: str, max_chars: int = 620) -> str:
    text = re.sub(r"\s+", " ", script or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def _rich_fallback_description(video, channel, title: str) -> str:
    """Useful deterministic copy when no text provider is configured.

    The old fallback merely repeated the title and channel name, which made a
    completed publication look unfinished. This version remains honest (no
    invented claims or visuals), but still tells viewers what they will get.
    """
    script = (getattr(video, "script_text", None) or "").strip()
    excerpt = _script_excerpt(script)
    duration = float(getattr(video, "duration_seconds", None) or 0)
    anchors = _chapter_anchors(duration)
    sections = [
        title,
        excerpt or f"Cette vidéo explore « {title} » et en présente les éléments essentiels de manière progressive.",
        "Au fil de la vidéo, le sujet se construit étape par étape, avec ses informations importantes, ses moments clés et ce qu’il faut en retenir. Le montage visuel accompagne la narration et donne un rythme clair à chaque partie du contenu.",
    ]
    if len(anchors) > 1:
        generic_titles = ["Introduction", "Mise en contexte", "Développement", "Points clés", "À retenir", "Conclusion"]
        chapter_lines = []
        for index, anchor in enumerate(anchors):
            label = generic_titles[min(index, len(generic_titles) - 1)] if index < len(anchors) - 1 else "Conclusion"
            chapter_lines.append(f"{_timestamp(anchor)} {label}")
        sections.append("Chapitres\n" + "\n".join(chapter_lines))
    sections.append(f"Si cette analyse t’aide à voir le sujet autrement, abonne-toi à {channel.name} et partage ton point de vue en commentaire.")
    return "\n\n".join(sections)[:5000]


def generate_metadata(video, channel, reuse_existing: bool = False) -> dict:
    fallback = _fallback_metadata(video, channel)
    if reuse_existing and all((getattr(video, field, None) or "").strip() for field in ("title", "youtube_description", "thumbnail_text")):
        return {
            "title": video.title.strip()[:100],
            "description": video.youtube_description.strip()[:5000],
            "tags": [],
            "thumbnail_text": video.thumbnail_text.strip()[:255],
        }
    if not any_text_provider_configured():
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
        duration = float(getattr(video, "duration_seconds", None) or 0)
        chapter_anchors = ", ".join(_timestamp(value) for value in _chapter_anchors(duration))
        prompt = f"""Tu es l'Agent éditorial KappGen. Prépare la publication YouTube de cette vidéo.
Chaîne: {channel.name}. Niche: {channel.niche}. Langue du script à conserver.
Durée réelle: {_timestamp(duration)}.
Le contenu doit être original, fidèle au script, sans clickbait trompeur et conforme aux règles YouTube.
Ne mentionne jamais KappGen ni aucun outil de production dans le titre ou la description : le texte est publié sous la marque du créateur.
Script: {script}

La description doit être un vrai texte éditorial prêt à publier, et non une répétition du titre. Adapte entièrement le vocabulaire, le ton et les sections à la niche et au type réel de contenu : tutoriel, récit, documentaire, actualité, recette, sport, spiritualité, finance, santé, divertissement ou autre. Ne présume jamais qu'il s'agit d'une « analyse » si le script ne l'est pas. Elle doit contenir, dans cet ordre :
1. une accroche originale de 2 ou 3 phrases qui formule la question ou le conflit central ;
2. un résumé précis de ce que la vidéo montre, raconte, enseigne ou explique, en 2 paragraphes adaptés à son format ;
3. une section courte sur le contenu visuel et l'atmosphère du montage. Décris uniquement ce qui peut honnêtement être déduit du script ; n'invente aucun plan précis, personne, lieu ou archive ;
4. une section « Chapitres » utilisant EXACTEMENT les horodatages suivants, dans le même ordre, chacun une seule fois : {chapter_anchors}. Donne à chaque horodatage un intitulé court et spécifique au passage correspondant du script. Si seul 0:00 est fourni, omets la section ;
5. un appel naturel à commenter et à s'abonner à {channel.name}, sans formule générique du type « Une vidéo originale de... ».
Utilise des paragraphes aérés. Évite le remplissage, les promesses vagues et les affirmations absentes du script.

Réponds uniquement en JSON valide avec: title (max 100 caractères), description (entre 700 et 3500 caractères, SANS hashtags à la fin — ils sont ajoutés automatiquement à partir du champ tags, ne les duplique pas dans le texte),
tags (liste de 5 à 12 expressions pertinentes), thumbnail_text (2 à 5 mots, 38 caractères maximum, phrase complète et naturelle, fidèle au sujet). Le texte est imprimé tel quel dans une image : ne le coupe jamais, ne le termine jamais par "...", et ne renvoie ni le titre complet ni un sous-titre."""
        # Metadata is editorially useful but does not need Sonnet. Prefer
        # Gemini's free tier and retain the normal provider fallback chain.
        text = generate_text(prompt, max_tokens=1800, operation='youtube_metadata', preferred_provider='gemini', free_only=True)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        data = json.loads(text)
        title = str(data.get("title") or fallback["title"]).strip()[:100]
        description = str(data.get("description") or fallback["description"]).strip()[:5000]
        # A technically valid but editorially empty response must not recreate
        # the old three-line placeholder. Prefer the informative fallback.
        if len(description) < 350 or (description.casefold().startswith(title.casefold()) and len(description) < 700):
            logger.warning("YouTube metadata description was too generic; using rich safe fallback.")
            description = fallback["description"]
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
        headline = generate_contextual_thumbnail_headline(
            script=script,
            title=title,
            niche=channel.niche or "",
            draft=str(data.get("thumbnail_text") or ""),
        )
        return {
            "title": title,
            "description": description[:5000],
            "tags": tags,
            "thumbnail_text": headline,
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

    text_side = thumbnail_style.get("text_side")
    if text_side not in ("left", "right"):
        # Keep the result deterministic, but vary the grid across videos for
        # legacy channels that do not yet have a locked art direction.
        text_side = "left" if int(digest[16:24], 16) % 2 == 0 else "right"

    return font_file, font_family, accent_color, text_position, text_side


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
  "text_position": "one of: top, center, bottom — wherever the headline reads best against this concept's composition",
  "text_side": "one of: left, right — lock one side for the headline and reserve the opposite side for the dominant subject",
  "niche_examples": ["3 short concrete visual thumbnail recipes that are proven/appropriate for this niche, each describing subject, action, symbol, camera framing and palette; these are learning examples, not copies"]
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
    if data.get("text_side") not in ("left", "right"):
        data["text_side"] = None
    if not isinstance(data.get("niche_examples"), list):
        data["niche_examples"] = []
    data["niche_examples"] = [str(x).strip() for x in data["niche_examples"] if str(x).strip()][:5]
    return data


def build_thumbnail_background_prompt(text: str, niche: str, thumbnail_style: dict = None, image_style: dict = None) -> str:
    """Build a shot-specific art brief, not a generic image prompt.

    Thumbnail generators otherwise interpret an abstract headline as scenery:
    a distant figure in a large empty room is technically relevant but has no
    mobile-size focal point. This contract forces the model to design the
    background around the later typography and to tell one readable visual
    story with foreground, midground and background detail.
    """
    thumbnail_style = thumbnail_style or {}
    text = clean_thumbnail_headline(text)
    style_prompt = (thumbnail_style.get("style_prompt") or "").strip() or (
        (image_style or {}).get("style_prompt") or ""
    ).strip()
    typography = (thumbnail_style.get("typography_prompt") or "").strip()
    text_side = thumbnail_style.get("text_side")
    if text_side not in ("left", "right"):
        text_side = "left"
    subject_side = "right" if text_side == "left" else "left"
    character_anchor = (thumbnail_style.get("character_anchor") or "").strip()
    character_clause = (f"RECURRING CHARACTER ANCHOR: {character_anchor}. Preserve this same recognizable character identity in every thumbnail; change only pose, gesture, expression and action to fit the new topic. " if character_anchor else "Preserve any recurring character identity visible in the supplied references across thumbnails. ")

    # A creator's uploaded reference IS the art direction — it must not be
    # argued with. The generic "cinematic / deep blacks / sculpted key light /
    # premium editorial grading" boilerplate below used to be appended on top
    # of it unconditionally, which is exactly how a soft, flat, warm pastel
    # reference came back as a dark, high-contrast, heavily textured painting:
    # the boilerplate contradicted the reference and the generator split the
    # difference. It is now only the floor for channels that never supplied
    # one.
    if style_prompt:
        art_clause = (
            f"ART DIRECTION — reproduce this channel's established style exactly: {style_prompt}. "
            f"Match its medium, line work, shading, palette, contrast level, lighting and finish precisely. "
            f"Do not add cinematic colour grading, deep blacks, heavy grain or dramatic chiaroscuro unless that "
            f"style brief already calls for them. "
        )
        finish_clause = "Keep the finish faithful to the style brief above, crisp and readable at phone size. "
    else:
        art_clause = (
            "ART DIRECTION: editorial cinematic poster, rich tactile detail, bold controlled palette. "
        )
        finish_clause = (
            "LIGHT AND FINISH: sculpted key light on the face, deep blacks, luminous highlights, rich texture, "
            "crisp subject separation, premium editorial poster finish, intentional color grading, sharp important "
            "details, nuanced background storytelling, no bland stock-photo staging. "
        )

    # The reference's own scene is the single biggest source of repetition:
    # its held objects, room and decorative bubbles get treated as part of the
    # channel identity and reappear on every unrelated topic. Say plainly that
    # only the character and the style carry over.
    scene_clause = (
        f"SCENE — must be newly invented for THIS headline: build the setting, action, gesture and props around "
        f"the idea “{text}”. Only the character identity and the art direction carry over between thumbnails; "
        f"the environment, held objects, decorative icons, speech or thought bubbles and background are NOT part "
        f"of the channel's identity and must not be reused from the references. "
    )

    if typography:
        type_clause = (
            f"Add the exact headline “{text}” in the reserved {text_side} area, reproducing this channel's "
            f"established headline treatment exactly: {typography}. The headline colours are mandatory: use those "
            f"exact text colours, accent-word colours and box/band fill colours — never substitute your own "
            f"palette, never default to plain white or black text, never invert the contrast. HARD TEXT RULE: render "
            f"only this complete headline, word-for-word; do not add, omit, abbreviate, split or crop a word. Keep it "
            f"within four short lines and a safe 8 percent margin on every edge. "
        )
    else:
        type_clause = (
            f"Add the exact headline “{text}” in the reserved {text_side} area, large bold condensed uppercase "
            f"editorial typography, perfectly spelled, high contrast, thick dark outline, integrated into the scene. "
            f"HARD TEXT RULE: render only this complete headline, word-for-word; do not add, omit, abbreviate, split or "
            f"crop a word. Keep it within four short lines and a safe 8 percent margin on every edge. "
        )

    return (
        f"Premium YouTube thumbnail key art for the idea: {text}. Niche: {niche or 'general'}. "
        f"{art_clause}{character_clause}{scene_clause}"
        f"COMPOSITION: reserve the {text_side} 42 percent for a readable headline while keeping it visually alive with controlled texture, "
        f"light and atmospheric detail; place the main subject on the {subject_side}, occupying 55 to 75 percent of the full frame height, "
        f"tight close-up or dramatic medium close-up, face/eyes or the key object clearly readable at phone size, expressive gesture, "
        f"strong silhouette, foreground detail, absolutely no tiny distant figure, no full-body silhouette, no empty corridor, no anonymous landscape. "
        f"VISUAL STORY: translate the idea into one immediate, emotionally legible metaphor; add 3 to 5 topic-specific secondary elements or characters, "
        f"foreground props, tactile costume/skin/material detail and a layered environment that reward a second look, while keeping one unmistakable focal point. "
        f"{finish_clause}"
        f"16:9 landscape, edge-to-edge artwork. [[ALLOW_TEXT]] {type_clause}"
        f"no other words, no logo, no watermark."
    )


def _generate_ai_thumbnail_background(text: str, channel, destination: Path, video_id: Optional[str] = None) -> Path:
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
    prompt = build_thumbnail_background_prompt(text, niche, thumbnail_style, channel.image_style or {})
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
        if not debit_izivoice_usage_by_user_id(channel.user_id, THUMBNAIL_CREDITS, "ai_thumbnail_generation", video_id=video_id):
            raise RuntimeError(f"Insufficient KappGen credit balance for AI thumbnail generation (user {channel.user_id}).")

    # Unlike the bulk per-scene image generation (many images, needs to fail fast to
    # avoid stalling the whole render), this is the single standalone call for the
    # thumbnail — the one image viewers judge the video by — so it's worth waiting
    # longer for it rather than falling back to a plain video-frame grab.
    with httpx.Client(timeout=120.0) as client:
        generate_thumbnail_image(prompt, ai_path, client, reference_image_paths=reference_paths, provider_order=provider_order)
    return ai_path


def generate_thumbnail(video_path: Path, destination: Path, text: str, channel=None, video_id: Optional[str] = None, strict: bool = False) -> tuple[Path, bool]:
    """strict=True (channels with a configured reference style only — see
    queue_runner.py) skips the frame-grab/solid-color fallbacks entirely on
    an AI failure and just raises instead: publishing a generic, unstyled
    placeholder was worse than a clear "couldn't make one, try again" state
    to the creator. Non-strict callers (manual regen, the preview endpoint)
    keep the old best-effort behavior."""
    text = clean_thumbnail_headline(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame_path = destination.with_suffix(".frame.jpg")
    image = None
    ai_success = False
    if channel is not None:
        try:
            ai_path = _generate_ai_thumbnail_background(text, channel, destination, video_id=video_id)
            image = Image.open(ai_path).convert("RGB")
            ai_success = True
        except Exception as exc:
            if strict:
                raise RuntimeError(f"AI thumbnail generation failed: {exc}") from exc
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
    # AI providers frequently return a soft 1024/720 image. A restrained
    # unsharp pass restores the crisp, poster-like micro-contrast visible in
    # strong competitor thumbnails without creating halos around typography.
    image = image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=125, threshold=3))
    # GPT Image 2 owns the typography for AI thumbnails. The overlay remains
    # only for legacy/frame fallback images, where no text was generated.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    thumbnail_style = (channel.thumbnail_style or {}) if channel is not None else {}
    font_file, font_family, accent_color, text_position, text_side = _pick_thumbnail_variant(text, thumbnail_style)
    accent_rgb = tuple(int(accent_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    use_upper = font_family not in _THUMBNAIL_MIXED_CASE_FAMILIES

    # Reference-quality "faceless channel" thumbnails (the style creators
    # pointed us at) wrap by actual rendered pixel width, not a fixed
    # character count — Anton is condensed enough that a char-count heuristic
    # either wrapped far too early or overflowed the frame depending on which
    # letters were involved. Wrap against MAX_TEXT_WIDTH at the largest font
    # size that still fits in <=4 lines, shrinking one step at a time.
    # Use a real two-column poster grid. The previous 1180px text block ran
    # across virtually the entire canvas and inevitably covered the image's
    # subject; generators then learned to leave an empty, low-impact scene.
    MAX_TEXT_WIDTH = 590
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
    for size in (124, 112, 100, 88, 76, 68):
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

    if ai_success:
        result = image.convert("RGB")
        result.save(destination, "JPEG", quality=92, optimize=True)
        destination.with_suffix(".ai.jpg").unlink(missing_ok=True)
        return destination, True

    y = top
    for index, line in enumerate(lines):
        color = accent_color if index in highlight_indices else "white"
        line_width = int(font.getlength(line))
        x = LEFT_MARGIN if text_side == "left" else 1280 - LEFT_MARGIN - line_width
        draw.text((x, y), line, font=font, fill=color, stroke_width=stroke_width, stroke_fill="#000000")
        y += line_height

    # Small diamond-dash divider under the headline, like the reference
    # style's decorative rule beneath the closing line — purely cosmetic
    # polish, not brand-critical, so it stays a subtle gold accent rather
    # than a full-width bar.
    divider_y = top + text_block_height + 14
    divider_width = min(360, int(font.getlength(lines[-1])) or 360)
    divider_left = LEFT_MARGIN if text_side == "left" else 1280 - LEFT_MARGIN - divider_width
    draw.line([(divider_left, divider_y), (divider_left + divider_width, divider_y)], fill=accent_rgb + (255,), width=3)
    for dx in (-14, divider_width + 14):
        cx = divider_left + dx
        r = 7
        draw.polygon([(cx, divider_y - r), (cx + r, divider_y), (cx, divider_y + r), (cx - r, divider_y)], fill=accent_rgb + (255,))

    result = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    result.save(destination, "JPEG", quality=90, optimize=True)
    frame_path.unlink(missing_ok=True)
    destination.with_suffix(".ai.jpg").unlink(missing_ok=True)
    return destination, False
