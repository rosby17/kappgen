from src.pipeline.music_video import pick_music_video_title


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
