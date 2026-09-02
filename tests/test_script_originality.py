from src.pipeline.youtube_compliance import text_similarity


def test_identical_scripts_are_detected():
    script = "Une analyse détaillée avec plusieurs exemples et une conclusion originale. " * 30
    assert text_similarity(script, script) == 1.0


def test_distinct_scripts_remain_below_blocking_threshold():
    history = "L’empire romain organise ses routes, ses légions et ses institutions antiques. " * 30
    psychology = "Comprendre la projection demande d’observer les émotions, les relations et les limites personnelles. " * 30
    assert text_similarity(history, psychology) < 0.72
