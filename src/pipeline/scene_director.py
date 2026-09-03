"""
Turns a script's raw per-segment narration text into proper AI-image-generation
prompts, all sharing one consistent "visual bible" (subject/character, era,
palette, lighting, style) so a multi-scene video doesn't look like a random
grab-bag of unrelated images. Batched in chunks of scenes per render (see
_DIRECTOR_BATCH_SIZE) rather than one call for the whole video — the visual
bible is derived once, from the first batch, and reused verbatim by every
later batch so scenes still stay coherent with each other across batches.
"""
import json
import re
from typing import List, Optional
from src.pipeline.ai_text import generate_text, any_text_provider_configured
from src.utils.logger import logger

SCENE_DIRECTOR_MODEL = "claude-sonnet-5"

# Both directors below used to ask for every scene of the whole video in one
# call. That's fine for a short video, but a long one (a 1h video can easily
# cut to 150-200+ scenes) blows straight past the call's fixed max_tokens —
# the response gets truncated mid-JSON, the entry-count check fails, and the
# ENTIRE video falls back to raw narration text / no stock footage at all,
# not just the scenes past the token budget. Chunking keeps each call's
# expected output comfortably inside its max_tokens regardless of video
# length, and confines a single batch's failure to that batch's scenes
# instead of nulling out the whole result.
_DIRECTOR_BATCH_SIZE = 40


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

    visual_bible: Optional[str] = None
    prompts: List[Optional[str]] = [None] * len(segment_texts)
    any_success = False

    for start in range(0, len(segment_texts), _DIRECTOR_BATCH_SIZE):
        batch = segment_texts[start:start + _DIRECTOR_BATCH_SIZE]
        numbered_segments = "\n".join(f"{start + i + 1}. {t.strip() or '(silence/transition)'}" for i, t in enumerate(batch))
        try:
            if visual_bible is None:
                bible_task = (
                    'Write a short "visual bible" (2-3 sentences): recurring subject/character appearance if any, '
                    'setting/era, color palette, lighting mood, art style. This keeps every scene visually consistent '
                    "with the others, including scenes outside this batch. "
                    + (f"Honor this creator-specified style direction: {style_prompt}. " if style_prompt else "")
                )
                bible_field = '"visual_bible": "...", '
            else:
                bible_task = f"Use this already-established visual bible for every prompt below, do not invent a new one: {visual_bible} "
                bible_field = ""

            instruction = f"""You are the visual director for a faceless YouTube narration video (niche: {niche or "general"}).

FULL SCRIPT (for context):
{script_text[:6000]}

This batch covers scenes {start + 1} to {start + len(batch)} of {len(segment_texts)} total in the video. Each scene's on-screen image plays while this exact narration is spoken:
{numbered_segments}

Task:
{bible_task}
For EACH of these {len(batch)} scenes, write one dense, comma-separated image-generation prompt (no full sentences, no preamble) that: visually represents what's being narrated in that scene, and applies the visual bible's style/palette/lighting so all scenes look like they belong to the same video. Vary the composition/camera angle across scenes so it doesn't feel repetitive.
Every scene must be a text-free visual. Never request or depict words, letters, numbers, captions, titles, slogans, labels, signs, posters, banners, written pages, interfaces, typography, logos, signatures, or watermarks. If the narration mentions written material or a named concept, represent its meaning visually instead of placing its name in the image.

Respond with ONLY this JSON object, no other text:
{{{bible_field}"scene_prompts": ["prompt for scene {start + 1}", "..."]}}
The scene_prompts array MUST have exactly {len(batch)} entries, in order."""

            raw_text = generate_text(instruction, max_tokens=1800, model=SCENE_DIRECTOR_MODEL, operation='scene_direction')
            data = _extract_json(raw_text)
            batch_prompts = data.get("scene_prompts")
            if not isinstance(batch_prompts, list) or len(batch_prompts) != len(batch):
                logger.warning(f"Scene director returned {len(batch_prompts) if isinstance(batch_prompts, list) else 'no'} prompts for scenes {start + 1}-{start + len(batch)}; those scenes fall back to raw narration text.")
                continue
            if visual_bible is None:
                visual_bible = str(data.get("visual_bible") or "").strip() or "consistent, cohesive visual style"
            for i, p in enumerate(batch_prompts):
                prompts[start + i] = str(p).strip()
            any_success = True
        except Exception as e:
            logger.warning(f"Scene director failed for scenes {start + 1}-{start + len(batch)}, falling back to raw narration text for them: {e}")
            continue

    if not any_success:
        return None
    # A batch that failed leaves its scenes as raw narration text rather than
    # None — those would otherwise reach the image generator as a literal
    # "None, <style>" prompt further down the pipeline.
    return [p if p is not None else segment_texts[i].strip() for i, p in enumerate(prompts)]


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

    queries: List[Optional[str]] = [None] * len(segment_texts)
    any_success = False

    for start in range(0, len(segment_texts), _DIRECTOR_BATCH_SIZE):
        batch = segment_texts[start:start + _DIRECTOR_BATCH_SIZE]
        try:
            numbered_segments = "\n".join(f"{start + i + 1}. {t.strip() or '(silence/transition)'}" for i, t in enumerate(batch))
            instruction = f"""You pick stock-footage search queries for a faceless YouTube narration video (niche: {niche or "general"}).

This batch covers scenes {start + 1} to {start + len(batch)} of {len(segment_texts)} total in the video. Each scene below plays under this narration:
{numbered_segments}

For EACH of these {len(batch)} scenes, write ONE English stock-video search query (2-4 words) naming a concrete, filmable subject that fits what's being narrated — the kind of thing a stock library actually has footage of (places, weather, nature, cities, machines, hands, crowds, objects, work, travel).

Rules:
- Concrete and visual only. Never abstract concepts, emotions, metaphors or proper nouns of people.
- No words that would put text on screen (no "sign", "book page", "newspaper", "screen").
- Vary the queries so consecutive scenes don't return the same footage.

Respond with ONLY this JSON object, no other text:
{{"queries": ["query for scene {start + 1}", "..."]}}
The queries array MUST have exactly {len(batch)} entries, in order."""

            # Stock keywords are a lightweight classification task. Prefer
            # Gemini (free tier when configured) and fall back automatically
            # through the admin provider chain if it is unavailable.
            raw_text = generate_text(
                instruction,
                max_tokens=1500,
                model=SCENE_DIRECTOR_MODEL,
                operation='stock_query_direction',
                preferred_provider='gemini',
            )
            data = _extract_json(raw_text)
            batch_queries = data.get("queries")
            if not isinstance(batch_queries, list) or len(batch_queries) != len(batch):
                logger.warning(f"Stock-query director returned {len(batch_queries) if isinstance(batch_queries, list) else 'no'} queries for scenes {start + 1}-{start + len(batch)}; those scenes get no stock footage.")
                continue
            for i, q in enumerate(batch_queries):
                queries[start + i] = str(q).strip()
            any_success = True
        except Exception as e:
            logger.warning(f"Stock-query director failed for scenes {start + 1}-{start + len(batch)}, no stock footage for them: {e}")
            continue

    return queries if any_success else None
