import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.pipeline import voiceover, youtube_metadata, music_video


def test_generate_voiceover_reuses_existing_audio(tmp_path: Path):
    audio_file = tmp_path / "voiceover.mp3"
    audio_file.write_bytes(b"dummy audio content " * 100)
    transcript_file = tmp_path / "transcript.json"
    transcript_file.write_text('{"text": "Bonjour", "duration": 5.0, "words": [{"word": "Bonjour", "start": 0, "end": 1}]}', encoding="utf-8")

    with patch("src.utils.billing.debit_izivoice_usage_by_user_id") as mock_debit, \
         patch("src.pipeline.voiceover._configured_providers", return_value=["izivoice"]), \
         patch("src.pipeline.voiceover._tts_via_izivoice") as mock_tts:
        
        out_path, transcript = voiceover.generate_voiceover(
            "Bonjour", audio_file, user_id="u123", video_id="v456"
        )
        assert out_path == audio_file
        assert transcript["duration"] == 5.0
        mock_debit.assert_not_called()
        mock_tts.assert_not_called()


def test_generate_voiceover_refunds_on_all_providers_failure(tmp_path: Path):
    audio_file = tmp_path / "new_voiceover.mp3"

    with patch("src.utils.billing.debit_izivoice_usage_by_user_id", return_value=True) as mock_debit, \
         patch("src.utils.billing.refund_izivoice_usage_by_user_id") as mock_refund, \
         patch("src.pipeline.voiceover._configured_providers", return_value=["izivoice"]), \
         patch("src.pipeline.voiceover._tts_via_izivoice", side_effect=RuntimeError("API 500")):

        with pytest.raises(RuntimeError, match="Voiceover generation failed on every configured provider"):
            voiceover.generate_voiceover(
                "Bonjour", audio_file, user_id="u123", video_id="v456"
            )
        mock_debit.assert_called_once()
        mock_refund.assert_called_once()


def test_generate_thumbnail_reuses_existing_file(tmp_path: Path):
    thumb_file = tmp_path / "thumbnail.jpg"
    thumb_file.write_bytes(b"dummy image content " * 100)

    with patch("src.utils.billing.debit_izivoice_usage_by_user_id") as mock_debit, \
         patch("src.pipeline.youtube_metadata._generate_ai_thumbnail_background") as mock_ai:
        
        path, ai_used, provider = youtube_metadata.generate_thumbnail(
            tmp_path / "video.mp4", thumb_file, "Titre", channel=MagicMock(), video_id="v456"
        )
        assert path == thumb_file
        assert ai_used is True
        mock_debit.assert_not_called()
        mock_ai.assert_not_called()


def test_thumbnail_refunds_on_failure(tmp_path: Path):
    thumb_file = tmp_path / "thumb_new.jpg"
    channel = MagicMock()
    channel.user_id = "u123"
    channel.thumbnail_style = {}

    with patch("src.utils.app_settings.thumbnail_provider_order", return_value=["fal"]), \
         patch("src.utils.billing.debit_izivoice_usage_by_user_id", return_value=True) as mock_debit, \
         patch("src.utils.billing.refund_izivoice_usage_by_user_id") as mock_refund, \
         patch("src.pipeline.images.generate_thumbnail_image", side_effect=RuntimeError("fal timeout")):

        with pytest.raises(RuntimeError):
            youtube_metadata._generate_ai_thumbnail_background("Prompt", channel, thumb_file, video_id="v456")

        mock_debit.assert_called_once()
        mock_refund.assert_called_once()


def test_music_video_track_reuses_existing_file(tmp_path: Path):
    track_file = tmp_path / "track_0.mp3"
    track_file.write_bytes(b"dummy audio content " * 100)

    with patch("src.utils.billing.debit_izivoice_usage_by_user_id") as mock_debit, \
         patch("src.pipeline.music_video.generate_music_izivoice") as mock_gen:

        res = music_video._generate_audio_track("prompt", 0, tmp_path, user_id="u123", video_id="v456")
        assert res == track_file
        mock_debit.assert_not_called()
        mock_gen.assert_not_called()
