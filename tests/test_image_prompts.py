from src.pipeline.images import TEXT_FREE_IMAGE_RULE, text_free_image_prompt


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
