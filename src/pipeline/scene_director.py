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
from src.pipeline.ai_text import generate_text, any_text_provider_configured
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
    if not any_text_provider_configured() or not segment_texts:
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
4. Every scene must be a text-free visual. Never request or depict words, letters, numbers, captions, titles, slogans, labels, signs, posters, banners, written pages, interfaces, typography, logos, signatures, or watermarks. If the narration mentions written material or a named concept, represent its meaning visually instead of placing its name in the image.

Respond with ONLY this JSON object, no other text:
{{"visual_bible": "...", "scene_prompts": ["prompt for scene 1", "prompt for scene 2", ...]}}
The scene_prompts array MUST have exactly {len(segment_texts)} entries, in order."""

        raw_text = generate_text(instruction, max_tokens=4000, model=SCENE_DIRECTOR_MODEL, operation='scene_direction')
        data = _extract_json(raw_text)
        prompts = data.get("scene_prompts")
        if not isinstance(prompts, list) or len(prompts) != len(segment_texts):
            logger.warning(f"Scene director returned {len(prompts) if isinstance(prompts, list) else 'no'} prompts for {len(segment_texts)} scenes; ignoring.")
            return None
        return [str(p).strip() for p in prompts]
    except Exception as e:
        logger.warning(f"Scene director failed, falling back to raw narration text as image prompts: {e}")
        return None


def build_stock_search_queries(
    segment_texts: List[str],
    niche: str = "",
) -> Optional[List[str]]:
    """Turns each scene's narration into a short ENGLISH stock-footage search
    query (Pexels indexes in English, and searches by concrete filmable
    subject — "snowy forest aerial", not "the fragility of the human soul").

    Deliberately a separate, cheap call rather than an extra field on
    build_scene_prompts: image prompts and footage queries want opposite
    things (dense stylistic description vs. two or three plain searchable
    nouns), and keeping them apart means enabling stock footage can't
    regress the image path if this call fails — it just returns None and
    every scene stays on its image.
    """
    if not any_text_provider_configured() or not segment_texts:
        return None

    try:
        numbered_segments = "\n".join(f"{i+1}. {t.strip() or '(silence/transition)'}" for i, t in enumerate(segment_texts))
        instruction = f"""You pick stock-footage search queries for a faceless YouTube narration video (niche: {niche or "general"}).

Each scene below plays under this narration:
{numbered_segments}

For EACH of the {len(segment_texts)} scenes, write ONE English stock-video search query (2-4 words) naming a concrete, filmable subject that fits what's being narrated — the kind of thing a stock library actually has footage of (places, weather, nature, cities, machines, hands, crowds, objects, work, travel).

Rules:
- Concrete and visual only. Never abstract concepts, emotions, metaphors or proper nouns of people.
- No words that would put text on screen (no "sign", "book page", "newspaper", "screen").
- Vary the queries so consecutive scenes don't return the same footage.

Respond with ONLY this JSON object, no other text:
{{"queries": ["query for scene 1", "query for scene 2", ...]}}
The queries array MUST have exactly {len(segment_texts)} entries, in order."""

        raw_text = generate_text(instruction, max_tokens=1500, model=SCENE_DIRECTOR_MODEL, operation='stock_query_direction')
        data = _extract_json(raw_text)
        queries = data.get("queries")
        if not isinstance(queries, list) or len(queries) != len(segment_texts):
            logger.warning(f"Stock-query director returned {len(queries) if isinstance(queries, list) else 'no'} queries for {len(segment_texts)} scenes; ignoring.")
            return None
        return [str(q).strip() for q in queries]
    except Exception as e:
        logger.warning(f"Stock-query director failed, no stock footage will be fetched: {e}")
        return None
