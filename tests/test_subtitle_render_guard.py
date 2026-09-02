from pathlib import Path
from types import SimpleNamespace

import pytest

from src.pipeline import assembler


def _write_ass(path: Path, with_dialogue: bool = True) -> None:
    event = "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Bonjour\n" if with_dialogue else ""
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + event,
        encoding="utf-8",
    )


def test_ffmpeg_filter_detection_matches_filter_column(monkeypatch):
    output = """Filters:
 ... ass               V->V       Render ASS subtitles onto input video.
 ... unrelated         V->V       Description mentioning subtitles only.
"""
    monkeypatch.setattr(
        assembler.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )

    assert assembler.check_ffmpeg_filter("ass") is True
    assert assembler.check_ffmpeg_filter("subtitles") is False


def test_enabled_subtitles_reject_empty_ass(tmp_path, monkeypatch):
    ass_path = tmp_path / "subtitles.ass"
    _write_ass(ass_path, with_dialogue=False)

    with pytest.raises(RuntimeError, match="aucune ligne"):
        assembler.assemble_final_video(
            clip_paths=[tmp_path / "clip.mp4"],
            audio_path=tmp_path / "audio.mp3",
            subtitle_ass_path=ass_path,
            output_path=tmp_path / "output.mp4",
            subtitle_style={"enabled": True},
        )


def test_enabled_subtitles_reject_server_without_libass(tmp_path, monkeypatch):
    ass_path = tmp_path / "subtitles.ass"
    _write_ass(ass_path)
    monkeypatch.setattr(assembler, "check_ffmpeg_filter", lambda _name: False)

    with pytest.raises(RuntimeError, match="ne possède ni le filtre"):
        assembler.assemble_final_video(
            clip_paths=[tmp_path / "clip.mp4"],
            audio_path=tmp_path / "audio.mp3",
            subtitle_ass_path=ass_path,
            output_path=tmp_path / "output.mp4",
            subtitle_style={"enabled": True},
        )


def test_ass_filter_is_included_in_final_render(tmp_path, monkeypatch):
    clip_path = tmp_path / "clip.mp4"
    audio_path = tmp_path / "audio.mp3"
    ass_path = tmp_path / "subtitles.ass"
    clip_path.touch()
    audio_path.touch()
    _write_ass(ass_path)
    monkeypatch.setattr(assembler, "check_ffmpeg_filter", lambda name: name == "ass")
    monkeypatch.setattr(assembler, "WATERMARK_PATH", tmp_path / "missing-watermark.png")
    captured = {}

    def fake_run_ffmpeg(cmd):
        captured["cmd"] = cmd
        Path(cmd[-1]).touch()

    monkeypatch.setattr(assembler, "run_ffmpeg", fake_run_ffmpeg)

    assembler.assemble_final_video(
        clip_paths=[clip_path],
        audio_path=audio_path,
        subtitle_ass_path=ass_path,
        output_path=tmp_path / "output.mp4",
        effects_config={"watermark_enabled": False, "overlay_effects": []},
        subtitle_style={"enabled": True},
    )

    filter_graph = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "ass=filename=" in filter_graph
    assert (tmp_path / "output.mp4").exists()
