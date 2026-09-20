from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.api.routes import videos


def _fake_session(video):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = video
    return db


def _local_video(tmp_path: Path, *, thumbnail_is_ai=False):
    video_dir = tmp_path / "channels" / "channel-1" / "videos" / "video-1"
    video_dir.mkdir(parents=True)
    (video_dir / "output.mp4").write_bytes(b"video")
    channel = SimpleNamespace(
        id="channel-1",
        name="Chaîne test",
        niche="prière",
        thumbnail_style={"reference_image_paths": ["reference.png"]},
    )
    video = SimpleNamespace(
        id="video-1",
        channel_id="channel-1",
        channel=channel,
        output_path="channels/channel-1/videos/video-1/output.mp4",
        storage_backend="local",
        script_text="Un script",
        title="Une prière",
        thumbnail_text="ANCIEN TEXTE",
        thumbnail_is_ai=thumbnail_is_ai,
        thumbnail_regenerating=True,
        thumbnail_updated_at=None,
        thumbnail_error="ancienne erreur",
    )
    return video, video_dir / "thumbnail.jpg"


def test_manual_thumbnail_regeneration_forces_a_new_image(tmp_path: Path):
    video, thumbnail = _local_video(tmp_path)
    old_bytes = b"old fallback thumbnail"
    new_bytes = b"new ai thumbnail"
    thumbnail.write_bytes(old_bytes)
    thumbnail.with_suffix(".ai.jpg").write_bytes(b"stale ai cache")
    db = _fake_session(video)

    def generate_new(_video_path, destination, *_args, **_kwargs):
        assert not destination.exists()
        assert not destination.with_suffix(".ai.jpg").exists()
        destination.write_bytes(new_bytes)
        return destination, True, "fal"

    with patch("src.db.session.SessionLocal", return_value=db), \
         patch.object(videos, "STORAGE_PATH", tmp_path), \
         patch.object(videos, "generate_contextual_thumbnail_headline", return_value="NOUVEAU TEXTE"), \
         patch.object(videos, "generate_thumbnail", side_effect=generate_new):
        videos._regenerate_thumbnail_background(video.id)

    assert thumbnail.read_bytes() == new_bytes
    assert video.thumbnail_is_ai is True
    assert video.thumbnail_error is None
    assert video.thumbnail_regenerating is False
    assert video.thumbnail_updated_at is not None
    assert list((thumbnail.parent / "thumbnail_history").glob("*.jpg"))


def test_failed_manual_thumbnail_regeneration_restores_previous_image(tmp_path: Path):
    video, thumbnail = _local_video(tmp_path, thumbnail_is_ai=False)
    old_bytes = b"old fallback thumbnail"
    thumbnail.write_bytes(old_bytes)
    db = _fake_session(video)

    with patch("src.db.session.SessionLocal", return_value=db), \
         patch.object(videos, "STORAGE_PATH", tmp_path), \
         patch.object(videos, "generate_contextual_thumbnail_headline", return_value="NOUVEAU TEXTE"), \
         patch.object(videos, "generate_thumbnail", side_effect=RuntimeError("provider unavailable")):
        videos._regenerate_thumbnail_background(video.id)

    assert thumbnail.read_bytes() == old_bytes
    assert video.thumbnail_is_ai is False
    assert video.thumbnail_error
    assert video.thumbnail_regenerating is False
    assert video.thumbnail_updated_at is None
