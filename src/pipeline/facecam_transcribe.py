"""Local word-level transcription for the facecam pipeline.

Uses faster-whisper (CTranslate2) instead of whisper.cpp so the whole facecam
pipeline stays pure Python — no Node/Chromium dependency gets introduced into
the backend (a deliberate decision: see facecam_editor.py's module docstring).
Runs entirely on this server, no API key required.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_MODEL = None
_MODEL_LOCK = threading.Lock()

# base.en/small would be faster but noticeably worse on non-English speech and
# on audio with background noise (webcam mics) — "small" is the floor for
# retake/mistake detection to be trustworthy, since a wrong word breaks the
# Jaccard-similarity match in facecam_cuts.py.
WHISPER_MODEL_SIZE = "small"


def _get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                from faster_whisper import WhisperModel

                _MODEL = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _MODEL


def transcribe_words(audio_path: Path, language: Optional[str] = None) -> Dict[str, Any]:
    """Returns {"words": [{"text", "start", "end"}], "duration": float}.

    Same shape (seconds, word-level) the facecam cut-planning and verification
    steps expect, and close enough to the transcript shape subtitles.py
    already consumes that a future switch to burning facecam captions can
    reuse generate_ass_subtitles() directly.
    """
    model = _get_model()
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )

    words: List[Dict[str, Any]] = []
    for segment in segments:
        for word in segment.words or []:
            text = (word.word or "").strip()
            if not text:
                continue
            words.append({"text": text, "start": float(word.start), "end": float(word.end)})

    return {"words": words, "duration": float(info.duration)}
