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


def _analyze_with_anthropic(image_bytes: bytes, media_type: str) -> str:
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
                    {"type": "text", "text": STYLE_ANALYSIS_INSTRUCTION},
                ],
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Vision analysis returned no text content.")


def _analyze_with_openai(image_bytes: bytes, media_type: str) -> str:
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
