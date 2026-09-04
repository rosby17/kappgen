"""Silence and mistake cut-planning for the facecam pipeline.

Ports the deterministic algorithms from the `editing-os` reference tool
(cut-silences + the mechanical half of cut-mistakes) to Python, plus a real
LLM call standing in for that tool's human "editorial read" of retake
candidates — KappGen has no operator watching each render, so the pick has to
be made by a model instead. See facecam_editor.py for how these compose.

All cut planning operates in SOURCE-file coordinates and is only ever applied
in a single ffmpeg pass against the original upload (never against an
already-cut render) to avoid re-encode drift accumulating across stages.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Silence planning (Snip) ------------------------------------------------

HEAD_PAD_SECONDS = 0.22
TAIL_PAD_SECONDS = 0.34
GAP_THRESHOLD_SECONDS = 0.55
SENTENCE_END_RE = re.compile(r"[.!?]$")


def _breath_room(gap: float, prev_word_text: str) -> float:
    """How much of a detected silence gap to leave in place as a natural
    breath, rather than cutting it out entirely — a hard cut on every pause
    reads as robotic. Biased toward the end of the preceding word (55/45)."""
    if gap >= 2.0:
        return 0.24
    if SENTENCE_END_RE.search(prev_word_text or ""):
        return 0.20
    return 0.14


def plan_silence_cuts(words: List[Dict[str, Any]], duration: float) -> List[Tuple[float, float]]:
    """Returns delete ranges (seconds, source timeline) for dead air: leading/
    trailing silence beyond the head/tail pad, and any inter-word gap at or
    above GAP_THRESHOLD_SECONDS (minus a kept breath allowance)."""
    if not words:
        return []

    deletes: List[Tuple[float, float]] = []

    if words[0]["start"] > HEAD_PAD_SECONDS:
        deletes.append((0.0, words[0]["start"] - HEAD_PAD_SECONDS))

    for prev, nxt in zip(words, words[1:]):
        gap = nxt["start"] - prev["end"]
        if gap >= GAP_THRESHOLD_SECONDS:
            breath = _breath_room(gap, prev["text"])
            cut_start = prev["end"] + breath * 0.45
            cut_end = nxt["start"] - breath * 0.55
            if cut_end > cut_start:
                deletes.append((cut_start, cut_end))

    last_end = words[-1]["end"]
    if duration - last_end > TAIL_PAD_SECONDS:
        deletes.append((last_end + TAIL_PAD_SECONDS, duration))

    return merge_delete_ranges(deletes)


def merge_delete_ranges(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def keep_ranges_from_deletes(deletes: List[Tuple[float, float]], duration: float) -> List[Tuple[float, float]]:
    keeps: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merge_delete_ranges(deletes):
        if start > cursor:
            keeps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keeps.append((cursor, duration))
    return [(s, e) for s, e in keeps if e - s > 0.01]


# --- Mechanical mistake detection (Redo, mechanical half) -------------------

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _split_segments(words: List[Dict[str, Any]], gap_threshold: float = 0.85) -> List[List[Dict[str, Any]]]:
    segments: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for prev, word in zip([None] + words[:-1], words):
        if prev is not None and word["start"] - prev["end"] > gap_threshold and current:
            segments.append(current)
            current = []
        current.append(word)
    if current:
        segments.append(current)
    return segments


def detect_mistake_candidates(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mechanical pass only — catches a minority of real retakes (adjacent
    stutters reliably; whole re-recorded sentences much less so). Each
    candidate is handed to the LLM editorial pick, not auto-applied."""
    candidates: List[Dict[str, Any]] = []

    for prev, nxt in zip(words, words[1:]):
        if prev["text"].lower() == nxt["text"].lower() and nxt["start"] - prev["end"] < 0.5:
            candidates.append({
                "type": "stutter",
                "start": prev["start"],
                "end": nxt["end"],
                "text": f"{prev['text']} {nxt['text']}",
                "keep_from": nxt["start"],
            })

    segments = _split_segments(words)
    for i, seg in enumerate(segments):
        seg_text = " ".join(w["text"] for w in seg)
        seg_tokens = _tokens(seg_text)
        seg_start, seg_end = seg[0]["start"], seg[-1]["end"]
        for j in range(i + 1, min(i + 9, len(segments))):
            other = segments[j]
            if other[0]["start"] - seg_end > 75:
                break
            other_text = " ".join(w["text"] for w in other)
            other_tokens = _tokens(other_text)
            score = _jaccard(seg_tokens, other_tokens)
            same_opening = " ".join(w["text"].lower() for w in seg[:4]) == " ".join(w["text"].lower() for w in other[:4])
            if score >= 0.34 or (score >= 0.24 and same_opening):
                kind = "false_start" if (seg_end - seg_start < 6.0 and other[0]["start"] - seg_end < 12.0) else "retake"
                candidates.append({
                    "type": kind,
                    "earlier": {"start": seg_start, "end": seg_end, "text": seg_text},
                    "later": {"start": other[0]["start"], "end": other[-1]["end"], "text": other_text},
                    "score": round(score, 3),
                })

    return candidates


# --- Editorial pick (LLM stands in for the human "editorial read") --------

def pick_best_takes_via_llm(
    candidates: List[Dict[str, Any]],
    user_id: Optional[str] = None,
    video_id: Optional[str] = None,
) -> List[Tuple[float, float]]:
    """For each retake/false_start candidate, asks an LLM which take to keep
    (usually the later one, but not always — a corrected fact or a cleaner
    delivery earlier can win). Returns delete ranges for the takes to drop.
    Stutters need no judgment call and are resolved mechanically below."""
    retake_candidates = [c for c in candidates if c["type"] in ("retake", "false_start")]
    if not retake_candidates:
        return []

    from src.pipeline.ai_text import generate_text

    prompt = (
        "Voici des paires de segments transcrits d'un même enregistrement vidéo, "
        "où l'orateur a probablement repris ou recommencé une phrase. Pour chaque "
        "paire (identifiée par son index), dis quelle version garder : "
        "\"earlier\" ou \"later\". Préfère la version la plus correcte et la plus "
        "fluide (pas toujours la dernière). Réponds uniquement en JSON strict : "
        '[{"index": 0, "keep": "later"}, ...].\n\n'
    )
    for i, c in enumerate(retake_candidates):
        prompt += f"{i}. earlier: \"{c['earlier']['text']}\"\n   later: \"{c['later']['text']}\"\n\n"

    try:
        raw = generate_text(
            prompt,
            max_tokens=1000,
            operation="facecam_editorial_pick",
            user_id=user_id,
            video_id=video_id,
        )
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        decisions = json.loads(match.group(0)) if match else []
    except Exception:
        logger.exception("facecam editorial pick failed, defaulting to keeping the later take for every candidate")
        decisions = [{"index": i, "keep": "later"} for i in range(len(retake_candidates))]

    deletes: List[Tuple[float, float]] = []
    decision_by_index = {d.get("index"): d.get("keep") for d in decisions if isinstance(d, dict)}
    for i, c in enumerate(retake_candidates):
        keep = decision_by_index.get(i, "later")
        drop = c["earlier"] if keep == "later" else c["later"]
        deletes.append((drop["start"], drop["end"]))

    return deletes


def stutter_deletes(candidates: List[Dict[str, Any]]) -> List[Tuple[float, float]]:
    return [(c["start"], c["keep_from"]) for c in candidates if c["type"] == "stutter"]


# --- Applying cuts (single ffmpeg pass from the original source) ----------

def build_trim_concat_filter(keep_ranges: List[Tuple[float, float]]) -> str:
    parts = []
    labels = []
    for i, (start, end) in enumerate(keep_ranges):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(keep_ranges)}:v=1:a=1[outv][outa]")
    return "".join(parts)


def apply_cuts(source_path: Path, keep_ranges: List[Tuple[float, float]], output_path: Path) -> Path:
    if not keep_ranges:
        raise ValueError("apply_cuts called with no keep ranges — refusing to produce an empty video")

    filter_complex = build_trim_concat_filter(keep_ranges)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(source_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def remap_words_to_output_timeline(words: List[Dict[str, Any]], keep_ranges: List[Tuple[float, float]]) -> List[Dict[str, Any]]:
    """Maps source-timeline word timestamps onto the post-cut output
    timeline, so captions/beat-detection for the cut render stay in sync.
    Words fully inside a deleted range are dropped."""
    remapped: List[Dict[str, Any]] = []
    offset = 0.0
    for keep_start, keep_end in keep_ranges:
        for word in words:
            if word["start"] >= keep_start and word["end"] <= keep_end:
                remapped.append({
                    "text": word["text"],
                    "start": word["start"] - keep_start + offset,
                    "end": word["end"] - keep_start + offset,
                })
        offset += keep_end - keep_start
    return remapped
