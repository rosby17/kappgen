import base64
import json
import re
import httpx
from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY
from src.utils.logger import logger

STYLE_ANALYSIS_INSTRUCTION = (
    "You are helping configure an AI image generator for a video pipeline. "
    "Look at this reference image and write a single, dense image-generation prompt "
    "(comma-separated descriptors, no full sentences, no preamble) that captures both its "
    "visual style — art style/medium, color palette, lighting, mood, composition, level of "
    "detail — AND its subject matter: what kind of people, objects, settings or scenes it "
    "shows (e.g. 'doctors in white coats, hospital corridor' for a health reference, or "
    "'open Bible, praying hands, church interior' for a faith reference). Both halves matter: "
    "a creator uploads this image not just to fix a look, but to show the AI what their niche "
    "actually looks like, so later prompts stay visually on-topic instead of defaulting to "
    "generic or unrelated imagery. The goal is that feeding your prompt to an image generator "
    "reliably produces new images that share this reference's style AND subject world."
)

MULTI_STYLE_ANALYSIS_INSTRUCTION = (
    "You are configuring an AI generator for a channel's video-scene visuals. "
    "Study this set of reference images as one moodboard. Return one dense, reusable "
    "image-generation prompt (comma-separated descriptors, no preamble) that captures "
    "the shared subject world, characters, settings, art direction, palette, lighting, "
    "mood, composition and detail level. Infer the common pattern across the set rather "
    "than describing individual images. These are B-roll and scene visuals: explicitly "
    "require no words, captions, logos, typography, watermarks, UI or readable text in "
    "the generated images. Reply with only the prompt text."
)


THUMBNAIL_STYLE_ANALYSIS_INSTRUCTION = (
    "You are a senior YouTube thumbnail art director. This reference thumbnail defines a "
    "channel's visual identity, and your brief will be reused to generate EVERY future "
    "thumbnail, on completely different topics. So you must separate what repeats from what "
    "belongs to this one video.\n"
    "REUSABLE — describe precisely: art medium and rendering technique (illustration / photo / "
    "3D, line work, shading, texture, edge treatment), the exact palette with its dominant and "
    "accent colours, the contrast level and whether it reads soft or harsh, lighting direction "
    "and warmth, overall finish, the framing grid (which side holds the subject, which side "
    "stays clear for the headline), how much of the frame height the subject occupies, mood.\n"
    "NEVER PUT IN THE STYLE BRIEF — these belong to this one video and must change every time: "
    "the action or gesture, the objects being held, the furniture and room, decorative "
    "icons / badges / speech bubbles / thought bubbles, the background scenery, the specific "
    "facial expression. A brief that mentions any of them would force this exact scene onto "
    "every future topic, which is the failure mode you are here to prevent.\n"
    "Return ONLY valid JSON: {\"style_prompt\": \"dense comma-separated reusable style brief — "
    "medium, palette, contrast, lighting, finish, grid, subject scale, mood; no scene, no props, "
    "no action, no icons\", \"character_anchor\": \"the recurring person's stable identity only "
    "(approximate age, hair, skin, glasses, clothing family), or empty string when there is no "
    "recurring person\", \"typography_style\": \"how the headline itself is treated: case, weight, "
    "condensed or wide, whether words sit inside solid boxes/bands or directly on the art, outline and "
    "shadow treatment, how accent words are highlighted, placement, and — mandatory — the EXPLICIT colours: "
    "the main word colour and every accent/highlight colour given by name AND approximate hex (e.g. deep "
    "espresso brown #3B2A20 for main words, vivid brick red #C4442A for accent words), plus the fill colour "
    "of any box or band behind the text\", "
    "\"text_side\": \"left or right — the side kept clear for the headline\", "
    "\"analysis_summary\": \"one concise sentence naming the repeatable visual grammar\"}"
)


THUMBNAIL_MULTI_STYLE_ANALYSIS_INSTRUCTION = (
    "You are a senior YouTube thumbnail art director studying a competitor moodboard. "
    "Separate the repeatable visual grammar from each video's incidental subject. Analyze "
    "the dominant human archetype and facial intensity, subject scale, camera distance, "
    "foreground/midground/background layering, recurring symbolic props, density, palette, "
    "contrast, lighting direction, texture, art medium, and which side is consistently left "
    "clear for copy. Infer the majority pattern across ALL images; never describe one screenshot "
    "in isolation and never copy a logo, creator identity, or exact composition. The resulting "
    "style must stay reusable while poses, actions, supporting characters and metaphors change "
    "to match each new video's idea. Never write a specific held object, room, furniture, "
    "decorative icon, speech/thought bubble or gesture into the style brief: those belong to one "
    "video and would force that same scene onto every future topic. "
    "Return ONLY valid JSON: {\"style_prompt\": \"dense comma-separated generation brief with "
    "all repeatable visual rules and explicit subject scale/grid; no scene, no props, no action, no icons\", "
    "\"text_side\": \"left or right\", "
    "\"typography_style\": \"how the headline is treated across the set: case, weight, condensed or wide, "
    "whether words sit inside solid boxes/bands or directly on the art, outline and shadow treatment, "
    "how accent words are highlighted, placement, and — mandatory — the EXPLICIT colours: the main word colour "
    "and every accent/highlight colour given by name AND approximate hex, plus the fill colour of any box or "
    "band behind the text\", "
    "\"analysis_summary\": \"one concise sentence explaining the shared visual grammar\", "
    "\"character_anchor\": \"only when the same main character clearly recurs: identify a recognizable public figure or describe a private character from the supplied portrait with age, face, hair, clothing and era; empty when recurrence is not reliable\"}."
)


MUSIC_PROMPT_INSTRUCTION = (
    "You are configuring an AI music generator for background music on a YouTube video. "
    "Given the channel's niche and (optionally) an excerpt of this specific video's script, "
    "write a single short, dense music-generation prompt (comma-separated descriptors, no "
    "full sentences, no preamble): instrumentation, mood, tempo/BPM feel, genre. The track "
    "must stay subtle and non-distracting under a voiceover — never suggest vocals, lyrics, "
    "or anything that would compete with narration. Reply with only the prompt text."
)


# ---------------------------------------------------------------------------
# Provider calls. Each raises on failure (missing key, HTTP error, no usable
# credits, ...) so the fallback chain below can just try the next one.
# ---------------------------------------------------------------------------

def _analyze_many_with_anthropic(images: list, instruction: str) -> str:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")

    # The endpoint is called directly from the browser while a creator adds
    # references. A default SDK timeout can outlive Cloudflare's response
    # window, which turns an ordinary provider delay into a misleading
    # browser-level "Failed to fetch". Keep each fallback short enough that
    # the route can return a normal JSON error instead.
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=25.0)
    content = []
    for image_bytes, media_type in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": instruction})

    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=400,
        messages=[{"role": "user", "content": content}],
    )

    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Anthropic vision analysis returned no text content.")


def _analyze_many_with_fal(images: list, instruction: str) -> str:
    """Runs Claude through fal.ai's OpenRouter vision router — a fallback that
    burns fal.ai credits instead of Anthropic's, for when the Anthropic
    account is out of credit."""
    if not FAL_API_KEY:
        raise RuntimeError("FAL_API_KEY is not configured on the server.")

    image_urls = [
        f"data:{media_type};base64,{base64.standard_b64encode(image_bytes).decode('utf-8')}"
        for image_bytes, media_type in images
    ]
    resp = httpx.post(
        "https://fal.run/openrouter/router/vision",
        headers={"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"},
        json={
            "image_urls": image_urls,
            "prompt": instruction,
            "model": "anthropic/claude-sonnet-4.5",
        },
        timeout=25.0,
    )
    resp.raise_for_status()
    output = (resp.json() or {}).get("output")
    if not output:
        raise RuntimeError("fal.ai vision analysis returned no output.")
    return output.strip()


def _analyze_many_with_openai(images: list, instruction: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")

    content = [{"type": "text", "text": instruction}]
    for image_bytes, media_type in images:
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}})

    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=25.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if not text:
        raise RuntimeError("OpenAI vision analysis returned no text content.")
    return text.strip()


def _generate_music_prompt_with_anthropic(user_text: str) -> str:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=200,
        messages=[{"role": "user", "content": user_text}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Anthropic music prompt generation returned no text content.")


def _generate_music_prompt_with_fal(user_text: str) -> str:
    if not FAL_API_KEY:
        raise RuntimeError("FAL_API_KEY is not configured on the server.")

    resp = httpx.post(
        "https://fal.run/openrouter/router/vision",
        headers={"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"},
        json={"prompt": user_text, "model": "anthropic/claude-sonnet-4.5"},
        timeout=60.0,
    )
    resp.raise_for_status()
    output = (resp.json() or {}).get("output")
    if not output:
        raise RuntimeError("fal.ai music prompt generation returned no output.")
    return output.strip()


def _generate_music_prompt_with_openai(user_text: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")

    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": user_text}],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if not text:
        raise RuntimeError("OpenAI music prompt generation returned no text content.")
    return text.strip()


def _run_with_fallback(steps: list) -> str:
    """Tries each (provider_name, fn) pair in order, moving to the next one
    on any failure (missing key, no credit, network/HTTP error, ...). Raises
    the last error if every provider failed."""
    last_exc = None
    for name, fn in steps:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a fallback chain
            logger.warning(f"[vision] provider '{name}' failed, trying next: {exc}")
            last_exc = exc
    raise RuntimeError(f"All AI providers failed for this request. Last error: {last_exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_reference_image(image_bytes: bytes, media_type: str) -> str:
    """Analyzes a reference image and returns a reusable image-generation style prompt.
    Tries Anthropic first, falls back to fal.ai (Claude via OpenRouter), then OpenAI."""
    return _run_with_fallback([
        ("anthropic", lambda: _analyze_many_with_anthropic([(image_bytes, media_type)], STYLE_ANALYSIS_INSTRUCTION)),
        ("fal.ai", lambda: _analyze_many_with_fal([(image_bytes, media_type)], STYLE_ANALYSIS_INSTRUCTION)),
        ("openai", lambda: _analyze_many_with_openai([(image_bytes, media_type)], STYLE_ANALYSIS_INSTRUCTION)),
    ])


def analyze_reference_images(images: list) -> str:
    """Synthesizes one style brief from a visual-reference moodboard.

    Scene images deliberately differ from thumbnails: their generated output must
    never contain title text or typography.
    """
    if not images:
        raise ValueError("At least one reference image is required.")
    instruction = STYLE_ANALYSIS_INSTRUCTION if len(images) == 1 else MULTI_STYLE_ANALYSIS_INSTRUCTION
    return _run_with_fallback([
        ("anthropic", lambda: _analyze_many_with_anthropic(images, instruction)),
        ("fal.ai", lambda: _analyze_many_with_fal(images, instruction)),
        ("openai", lambda: _analyze_many_with_openai(images, instruction)),
    ])


def analyze_thumbnail_reference_image(image_bytes: bytes, media_type: str) -> str:
    """
    Analyzes a reference YouTube thumbnail and returns a reusable image-generation
    prompt for the thumbnail background, including its recurring subject archetype
    (e.g. a consistent character) rather than stripping it out like the generic
    per-video style prompt does.
    """
    return analyze_thumbnail_reference_images([(image_bytes, media_type)])


def analyze_thumbnail_reference_images(images: list) -> str:
    """
    Analyzes one or more reference YouTube thumbnails together and returns a single
    reusable image-generation prompt synthesizing their shared visual identity.
    images: list of (image_bytes, media_type) tuples.
    Tries Anthropic first, falls back to fal.ai (Claude via OpenRouter), then OpenAI.
    """
    return analyze_thumbnail_reference_profile(images)["style_prompt"]


def _thumbnail_analysis_instruction(images: list) -> str:
    return THUMBNAIL_STYLE_ANALYSIS_INSTRUCTION if len(images) == 1 else THUMBNAIL_MULTI_STYLE_ANALYSIS_INSTRUCTION


def analyze_thumbnail_reference_profile(images: list) -> dict:
    """Return the reusable visual brief plus everything that has to stay
    separate from it: the recurring character's identity, the channel's
    headline typography, and which side stays clear for that headline.

    Both the single-reference and the moodboard instructions now answer in the
    same JSON shape. A single reference used to come back as one free-form
    paragraph, and that paragraph inevitably described the reference's own
    scene — the held phone, the couch, the little chat bubbles — which then
    got replayed as the background brief of every later thumbnail whatever the
    topic was, and left typography undefined so the generator reinvented it
    each time."""
    raw = _run_with_fallback([
        ("anthropic", lambda: _analyze_many_with_anthropic(images, _thumbnail_analysis_instruction(images))),
        ("fal.ai", lambda: _analyze_many_with_fal(images, _thumbnail_analysis_instruction(images))),
        ("openai", lambda: _analyze_many_with_openai(images, _thumbnail_analysis_instruction(images))),
    ])
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(cleaned)
    except ValueError:
        # A model that ignored the JSON contract still gives usable art
        # direction — keep it rather than failing the whole upload.
        return {"style_prompt": raw.strip(), "text_side": None, "character_anchor": "", "typography_style": "", "analysis_summary": None}
    if not str(data.get("style_prompt") or "").strip():
        raise ValueError("Reference analysis returned no style_prompt")
    if data.get("text_side") not in ("left", "right"):
        data["text_side"] = None
    data["character_anchor"] = str(data.get("character_anchor") or "").strip()[:600]
    data["typography_style"] = str(data.get("typography_style") or "").strip()[:600]
    return data


def generate_music_prompt(niche: str, script_excerpt: str = "") -> str:
    """Uses Claude to turn a channel's niche (and optionally this video's script) into a
    focused instrumental-music generation prompt, instead of a naive template string.
    Tries Anthropic first, falls back to fal.ai (Claude via OpenRouter), then OpenAI."""
    user_text = f"Channel niche: {niche or 'general'}\n"
    if script_excerpt:
        user_text += f"Video script excerpt: {script_excerpt[:800]}\n"
    user_text += "\n" + MUSIC_PROMPT_INSTRUCTION

    return _run_with_fallback([
        ("anthropic", lambda: _generate_music_prompt_with_anthropic(user_text)),
        ("fal.ai", lambda: _generate_music_prompt_with_fal(user_text)),
        ("openai", lambda: _generate_music_prompt_with_openai(user_text)),
    ])
