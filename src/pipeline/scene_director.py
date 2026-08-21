"""
Turns a script's raw per-segment narration text into proper AI-image-generation
prompts, all sharing one consistent "visual bible" (subject/character, era,
palette, lighting, style) so a multi-scene video doesn't look like a random
grab-bag of unrelated images. One Claude call per render: cheap, and it sees
the whole script at once so the bible and every scene prompt stay coherent
with each other.
"""
import json
import re
from typing import List, Optional
from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY
from src.pipeline.ai_text import generate_text
from src.utils.logger import logger

SCENE_DIRECTOR_MODEL = "claude-sonnet-5"


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def build_scene_prompts(
    script_text: str,
    segment_texts: List[str],
    style_prompt: str = "",
    niche: str = "",
) -> Optional[List[str]]:
    """
    Given the full script and the narration text spoken during each visual
    segment (same order/count as the video's scene timeline), returns one
    ready-to-use image-generation prompt per segment — grounded in that
    segment's actual content, and consistent with a shared visual bible
    (character/setting/style) derived from the whole script. Returns None on
    any failure so callers can fall back to their previous behavior.
    """
    if not (ANTHROPIC_API_KEY or FAL_API_KEY or OPENAI_API_KEY) or not segment_texts:
        return None

    try:
        numbered_segments = "\n".join(f"{i+1}. {t.strip() or '(silence/transition)'}" for i, t in enumerate(segment_texts))

        instruction = f"""You are the visual director for a faceless YouTube narration video (niche: {niche or "general"}).

FULL SCRIPT:
{script_text[:6000]}

The video is cut into {len(segment_texts)} visual scenes, in order. Each scene's on-screen image plays while this exact narration is spoken:
{numbered_segments}

Task:
1. Write a short "visual bible" (2-3 sentences): recurring subject/character appearance if any, setting/era, color palette, lighting mood, art style. This keeps every scene visually consistent with the others.
{f"2. Honor this creator-specified style direction: {style_prompt}" if style_prompt else ""}
3. For EACH of the {len(segment_texts)} scenes, write one dense, comma-separated image-generation prompt (no full sentences, no preamble) that: visually represents what's being narrated in that scene, and applies the visual bible's style/palette/lighting so all scenes look like they belong to the same video. Vary the composition/camera angle across scenes so it doesn't feel repetitive.

Respond with ONLY this JSON object, no other text:
{{"visual_bible": "...", "scene_prompts": ["prompt for scene 1", "prompt for scene 2", ...]}}
The scene_prompts array MUST have exactly {len(segment_texts)} entries, in order."""

        raw_text = generate_text(instruction, max_tokens=4000, model=SCENE_DIRECTOR_MODEL)
        data = _extract_json(raw_text)
        prompts = data.get("scene_prompts")
        if not isinstance(prompts, list) or len(prompts) != len(segment_texts):
            logger.warning(f"Scene director returned {len(prompts) if isinstance(prompts, list) else 'no'} prompts for {len(segment_texts)} scenes; ignoring.")
            return None
        return [str(p).strip() for p in prompts]
    except Exception as e:
        logger.warning(f"Scene director failed, falling back to raw narration text as image prompts: {e}")
        return None
