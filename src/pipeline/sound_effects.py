"""
Picks moments in a video's transcript where one of the creator's own
uploaded sound effects (ChannelSoundEffect) fits, e.g. a "whoosh" under a
topic change or a "ding" on a punchline. Deliberately does NOT source or
generate effects itself — it only ever chooses from clips the creator
already uploaded for that channel (see the /channels/{id}/sfx routes), the
same way scene_director.py only ever writes image prompts, never fetches
images itself.

Skips the LLM call entirely (and its cost) when a channel has no SFX
uploaded — there's nothing to pick from, so there's nothing to ask.
"""
import json
import re
from typing import Any, Dict, List, Optional
from src.pipeline.ai_text import generate_text, any_text_provider_configured
from src.utils.logger import logger

SOUND_EFFECTS_MODEL = "claude-sonnet-5"

# One effect roughly every 12s of video, capped — a talking-head/narration
# video peppered with a sound effect every couple of seconds reads as
# gimmicky, not polished. This mirrors the "a real editor uses SFX
# sparingly, for emphasis" intent from the reference video that started
# this feature.
_SECONDS_PER_EFFECT = 12
_MAX_EFFECTS = 14


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def pick_sound_effect_cues(
    words: List[Dict[str, Any]],
    available_effects: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    words: transcript word list, each {"word": str, "start": float, "end": float}
        (same shape generate_ass_subtitles already consumes).
    available_effects: [{"id": str, "label": str, "filename": str, "duration_seconds": float|None}, ...]
        — exactly what GET /channels/{id}/sfx returns.

    Returns [{"start": float, "filename": str, "label": str}, ...], sorted by
    start time, never more than one every _SECONDS_PER_EFFECT seconds on
    average and never more than _MAX_EFFECTS total. Empty list (never None)
    on any failure or when there's nothing to pick from — callers can always
    treat the result as "however many effects to place, possibly zero"
    without a separate error branch.
    """
    if not words or not available_effects or not any_text_provider_configured():
        return []

    duration = float(words[-1].get("end", 0) or 0)
    if duration <= 0:
        return []

    max_effects = max(1, min(_MAX_EFFECTS, int(duration // _SECONDS_PER_EFFECT)))
    labels_by_lower = {e["label"].strip().lower(): e for e in available_effects if e.get("label")}
    if not labels_by_lower:
        return []

    effects_list = "\n".join(f'- "{e["label"]}"' for e in labels_by_lower.values())
    # Timestamps only every few words (not every single one) keeps the
    # transcript block compact for long videos while still giving the model
    # enough granularity to place a cue within a second or two of the right
    # moment.
    transcript_lines = []
    for i in range(0, len(words), 5):
        chunk = words[i:i + 5]
        text = " ".join(w.get("word", "") for w in chunk).strip()
        if text:
            transcript_lines.append(f"[{chunk[0].get('start', 0):.1f}s] {text}")
    transcript_block = "\n".join(transcript_lines)[:8000]

    instruction = f"""You are a sound editor placing sound effects into a video's transcript.

Available sound effects for this channel (use ONLY these exact labels, never invent your own):
{effects_list}

Transcript with timestamps (seconds into the video):
{transcript_block}

Task: pick up to {max_effects} moments where one of the effects above would genuinely enhance the video — a transition, an emphasis, a punchline, a topic change. Be selective: a real editor uses sound effects sparingly, not on every sentence. Skip this entirely for lines where nothing fits rather than forcing a weak match.

Respond with ONLY this JSON object, no other text:
{{"cues": [{{"time": 12.4, "effect_label": "exact label from the list above"}}, ...]}}
"cues" may have fewer than {max_effects} entries (including zero) if fewer good moments exist. Never exceed {max_effects}."""

    try:
        raw_text = generate_text(instruction, max_tokens=1200, model=SOUND_EFFECTS_MODEL, operation="sound_effect_matching")
        data = _extract_json(raw_text)
        cues = data.get("cues")
        if not isinstance(cues, list):
            return []
    except Exception as e:
        logger.warning(f"Sound effect matching failed, video renders without SFX: {e}")
        return []

    result: List[Dict[str, Any]] = []
    for cue in cues:
        try:
            time = float(cue.get("time"))
            label = str(cue.get("effect_label") or "").strip().lower()
        except (TypeError, ValueError):
            continue
        effect = labels_by_lower.get(label)
        if not effect or not (0 <= time <= duration):
            continue
        result.append({"start": time, "filename": effect["filename"], "label": effect["label"]})

    result.sort(key=lambda c: c["start"])
    return result[:max_effects]
