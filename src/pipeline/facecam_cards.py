"""Animated title-card motion graphics for the facecam pipeline (Mo).

Uses HyperFrames (github.com/heygen-com/hyperframes, HTML/CSS/GSAP video
compositions rendered via headless Chrome) in *isolated* mode, per the
adoption plan recorded in ROADMAP.md: short transparent overlay clips
rendered separately, then composited onto the existing ffmpeg pipeline's
output via the overlay filter (see facecam_editor.py) — never a dependency
of the base render itself, and no change to the existing faceless pipeline.
This is a real Node.js + headless-Chromium dependency at render time (see
backend/Dockerfile); each card takes ~15-25s to render, acceptable for a
background job.
"""
from __future__ import annotations

import hashlib
import html
import logging
import random
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

POP_IN_SECONDS = 0.35
HOLD_SECONDS = 2.2
FADE_OUT_SECONDS = 0.35
CARD_DURATION_SECONDS = POP_IN_SECONDS + HOLD_SECONDS + FADE_OUT_SECONDS

MIN_GAP_BETWEEN_BEATS = 30.0
TEMPLATES = ["kicker_headline", "stat_bold", "lower_third"]

STAT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s?%|\$\s?\d+|\b\d+\s?(?:x|fois|times)\b", re.IGNORECASE)
SECTION_CUE_RE = re.compile(
    r"\b(next|d'abord|ensuite|enfin|first|second|third|premièrement|deuxièmement|"
    r"passons à|let's move on|voici|here's)\b", re.IGNORECASE,
)

_TEMPLATE_BODIES = {
    "kicker_headline": """
        <div id="kicker">•  SECTION SUIVANTE</div>
        <div id="headline">{text}</div>
    """,
    "stat_bold": """
        <div id="stat">{text}</div>
    """,
    "lower_third": """
        <div id="bar"></div>
        <div id="lower">{text}</div>
    """,
}

_TEMPLATE_CSS = {
    "kicker_headline": """
        #card{position:absolute;left:6%;top:70%;color:#fff;}
        #kicker{color:#00c2ff;font-weight:800;font-size:34px;letter-spacing:2px;}
        #headline{font-weight:800;font-size:58px;margin-top:12px;max-width:88%;}
    """,
    "stat_bold": """
        #card{position:absolute;left:0;top:8%;width:100%;text-align:center;color:#00c2ff;}
        #stat{font-weight:900;font-size:80px;}
    """,
    "lower_third": """
        #card{position:absolute;left:0;top:80%;width:100%;color:#fff;}
        #bar{position:absolute;inset:0;background:rgba(10,15,25,0.75);}
        #lower{position:relative;padding:22px 5%;font-weight:800;font-size:40px;}
    """,
}


def detect_beats(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mechanical candidate detector: numeric stats, section-transition cue
    phrases, and pacing gaps with nothing else happening for a while."""
    beats: List[Dict[str, Any]] = []
    last_beat_time = -MIN_GAP_BETWEEN_BEATS

    for i, word in enumerate(words):
        if word["start"] - last_beat_time < MIN_GAP_BETWEEN_BEATS:
            continue
        window = " ".join(w["text"] for w in words[max(0, i - 3):i + 4])
        if STAT_RE.search(window):
            beats.append({"time": max(0.0, word["start"] - 0.3), "kind": "stat_bold", "text": window.strip()})
            last_beat_time = word["start"]
        elif SECTION_CUE_RE.search(word["text"]):
            beats.append({"time": max(0.0, word["start"] - 0.3), "kind": "kicker_headline", "text": window.strip()})
            last_beat_time = word["start"]

    return beats


def select_card_templates(seed: str, count: int) -> List[str]:
    """Deterministic RNG rotation: same video always gets the same card
    styles on re-render, but never repeats the same template back to back."""
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
    pool = list(TEMPLATES)
    rng.shuffle(pool)
    picks = []
    last = None
    for i in range(count):
        candidates = [t for t in pool if t != last] or pool
        pick = candidates[i % len(candidates)]
        picks.append(pick)
        last = pick
    return picks


def _build_composition_html(template: str, text: str, size: tuple) -> str:
    width, height = size
    safe_text = html.escape(text[:70])
    body = _TEMPLATE_BODIES[template].format(text=safe_text)
    css = _TEMPLATE_CSS[template]
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"></script>
<style>
  html,body{{margin:0;background:transparent;width:{width}px;height:{height}px;overflow:hidden;
    font-family:'DejaVu Sans',Arial,sans-serif;}}
  {css}
</style>
</head>
<body>
<div id="root" data-composition-id="card" data-fps="30" data-duration="{CARD_DURATION_SECONDS:.2f}">
  <div id="card" class="clip" data-duration="{CARD_DURATION_SECONDS:.2f}">
    {body}
  </div>
</div>
<script>
  const tl = gsap.timeline({{paused:true}});
  tl.from("#card", {{opacity:0, y:40, scale:0.85, duration:{POP_IN_SECONDS}, ease:"back.out(1.7)"}});
  tl.to({{}}, {{duration:{HOLD_SECONDS}}});
  tl.to("#card", {{opacity:0, duration:{FADE_OUT_SECONDS}}});
  window.__timelines = window.__timelines || {{}};
  window.__timelines["card"] = tl;
</script>
</body>
</html>
"""


def render_card_clip(template: str, text: str, size: tuple, output_path: Path) -> Path:
    project_dir = output_path.parent / f"_hf_project_{output_path.stem}"
    project_dir.mkdir(parents=True, exist_ok=True)
    try:
        (project_dir / "index.html").write_text(_build_composition_html(template, text, size), encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "npx", "--yes", "hyperframes", "render", str(project_dir),
                "--format", "mov", "-o", str(output_path), "--quiet",
            ],
            check=True, capture_output=True, timeout=180,
        )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
    return output_path
