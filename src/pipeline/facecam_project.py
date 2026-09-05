"""Persistent, source-timed editing decisions for the Facecam studio.

Artifacts are separate from the immutable uploaded rush. API writers lock the
Video row; the worker exclusively owns the project while queued/rendering.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.config import STORAGE_PATH
from src.pipeline import facecam_cuts


class FacecamSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_before_render: bool = True
    silences: bool = True
    mistakes: bool = True
    captions: bool = True
    motion: bool = True
    broll: bool = False
    format: Literal["original", "vertical", "square", "landscape"] = "original"
    quality: Literal["draft", "master"] = "draft"
    card_style: Literal["minimal", "bold", "editorial"] = "minimal"
    accent_color: str = Field(default="#00c2ff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_family: Literal["DejaVu Sans", "Arial", "Inter", "Montserrat", "Roboto", "Poppins"] = "DejaVu Sans"
    caption_position: Literal["bottom", "center", "top"] = "bottom"
    words_per_line: int = Field(default=5, ge=2, le=10)


def now():
    return datetime.now(timezone.utc).isoformat()


def project_dir(video_id):
    # video_id must come from an owned database Video, never a file parameter.
    return STORAGE_PATH / "facecam" / str(video_id)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def settings_for(video, channel):
    branding = channel.branding or {}
    defaults = FacecamSettings().model_dump()
    if branding.get("accent_color"):
        from src.pipeline.facecam_cards import _safe_accent_color
        color = _safe_accent_color(branding["accent_color"])
        if len(color) == 7:
            defaults["accent_color"] = color
    if branding.get("font_family") in FacecamSettings.model_fields["font_family"].annotation.__args__:
        defaults["font_family"] = branding["font_family"]
    defaults.update(video.facecam_settings or {})
    return FacecamSettings(**defaults).model_dump()


def plan_cuts(words, duration, user_id=None, video_id=None):
    candidates = facecam_cuts.detect_mistake_candidates(words)
    groups = [
        ("silence", "Pause prolongée", facecam_cuts.plan_silence_cuts(words, duration)),
        ("stutter", "Mot répété", facecam_cuts.stutter_deletes(candidates)),
        ("retake", "Prise alternative retenue", facecam_cuts.pick_best_takes_via_llm(candidates, user_id, video_id)),
    ]
    cuts = []
    for kind, reason, ranges in groups:
        for start, end in ranges:
            start, end = max(0, start), min(duration, end)
            if end <= start:
                continue
            cuts.append({"id": f"cut-{len(cuts)}", "kind": kind, "start": start, "end": end,
                         "text": " ".join(w["text"] for w in words if start <= w["start"] < end),
                         "reason": reason, "enabled": True})
    return sorted(cuts, key=lambda cut: cut["start"])


def selected_ranges(project):
    settings = project["settings"]
    return facecam_cuts.merge_delete_ranges([
        (c["start"], c["end"]) for c in project["cuts"] if c["enabled"] and
        (settings["silences"] if c["kind"] == "silence" else settings["mistakes"] if c["kind"] != "manual" else True)
    ])


def source_to_output(time, keeps):
    offset = 0
    for start, end in keeps:
        if start <= time < end:
            return offset + time - start
        offset += end - start
    return None


def append_activity(project, message):
    project["activity"] = (project.get("activity", []) + [{"at": now(), "message": message}])[-100:]


def caption_segments(words, size=5):
    for i in range(0, len(words), size):
        chunk = words[i:i + size]
        yield {"start": chunk[0]["start"], "end": chunk[-1]["end"], "text": " ".join(w["text"] for w in chunk)}


def srt_text(words):
    def stamp(value):
        ms = round(max(0, value) * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    return "\n\n".join(f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}" for i, c in enumerate(caption_segments(words), 1)) + "\n"
