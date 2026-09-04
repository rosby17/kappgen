"""Post-render verification for the facecam pipeline (Vera).

Never touches the video — only re-transcribes the rendered output and checks
it mechanically, so nothing ships on the assumption that ffmpeg did what it
was told. A failure routes the video to "needs_review" (see facecam_editor.py)
instead of "completed".
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

HEAD_MAX_SECONDS = 0.6
TAIL_MAX_SECONDS = 0.9
GAP_MAX_SECONDS = 0.75
DURATION_DRIFT_TOLERANCE = 0.5


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def verify_silence_sweep(words: List[Dict[str, Any]], duration: float) -> List[str]:
    failures = []
    if words:
        if words[0]["start"] > HEAD_MAX_SECONDS:
            failures.append(f"Silence de tête résiduel : {words[0]['start']:.2f}s")
        if duration - words[-1]["end"] > TAIL_MAX_SECONDS:
            failures.append(f"Silence de fin résiduel : {duration - words[-1]['end']:.2f}s")
        for prev, nxt in zip(words, words[1:]):
            gap = nxt["start"] - prev["end"]
            if gap > GAP_MAX_SECONDS:
                failures.append(f"Silence résiduel de {gap:.2f}s entre '{prev['text']}' et '{nxt['text']}'")
    return failures


def verify_cuts_sweep(rendered_text: str, approved_cuts: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    failures: List[str] = []
    warnings: List[str] = []
    normalized = rendered_text.lower()
    for cut in approved_cuts:
        removed_text = (cut.get("removes") or "").lower().strip()
        expected_count = cut.get("expect", 0)
        actual_count = normalized.count(removed_text) if removed_text else 0
        if actual_count != expected_count:
            failures.append(
                f"La phrase supprimée « {cut.get('removes')} » apparaît {actual_count} fois "
                f"dans le rendu final (attendu {expected_count})."
            )

    tokens = normalized.split()
    for prev, nxt in zip(tokens, tokens[1:]):
        if prev == nxt:
            warnings.append(f"Bégaiement résiduel possible : '{prev} {nxt}'")

    return failures, warnings


def verify_edl_ledger(source_duration: float, edited_duration: float, delete_ranges: List[Tuple[float, float]], rendered_path: Path) -> List[str]:
    failures = []
    removed_total = sum(end - start for start, end in delete_ranges)
    if delete_ranges != sorted(delete_ranges):
        failures.append("Les plages supprimées ne sont pas triées.")
    for (s1, e1), (s2, e2) in zip(delete_ranges, delete_ranges[1:]):
        if s2 < e1:
            failures.append(f"Plages supprimées qui se chevauchent : ({s1:.2f}-{e1:.2f}) et ({s2:.2f}-{e2:.2f})")
    if abs((source_duration - removed_total) - edited_duration) > 0.1:
        failures.append(
            f"Incohérence de durée attendue : source {source_duration:.2f}s - supprimé "
            f"{removed_total:.2f}s != montage attendu {edited_duration:.2f}s"
        )
    try:
        actual_duration = _ffprobe_duration(rendered_path)
        if abs(actual_duration - edited_duration) > DURATION_DRIFT_TOLERANCE:
            failures.append(
                f"Durée du fichier rendu ({actual_duration:.2f}s) s'écarte de la durée attendue "
                f"({edited_duration:.2f}s) au-delà de la tolérance ({DURATION_DRIFT_TOLERANCE}s)."
            )
    except Exception:
        failures.append("Impossible de lire la durée du fichier rendu (ffprobe a échoué).")
    return failures


def run_verification(
    rendered_path: Path,
    rendered_words: List[Dict[str, Any]],
    approved_cuts: List[Dict[str, Any]],
    source_duration: float,
    edited_duration: float,
    delete_ranges: List[Tuple[float, float]],
) -> Dict[str, Any]:
    """Three mechanical passes. Returns {"passed", "failures", "warnings"}."""
    rendered_text = " ".join(w["text"] for w in rendered_words)

    pass1 = verify_silence_sweep(rendered_words, edited_duration)
    pass2_failures, pass2_warnings = verify_cuts_sweep(rendered_text, approved_cuts)
    pass3 = verify_edl_ledger(source_duration, edited_duration, delete_ranges, rendered_path)

    failures = pass1 + pass2_failures + pass3
    return {"passed": not failures, "failures": failures, "warnings": pass2_warnings}
