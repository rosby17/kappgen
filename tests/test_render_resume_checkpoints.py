from pathlib import Path

from src.pipeline import orchestrator


def test_media_checkpoint_requires_non_empty_probeable_file(tmp_path, monkeypatch):
    artifact = tmp_path / "scene.mp4"
    assert orchestrator._media_checkpoint_is_valid(artifact, 10.0) is False

    artifact.touch()
    monkeypatch.setattr(orchestrator, "get_audio_duration", lambda _path: 10.0)
    assert orchestrator._media_checkpoint_is_valid(artifact, 10.0) is False


def test_media_checkpoint_accepts_matching_duration(tmp_path, monkeypatch):
    artifact = tmp_path / "scene.mp4"
    artifact.write_bytes(b"valid media placeholder")
    monkeypatch.setattr(orchestrator, "get_audio_duration", lambda _path: 10.4)

    assert orchestrator._media_checkpoint_is_valid(artifact, 10.0) is True


def test_media_checkpoint_rejects_truncated_duration(tmp_path, monkeypatch):
    artifact = tmp_path / "scene.mp4"
    artifact.write_bytes(b"partial media placeholder")
    monkeypatch.setattr(orchestrator, "get_audio_duration", lambda _path: 2.0)

    assert orchestrator._media_checkpoint_is_valid(artifact, 10.0) is False
