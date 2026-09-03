from types import SimpleNamespace

from src.pipeline import youtube_metadata


def _video(duration=620):
    return SimpleNamespace(
        id="video-1",
        title="Pourquoi Machiavel t'ordonne d'abandonner ceux qui ne te servent plus à rien",
        script_text=(
            "Machiavel ne demande pas de traiter les autres comme des objets. "
            "Il observe plutôt comment les alliances changent lorsque les intérêts divergent. "
            "Cette analyse distingue la loyauté, la dépendance et la réciprocité afin de comprendre "
            "quand une relation devient destructrice et comment poser des limites sans cruauté."
        ),
        duration_seconds=duration,
    )


def _channel():
    return SimpleNamespace(name="Clarté Invisible", niche="philosophie et psychologie")


def test_fallback_description_is_informative_and_chaptered(monkeypatch):
    monkeypatch.setattr(youtube_metadata, "any_text_provider_configured", lambda: False)

    result = youtube_metadata.generate_metadata(_video(), _channel())

    assert len(result["description"]) > 500
    assert "Chapitres" in result["description"]
    assert "0:00 Introduction" in result["description"]
    assert "Une vidéo originale" not in result["description"]


def test_generic_ai_description_is_replaced_by_rich_fallback(monkeypatch):
    monkeypatch.setattr(youtube_metadata, "any_text_provider_configured", lambda: True)
    monkeypatch.setattr(
        youtube_metadata,
        "generate_text",
        lambda *args, **kwargs: '{"title":"Titre propre","description":"Titre propre\\n\\nUne vidéo originale.","tags":[],"thumbnail_text":"CHOISIS TES ALLIÉS"}',
    )

    result = youtube_metadata.generate_metadata(_video(), _channel())

    assert "Chapitres" in result["description"]
    assert len(result["description"]) > 500


def test_chapter_anchors_start_at_zero_and_stay_bounded():
    anchors = youtube_metadata._chapter_anchors(1840)

    assert anchors[0] == 0
    assert 3 <= len(anchors) <= 9
    assert anchors == sorted(anchors)
    assert anchors[-1] < 1840


def test_fallback_copy_does_not_assume_an_analysis_niche(monkeypatch):
    monkeypatch.setattr(youtube_metadata, "any_text_provider_configured", lambda: False)
    video = _video()
    video.title = "Préparer un gâteau au chocolat"
    video.script_text = "Préchauffez le four. Mélangez ensuite le chocolat fondu avec les œufs et la farine avant de verser la pâte dans le moule."

    result = youtube_metadata.generate_metadata(video, SimpleNamespace(name="Cuisine Maison", niche="recettes"))

    assert "l’analyse" not in result["description"].casefold()
    assert "le sujet se construit étape par étape" in result["description"]
