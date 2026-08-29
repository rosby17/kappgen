"""
Full-auto pipeline: picks a fresh topic and writes a complete narration script
for a channel, with zero human input, for channels running in "auto" mode.

The script's shape (how many parts, how long each one is, what it must cover,
formatting rules, call-to-action style, output language) is fully configurable
per channel via Channel.script_structure — inspired by the kind of detailed
"project instructions" creators already write by hand for long-form scripts
(e.g. a six-part, ten-thousand-word narration with Bible references for a
faith channel, or a completely different shape for another niche). Channels
that haven't configured one yet fall back to DEFAULT_SCRIPT_STRUCTURE, a
generic long-form narrative shape that works for any niche.

Long scripts (thousands of words) are written one part at a time in separate
Claude calls — each part sees the title, the niche, the shared style/formatting
rules, and the tail of what was already written for continuity — then stitched
into one continuous block of prose. This keeps every individual call well
within a safe output-token budget regardless of how long the full script is.
"""
import json
import re
from typing import Dict, List, Optional
from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY
from src.pipeline.ai_text import generate_text
from src.utils.logger import logger

SCRIPT_WRITER_MODEL = "claude-sonnet-5"

# Generic, niche-agnostic fallback — used only for channels that haven't
# configured their own script_structure yet.
DEFAULT_SCRIPT_STRUCTURE = {
    "language": "English",
    "parts": [
        {
            "name": "hook_intro",
            "word_count": 250,
            "guidance": "Open with a striking hook — a strong claim, a vivid moment, or a question that pulls the listener in. Introduce the topic and why it matters. Naturally invite the viewer to like the video and subscribe, without breaking the tone. Tease what's coming without giving it all away.",
        },
        {
            "name": "context",
            "word_count": 250,
            "guidance": "Give the background and context needed to understand the topic. Explain why this truth is often misunderstood, overlooked, or hard to grasp today.",
        },
        {
            "name": "main_part_one",
            "word_count": 900,
            "guidance": "Develop the core ideas one at a time, with concrete examples, stories, or analogies that make each one memorable. Ask thought-provoking questions along the way. Partway through, naturally remind the listener to like and subscribe.",
        },
        {
            "name": "main_part_two",
            "word_count": 900,
            "guidance": "Go deeper — surface less obvious insights, explain the real benefits of understanding and applying this, and gently correct common misconceptions about the topic.",
        },
        {
            "name": "application",
            "word_count": 900,
            "guidance": "Give concrete, practical steps the listener can apply starting today. Explain how this understanding changes daily life. Include one short original illustrative story (not a real historical account) that carries the lesson without stating the lesson outright — let the listener draw the conclusion themselves.",
        },
        {
            "name": "conclusion",
            "word_count": 300,
            "guidance": "Summarize the key ideas with power and clarity. End on a strong closing statement. Close with a natural, not pushy, call to action — sharing the video, leaving a comment, or exploring the topic further.",
        },
    ],
    "formatting_rules": [
        "Write every number out in words, never as digits.",
        "Do not include any section titles, labels, or headings anywhere in the text — it must read as one continuous piece.",
        "Write only words meant to be read aloud by a voiceover — no visual directions, no music cues, no camera directions, no stage directions of any kind.",
        "Write in flowing continuous paragraphs — never a single short line standing alone, never poetry-style formatting.",
    ],
    "cta_style": "Weave invitations to like, subscribe, and comment naturally into the narration, never as a jarring aside.",
}

MAX_PART_WORD_COUNT_PER_CALL = 1600  # keeps every single Claude call comfortably inside a safe output-token budget


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # With web search enabled Claude sometimes wraps the JSON in a line
        # or two of commentary despite the "ONLY this JSON object" instruction
        # (e.g. "Based on my search, here's a topic:\n{...}") — pull out the
        # first {...} block instead of failing the whole topic-pick call.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _split_oversized_parts(parts: List[dict]) -> List[dict]:
    """Splits any single part whose word_count is too large for one safe Claude
    call into several same-guidance sub-parts, so no individual call is asked
    to produce more than MAX_PART_WORD_COUNT_PER_CALL words at once."""
    result = []
    for part in parts:
        word_count = int(part.get("word_count", 0) or 0)
        if word_count <= MAX_PART_WORD_COUNT_PER_CALL:
            result.append(part)
            continue
        chunks = -(-word_count // MAX_PART_WORD_COUNT_PER_CALL)  # ceil div
        chunk_words = word_count // chunks
        for i in range(chunks):
            words = chunk_words if i < chunks - 1 else (word_count - chunk_words * (chunks - 1))
            result.append({"name": f"{part.get('name', 'part')}_{i+1}", "word_count": words, "guidance": part.get("guidance", "")})
    return result


def _pick_topic(
    niche: str,
    recent_titles: List[str],
    style_prompt: Optional[str],
    language: str,
    cost_sink: Optional[List[float]] = None,
    topic_examples: Optional[str] = None,
    use_web_trends: bool = False,
) -> Optional[str]:
    avoid_list = "\n".join(f"- {t}" for t in recent_titles[:20]) or "(none yet — this is the first video)"
    # Without real examples, topic selection has nothing to anchor on but the
    # niche label itself, which reads as generic/random rather than matching
    # a specific angle. `topic_examples` is free text the creator pasted —
    # either their own best-performing titles, or ones copied from a channel
    # they want to emulate — treated as "study this pattern", not "avoid it".
    examples_block = ""
    if topic_examples and topic_examples.strip():
        examples_lines = "\n".join(f"- {line.strip()}" for line in topic_examples.splitlines() if line.strip())
        examples_block = f"""
Here are example titles/topics that show exactly the kind of angle, specificity, and hook style this channel's audience responds to (these are either the creator's own best videos, or titles from a channel they want to emulate — study the pattern, don't just reuse the surface theme):
{examples_lines}
"""
    web_search_line = (
        "This channel covers current events/trends — use web search to find something genuinely happening right now (recent news, a real trending story, an actual event) and build the topic around it, instead of a generic evergreen angle."
        if use_web_trends else ""
    )
    instruction = f"""You are the head writer for a faceless YouTube channel (niche: {niche or "general"}).
{f"Creative/tone direction from the channel owner: {style_prompt}" if style_prompt else ""}
{examples_block}
{web_search_line}

Titles of videos already published on this channel (never repeat these topics or very close variants):
{avoid_list}

Invent ONE brand-new, specific video topic that fits this niche and hasn't been covered yet{" — matching the style and specificity of the examples above" if examples_block else ""}. Respond in {language} with ONLY this JSON object, no other text:
{{"title": "short punchy video title"}}"""
    try:
        raw_text = generate_text(
            instruction, max_tokens=1000 if use_web_trends else 300, model=SCRIPT_WRITER_MODEL,
            operation='script_topic', cost_sink=cost_sink, enable_web_search=use_web_trends,
        )
        data = _extract_json(raw_text)
        title = str(data.get("title", "")).strip()
        return title or None
    except Exception as e:
        logger.warning(f"Daily script topic selection failed: {e}")
        return None


def _write_part(
    title: str,
    niche: str,
    language: str,
    style_prompt: Optional[str],
    formatting_rules: List[str],
    cta_style: str,
    part: dict,
    previous_tail: str,
    is_last_part: bool,
    cost_sink: Optional[List[float]] = None,
) -> Optional[str]:
    word_count = int(part.get("word_count", 300) or 300)
    rules_block = "\n".join(f"- {r}" for r in formatting_rules) if formatting_rules else ""
    guidance_text = part.get("guidance", "")
    continuity_block = (
        f'The script so far ends with this text — continue the narration seamlessly from here, in the same voice, with no repetition and no break in flow (do not restate or summarize what was already said):\n"{previous_tail}"'
        if previous_tail else "This is the very opening of the script — start strong."
    )
    style_line = f"Creative/tone direction from the channel owner: {style_prompt}" if style_prompt else ""
    cta_line = f"Call-to-action style: {cta_style}" if cta_style else ""
    closing_line = "This is the closing section of the script." if is_last_part else ""

    instruction = f"""You are writing one continuous section of a long-form narration script, in {language}, for a video titled "{title}" (niche: {niche or "general"}).
{style_line}
{cta_line}

Formatting rules that apply to the whole script:
{rules_block}

This section must be about {word_count} words long and must cover: {guidance_text}
{closing_line}
{continuity_block}

Respond with ONLY the narration text for this section, nothing else — no title, no preamble, no quotation marks, no labels."""

    max_tokens = min(8000, int(word_count * 1.8) + 300)
    try:
        text = generate_text(instruction, max_tokens=max_tokens, model=SCRIPT_WRITER_MODEL, operation='script', cost_sink=cost_sink).strip()
        return text or None
    except Exception as e:
        logger.warning(f"Daily script part generation failed for part '{part.get('name')}': {e}")
        return None


def generate_daily_script(
    niche: str,
    recent_titles: List[str],
    style_prompt: Optional[str] = None,
    script_structure: Optional[Dict] = None,
    default_language: Optional[str] = None,
    topic_examples: Optional[str] = None,
    use_web_trends: bool = False,
) -> Optional[Dict[str, str]]:
    """
    Returns {"title": str, "script_text": str} for a brand-new video topic in
    this niche, written according to script_structure (or DEFAULT_SCRIPT_STRUCTURE
    if the channel hasn't configured one), or None if Claude isn't configured /
    the call fails — callers should treat None as "skip today, try again on the
    next scheduled run" rather than publishing a broken video.

    Language priority: the channel's own script_structure.language always wins.
    If the channel hasn't configured one, `default_language` (the creator's own
    locale, passed by the caller) is used instead of silently defaulting to
    English — a channel run by a French creator should never get an English
    script just because nobody explicitly typed "French" into its settings.
    """
    if not (ANTHROPIC_API_KEY or FAL_API_KEY or OPENAI_API_KEY):
        logger.warning("No AI provider configured (Anthropic/fal.ai/OpenAI) — cannot auto-generate a daily script.")
        return None

    structure = script_structure or DEFAULT_SCRIPT_STRUCTURE
    language = structure.get("language") or default_language or "English"
    raw_parts = structure.get("parts") or DEFAULT_SCRIPT_STRUCTURE["parts"]
    parts = _split_oversized_parts(raw_parts)
    formatting_rules = structure.get("formatting_rules") or DEFAULT_SCRIPT_STRUCTURE["formatting_rules"]
    cta_style = structure.get("cta_style") or DEFAULT_SCRIPT_STRUCTURE["cta_style"]

    if not parts:
        logger.warning("Daily script generation: script_structure has no parts configured.")
        return None

    cost_sink: List[float] = []
    try:
        title = _pick_topic(
            niche, recent_titles, style_prompt, language, cost_sink=cost_sink,
            topic_examples=topic_examples, use_web_trends=use_web_trends,
        )
        if not title:
            return None

        written_parts: List[str] = []
        tail = ""
        for i, part in enumerate(parts):
            part_text = _write_part(
                title, niche, language, style_prompt, formatting_rules, cta_style,
                part, tail, is_last_part=(i == len(parts) - 1), cost_sink=cost_sink,
            )
            if not part_text:
                logger.warning(f"Daily script generation: part '{part.get('name')}' failed, aborting this run.")
                return None
            written_parts.append(part_text)
            tail = part_text[-600:]

        script_text = " ".join(written_parts).strip()
        if not script_text:
            return None
        return {"title": title, "script_text": script_text, "generation_cost_usd": sum(cost_sink)}
    except Exception as e:
        logger.warning(f"Daily script generation failed: {e}")
        return None
