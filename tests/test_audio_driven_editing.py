from array import array
from pathlib import Path
from unittest.mock import patch

from src.pipeline.clip_builder import analyze_scene_audio_energy


def test_audio_energy_distinguishes_calm_and_loud_scenes(tmp_path: Path):
    pcm = array("h", [100] * 2000 + [12_000] * 2000).tobytes()
    completed = type("Result", (), {"stdout": pcm})()
    segments = [
        {"start": 0.0, "end": 1.0, "duration": 1.0},
        {"start": 1.0, "end": 2.0, "duration": 1.0},
    ]

    with patch("src.pipeline.clip_builder.subprocess.run", return_value=completed):
        scores = analyze_scene_audio_energy(tmp_path / "voice.mp3", segments)

    assert scores[0] == 0.0
    assert scores[1] == 1.0


def test_audio_energy_falls_back_to_neutral_when_decode_fails(tmp_path: Path):
    with patch("src.pipeline.clip_builder.subprocess.run", side_effect=RuntimeError("decode failed")):
        scores = analyze_scene_audio_energy(
            tmp_path / "voice.mp3",
            [{"start": 0.0, "end": 1.0, "duration": 1.0}],
        )

    assert scores == [0.5]
