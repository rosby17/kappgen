from src.pipeline.music_video import pick_music_video_title
from src.worker import queue_runner


def test_music_titles_rotate_creator_examples_before_repeating():
    title = pick_music_video_title(
        "lofi nocturne",
        "Focus calme\nNuit lofi\nCafé tardif",
        ["Focus calme", "Café tardif"],
    )

    assert title == "Nuit lofi"


def test_music_titles_have_a_free_style_based_fallback():
    title = pick_music_video_title("ambient piano doux pour le soir", None, ["Ancien titre"])

    assert title == "Ambient Piano Doux Pour Le Soir — Session 2"


def test_music_duration_keeps_the_selected_seconds_and_avoids_round_outputs(monkeypatch):
    monkeypatch.setattr(queue_runner.random, "choice", lambda offsets: -9)

    assert queue_runner._music_video_duration_seconds({"target_duration_seconds": 600}) == 591
