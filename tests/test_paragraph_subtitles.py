from pathlib import Path

from src.pipeline.subtitles import generate_ass_subtitles


def test_paragraph_mode_keeps_blocks_visible_and_wraps_lines(tmp_path: Path):
    words = [
        {"word": f"mot{i}", "start": float(i), "end": float(i) + 0.8}
        for i in range(12)
    ]
    output = tmp_path / "paragraph.ass"
    generate_ass_subtitles({"words": words, "duration": 12}, {
        "subtitle_mode": "paragraph",
        "paragraph_duration_seconds": 6,
        "paragraph_max_words": 6,
        "paragraph_words_per_line": 3,
        "align": "left",
        "position": "center",
    }, output)

    content = output.read_text()
    dialogues = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 2
    assert r"mot0 mot1 mot2\Nmot3 mot4 mot5" in dialogues[0]
    assert "0:00:00.00,0:00:05.80" in dialogues[0]
