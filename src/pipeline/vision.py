import base64
from typing import Tuple
from src.config import VISION_PROVIDER, ANTHROPIC_API_KEY, OPENAI_API_KEY
from src.utils.logger import logger

STYLE_ANALYSIS_INSTRUCTION = (
    "You are helping configure an AI image generator for a video pipeline. "
    "Look at this reference image and write a single, dense image-generation prompt "
    "(comma-separated descriptors, no full sentences, no preamble) that captures its "
    "visual style: art style/medium, color palette, lighting, mood, composition, and "
    "level of detail. The goal is that feeding your prompt to an image generator "
    "reliably produces new images in this same style. Do not describe the specific "
    "subject/content of this image, only its reusable visual style."
)


THUMBNAIL_STYLE_ANALYSIS_INSTRUCTION = (
    "You are helping configure an AI image generator for YouTube thumbnail backgrounds. "
    "Look at this reference thumbnail and write a single, dense image-generation prompt "
    "(comma-separated descriptors, no full sentences, no preamble) that captures its "
    "reusable visual identity: subject type/archetype (e.g. elderly bearded man in a robe, "
    "praying), framing/composition, art style/medium, color palette, lighting, mood, and "
    "level of detail. Unlike a generic style prompt, DO include the recurring subject "
    "archetype if the thumbnail centers on a consistent character type — that's part of "
    "this channel's identity. Do not mention any on-image text/typography, since that is "
    "added separately. Reply with only the prompt text."
)


def _analyze_with_anthropic(image_bytes: bytes, media_type: str, instruction: str = STYLE_ANALYSIS_INSTRUCTION) -> str:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    },
                    {"type": "text", "text": instruction},
                ],
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Vision analysis returned no text content.")


def _analyze_with_openai(image_bytes: bytes, media_type: str, instruction: str = STYLE_ANALYSIS_INSTRUCTION) -> str:
    raise NotImplementedError(
        "OpenAI vision analysis isn't wired up yet — set VISION_PROVIDER=openai and "
        "OPENAI_API_KEY once available, and implement this function."
    )


def analyze_reference_image(image_bytes: bytes, media_type: str) -> str:
    """
    Analyzes a reference image and returns a reusable image-generation style prompt.
    Provider is chosen via VISION_PROVIDER ("anthropic" | "openai").
    """
    if VISION_PROVIDER == "openai":
        return _analyze_with_openai(image_bytes, media_type)
    return _analyze_with_anthropic(image_bytes, media_type)


def analyze_thumbnail_reference_image(image_bytes: bytes, media_type: str) -> str:
    """
    Analyzes a reference YouTube thumbnail and returns a reusable image-generation
    prompt for the thumbnail background, including its recurring subject archetype
    (e.g. a consistent character) rather than stripping it out like the generic
    per-video style prompt does.
    """
    if VISION_PROVIDER == "openai":
        return _analyze_with_openai(image_bytes, media_type, THUMBNAIL_STYLE_ANALYSIS_INSTRUCTION)
    return _analyze_with_anthropic(image_bytes, media_type, THUMBNAIL_STYLE_ANALYSIS_INSTRUCTION)


MUSIC_PROMPT_INSTRUCTION = (
    "You are configuring an AI music generator for background music on a YouTube video. "
    "Given the channel's niche and (optionally) an excerpt of this specific video's script, "
    "write a single short, dense music-generation prompt (comma-separated descriptors, no "
    "full sentences, no preamble): instrumentation, mood, tempo/BPM feel, genre. The track "
    "must stay subtle and non-distracting under a voiceover — never suggest vocals, lyrics, "
    "or anything that would compete with narration. Reply with only the prompt text."
)


def _generate_music_prompt_with_anthropic(niche: str, script_excerpt: str) -> str:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_text = f"Channel niche: {niche or 'general'}\n"
    if script_excerpt:
        user_text += f"Video script excerpt: {script_excerpt[:800]}\n"
    user_text += "\n" + MUSIC_PROMPT_INSTRUCTION

    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=200,
        messages=[{"role": "user", "content": user_text}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Music prompt generation returned no text content.")


def generate_music_prompt(niche: str, script_excerpt: str = "") -> str:
    """Uses Claude to turn a channel's niche (and optionally this video's script) into a
    focused instrumental-music generation prompt, instead of a naive template string."""
    if VISION_PROVIDER == "openai":
        raise NotImplementedError(
            "OpenAI music-prompt generation isn't wired up yet — implement alongside "
            "_analyze_with_openai once OPENAI_API_KEY is available."
        )
    return _generate_music_prompt_with_anthropic(niche, script_excerpt)
