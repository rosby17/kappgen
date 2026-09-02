from types import SimpleNamespace

from src.pipeline.youtube_compliance import evaluate_audio_compliance, evaluate_script_compliance


def channel(niche="Philosophie", made_for_kids=False):
    return SimpleNamespace(niche=niche, description="", youtube_made_for_kids=made_for_kids)


def video(script, title="Une ancienne vidéo"):
    return SimpleNamespace(script_text=script, title=title)


def substantial_script(seed="idée originale"):
    return " ".join(f"Cette phrase développe {seed} avec un exemple concret numéro {index}." for index in range(30))


def test_original_substantial_script_is_green():
    report = evaluate_script_compliance(substantial_script(), "Comprendre une idée essentielle", channel())
    assert report["status"] == "green"
    assert report["can_render"] is True


def test_short_script_is_red_and_cannot_render():
    report = evaluate_script_compliance("Un texte beaucoup trop court.", "Sujet", channel())
    assert report["status"] == "red"
    assert report["can_render"] is False


def test_near_duplicate_script_is_red():
    script = substantial_script("stoïcienne")
    report = evaluate_script_compliance(script, "Nouvelle sagesse", channel(), [video(script + " conclusion")])
    assert report["status"] == "red"
    assert any(check["code"] == "originality" and check["state"] == "fail" for check in report["checks"])


def test_exact_duplicate_script_is_red():
    script = substantial_script("historique")
    report = evaluate_script_compliance(script, "Un autre titre", channel(), [video(script)])
    assert report["status"] == "red"


def test_sensitive_niche_requires_human_review():
    report = evaluate_script_compliance(substantial_script("financière"), "Comprendre les marchés", channel("Finance"))
    assert report["status"] == "orange"
    assert report["requires_human_review"] is True


def test_guaranteed_financial_claim_blocks_render():
    script = substantial_script("financière") + " Ce placement offre un profit garanti et sans aucun risque."
    report = evaluate_script_compliance(script, "Le placement parfait", channel("Finance"))
    assert report["status"] == "red"
    assert report["can_render"] is False


def test_untranscribed_audio_is_orange_and_requires_review():
    report = evaluate_audio_compliance({"transcription_source": "fallback"}, "Audio importé", channel())
    assert report["status"] == "orange"
    assert report["can_render"] is True
    assert report["requires_human_review"] is True


def test_transcribed_audio_uses_script_policy():
    report = evaluate_audio_compliance({
        "transcription_source": "izivoice",
        "text": substantial_script("audio"),
    }, "Narration originale", channel())
    assert report["status"] == "green"


def test_partial_audio_transcription_requires_review():
    report = evaluate_audio_compliance({
        "transcription_source": "izivoice",
        "transcription_partial": True,
        "text": substantial_script("audio partiel"),
    }, "Narration partielle", channel())
    assert report["status"] == "orange"
    assert report["requires_human_review"] is True


def test_third_party_audio_requires_review_even_when_transcribed():
    report = evaluate_audio_compliance({
        "transcription_source": "izivoice",
        "text": substantial_script("documentaire"),
    }, "Documentaire commenté", channel(), source_type="third_party")
    assert report["status"] == "orange"
    assert report["can_render"] is True
    assert report["requires_human_review"] is True
