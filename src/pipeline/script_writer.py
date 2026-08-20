"""
Full-auto pipeline: picks a fresh topic and writes a complete narration script
for a channel, with zero human input, for channels running in "auto" mode.
Given the channel's niche/style and its recent video titles (to avoid
repeating the same topic), returns a ready-to-render {title, script_text}.
"""
import json
import re
from typing import Dict, List, Optional
from src.config import ANTHROPIC_API_KEY
from src.utils.logger import logger

SCRIPT_WRITER_MODEL = "claude-sonnet-5"


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def generate_daily_script(
    niche: str,
    recent_titles: List[str],
    style_prompt: Optional[str] = None,
    target_minutes: float = 5.0,
) -> Optional[Dict[str, str]]:
    """
    Returns {"title": str, "script_text": str} for a brand-new video topic in
    this niche, or None if Claude isn't configured / the call fails — callers
    should treat None as "skip today, try again on the next scheduled run"
    rather than publishing a broken video.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — cannot auto-generate a daily script.")
        return None

    avoid_list = "\n".join(f"- {t}" for t in recent_titles[:20]) or "(none yet — this is the first video)"
    target_words = int(target_minutes * 150)  # ~150 spoken words/minute

    instruction = f"""You are the head writer for a faceless YouTube channel.

Niche: {niche or "general"}
{f"Creative/tone direction from the channel owner: {style_prompt}" if style_prompt else ""}

Titles of videos already published on this channel (never repeat these topics or very close variants):
{avoid_list}

Task: invent ONE brand-new, specific video topic that fits this niche and hasn't been covered yet, then write a complete narration script for it — the exact text a voiceover will read aloud, start to finish, with a strong hook in the first sentence, a clear throughline, and a natural closing line. No stage directions, no scene numbers, no headings — just the spoken narration text itself. Target length: about {target_words} words (~{target_minutes:.0f} minutes spoken).

Respond with ONLY this JSON object, no other text:
{{"title": "short punchy video title", "script_text": "the full narration script"}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=SCRIPT_WRITER_MODEL,
            max_tokens=6000,
            messages=[{"role": "user", "content": instruction}],
        )
        raw_text = "".join(b.text for b in response.content if b.type == "text")
        data = _extract_json(raw_text)
        title = str(data.get("title", "")).strip()
        script_text = str(data.get("script_text", "")).strip()
        if not title or not script_text:
            logger.warning("Daily script generation returned an empty title/script.")
            return None
        return {"title": title, "script_text": script_text}
    except Exception as e:
        logger.warning(f"Daily script generation (Claude) failed: {e}")
        return None
