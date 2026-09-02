from src.pipeline.images import TEXT_FREE_IMAGE_RULE, text_free_image_prompt
from src.pipeline.youtube_metadata import build_thumbnail_background_prompt


def test_text_free_rule_is_always_appended():
    result = text_free_image_prompt("a calm wellness scene")

    assert result.startswith("a calm wellness scene")
    assert TEXT_FREE_IMAGE_RULE in result
    assert "no words" in result
    assert "no logos" in result


def test_text_free_prompt_respects_provider_limit():
    result = text_free_image_prompt("x" * 5000)

    assert len(result) <= 4000
    assert result.endswith(TEXT_FREE_IMAGE_RULE)


def test_thumbnail_prompt_forces_a_large_subject_and_reserved_text_zone():
    result = build_thumbnail_background_prompt(
        "TON SILENCE EST UNE ARME",
        "Machiavel et psychologie",
        {"style_prompt": "Renaissance oil-paint collage, black and burnt gold", "text_side": "left"},
    )

    assert "reserve the left 48 percent" in result
    assert "main subject on the right" in result
    assert "45 to 60 percent" in result
    assert "never a tiny distant figure" in result
    assert "2 to 4 topic-specific secondary elements" in result
    assert "no empty generic hallway" in result


def test_thumbnail_prompt_puts_subject_opposite_right_hand_copy():
    result = build_thumbnail_background_prompt("LE PIÈGE", "psychology", {"text_side": "right"})

    assert "reserve the right 48 percent" in result
    assert "main subject on the left" in result
