"""Orchestrates the facecam auto-editing pipeline end to end.

Stage order mirrors the `editing-os` reference tool's render contract: cut
silences -> cut mistakes -> verify (hard gate) -> b-roll -> motion-graphic
cards -> final mux. Deliberately Python + ffmpeg + Pillow only — no
Node/Chromium/HyperFrames dependency (see facecam_cards.py's docstring for
why that was ruled out for this backend).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sqlalchemy.orm import Session

from src.config import STORAGE_PATH
from src.db.models import Video, Channel
from src.pipeline import facecam_broll, facecam_cards, facecam_cuts, facecam_verify
from src.pipeline.audio_extract import ensure_extracted_audio
from src.pipeline.assembler import assemble_final_video
from src.pipeline.facecam_transcribe import transcribe_words
from src.utils.billing import FACECAM_EDIT_CREDITS, STOCK_MEDIA_CREDITS, debit_izivoice_usage_by_user_id

logger = logging.getLogger(__name__)


def _manifest_dir(video: Video) -> Path:
    d = STORAGE_PATH / "facecam" / video.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_delete_ranges(words: List[Dict[str, Any]], user_id: str, video_id: str) -> Tuple[List[Tuple[float, float]], List[Dict[str, Any]]]:
    """Combines silence cuts + mechanical stutter cuts + LLM-picked retake
    cuts into one merged delete-range list, plus the approved-cuts ledger
    verify_cuts_sweep checks against afterwards."""
    duration = words[-1]["end"] if words else 0.0
    silence_deletes = facecam_cuts.plan_silence_cuts(words, duration)

    candidates = facecam_cuts.detect_mistake_candidates(words)
    stutter_deletes = facecam_cuts.stutter_deletes(candidates)
    retake_deletes = facecam_cuts.pick_best_takes_via_llm(candidates, user_id=user_id, video_id=video_id)

    approved_cuts = [
        {"removes": " ".join(w["text"] for w in words if start <= w["start"] < end), "expect": 0}
        for start, end in stutter_deletes + retake_deletes
    ]

    all_deletes = facecam_cuts.merge_delete_ranges(silence_deletes + stutter_deletes + retake_deletes)
    return all_deletes, approved_cuts


def _composite_broll_and_cards(
    base_video: Path,
    words: List[Dict[str, Any]],
    db: Session,
    video_size: Tuple[int, int],
    output_path: Path,
) -> Path:
    triggers = [t for t in facecam_broll.detect_broll_triggers(words) if t.get("query")]
    beats = facecam_cards.detect_beats(words)
    templates = facecam_cards.select_card_templates(seed=str(output_path), count=len(beats))

    overlays: List[Dict[str, Any]] = []
    for trig in triggers:
        asset = facecam_broll.source_broll_asset(trig["query"], trig["kind"], db)
        if asset:
            overlays.append({"path": asset, "start": trig["time"], "duration": 3.0, "kind": "broll"})

    for beat, template in zip(beats, templates):
        card_path = output_path.parent / f"card_{beat['time']:.2f}.mov"
        facecam_cards.render_card_clip(template, beat["text"], video_size, card_path)
        overlays.append({"path": card_path, "start": beat["time"], "duration": facecam_cards.CARD_DURATION_SECONDS, "kind": "card"})

    if not overlays:
        return base_video

    inputs = ["-i", str(base_video)]
    filter_parts = ["[0:v]null[base0]"]
    base_label = "base0"
    for i, ov in enumerate(overlays):
        inputs += ["-i", str(ov["path"])]
        idx = i + 1
        next_label = f"base{i + 1}"
        if ov["kind"] == "broll":
            filter_parts.append(
                f"[{idx}:v]scale={video_size[0]}:{video_size[1]},setpts=PTS-STARTPTS+{ov['start']}/TB[ov{i}]"
            )
            filter_parts.append(
                f"[{base_label}][ov{i}]overlay=enable='between(t,{ov['start']},{ov['start'] + ov['duration']})'[{next_label}]"
            )
        else:
            filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{ov['start']}/TB[ov{i}]")
            filter_parts.append(
                f"[{base_label}][ov{i}]overlay=enable='between(t,{ov['start']},{ov['start'] + ov['duration']})'[{next_label}]"
            )
        base_label = next_label

    filter_complex = ";".join(filter_parts)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import subprocess
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{base_label}]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "copy",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def _probe_video_size(path: Path) -> Tuple[int, int]:
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, check=True,
    )
    width, height = out.stdout.strip().split("x")
    return int(width), int(height)


def run_facecam_pipeline(video_id: str, db: Session) -> None:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.raw_asset_path:
        raise ValueError(f"Facecam video {video_id} has no raw_asset_path")
    channel = db.query(Channel).filter(Channel.id == video.channel_id).first()

    source_path = STORAGE_PATH / video.raw_asset_path
    manifest_dir = _manifest_dir(video)

    debit_izivoice_usage_by_user_id(channel.user_id, FACECAM_EDIT_CREDITS, "facecam_edit", video_id=video.id)

    video.progress_stage = "transcription"
    db.commit()
    audio_path = ensure_extracted_audio(source_path)
    transcript = transcribe_words(audio_path)
    words = transcript["words"]
    (manifest_dir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2))

    video.progress_stage = "cuts"
    db.commit()
    delete_ranges, approved_cuts = _build_delete_ranges(words, user_id=channel.user_id, video_id=video.id)
    keep_ranges = facecam_cuts.keep_ranges_from_deletes(delete_ranges, transcript["duration"])
    edited_duration = sum(e - s for s, e in keep_ranges)

    cut_path = manifest_dir / "cut.mp4"
    facecam_cuts.apply_cuts(source_path, keep_ranges, cut_path)
    cut_words = facecam_cuts.remap_words_to_output_timeline(words, keep_ranges)

    video.progress_stage = "verification"
    db.commit()
    rendered_transcript = transcribe_words(ensure_extracted_audio(cut_path))
    report = facecam_verify.run_verification(
        rendered_path=cut_path,
        rendered_words=rendered_transcript["words"],
        approved_cuts=approved_cuts,
        source_duration=transcript["duration"],
        edited_duration=edited_duration,
        delete_ranges=delete_ranges,
    )
    (manifest_dir / "verify-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    if not report["passed"]:
        video.status = "needs_review"
        video.error_message = "Vérification des coupes échouée : " + "; ".join(report["failures"])
        db.commit()
        return

    video.progress_stage = "broll_and_cards"
    db.commit()
    video_size = _probe_video_size(cut_path)
    composited_path = manifest_dir / "composited.mp4"
    _composite_broll_and_cards(cut_path, cut_words, db, video_size, composited_path)

    for ov_path in manifest_dir.glob("card_*.mov"):
        ov_path.unlink(missing_ok=True)

    video.progress_stage = "final_mux"
    db.commit()
    final_audio = ensure_extracted_audio(composited_path)
    output_path = STORAGE_PATH / "videos" / f"{video.id}.mp4"
    assemble_final_video(
        clip_paths=[composited_path],
        audio_path=final_audio,
        subtitle_ass_path=manifest_dir / "no-subtitles.ass",
        output_path=output_path,
        branding_config=(channel.branding if channel else None),
        subtitles_preburned=True,
    )

    video.output_path = str(output_path.relative_to(STORAGE_PATH))
    video.status = "completed"
    video.duration_seconds = edited_duration
    db.commit()
