from types import SimpleNamespace

from src.pipeline.youtube_compliance import evaluate_youtube_compliance, build_compliance_dossier


def video(script, title="Un titre YouTube suffisamment précis", description="Une description complète " * 8, id="new"):
    return SimpleNamespace(id=id, script_text=script, title=title, youtube_description=description)


def channel(niche="Général", description="Documentaires originaux", made_for_kids=False):
    return SimpleNamespace(niche=niche, description=description, youtube_made_for_kids=made_for_kids)


def test_original_substantial_video_is_green():
    script = " ".join(f"mot{i}" for i in range(500))
    report = evaluate_youtube_compliance(video(script), channel(), [])
    assert report["status"] == "green"
    assert report["can_auto_publish"] is True


def test_duplicate_script_is_blocked_red():
    script = "Une histoire originale avec des détails précis. " * 100
    previous = video(script, id="old")
    report = evaluate_youtube_compliance(video(script), channel(), [previous])
    assert report["status"] == "red"
    assert report["can_human_publish"] is False


def test_sensitive_niche_requires_human_review():
    script = " ".join(f"conseil{i}" for i in range(500))
    report = evaluate_youtube_compliance(video(script), channel("Santé"), [])
    assert report["status"] == "orange"
    assert report["requires_human_review"] is True


def test_financial_guarantee_is_blocked():
    script = ("Cette méthode offre un profit garanti et un rendement garanti. " * 80)
    report = evaluate_youtube_compliance(video(script), channel("Finance et trading"), [])
    assert report["status"] == "red"
    assert any(check["code"] == "dangerous_claim" and check["state"] == "fail" for check in report["checks"])


def test_kids_channel_requires_correct_youtube_declaration():
    script = " ".join(f"histoire{i}" for i in range(500))
    blocked = evaluate_youtube_compliance(video(script), channel("Comptines pour enfants"), [])
    allowed = evaluate_youtube_compliance(video(script), channel("Comptines pour enfants", made_for_kids=True), [])
    assert blocked["status"] == "red"
    assert allowed["status"] == "orange"


def test_source_link_is_recognised_for_history():
    script = " ".join(f"archive{i}" for i in range(500))
    item = video(script, description="Analyse historique détaillée. Source : https://example.org/archive")
    report = evaluate_youtube_compliance(item, channel("Histoire"), [])
    source_check = next(check for check in report["checks"] if check["code"] == "sources")
    assert source_check["state"] == "pass"


def test_traceability_dossier_records_known_provenance():
    item = video("contenu original " * 400, description="Source https://example.org/report")
    item.voice_id = "voice-123"
    item.youtube_compliance_report = {"score": 92, "status": "green"}
    item.youtube_compliance_history = [{"event": "check_completed"}]
    item.youtube_compliance_reviewed_at = None
    item.youtube_compliance_reviewed_by = None
    ch = channel()
    ch.image_style = {"sources": ["ai_generated", "library"], "style_prompt": "documentaire"}
    ch.music_preference = {"mode": "library", "tracks": ["channels/x/music/track.mp3"]}
    ch.voice_id = None
    dossier = build_compliance_dossier(item, ch)
    assert dossier["content"]["description_source_urls"] == ["https://example.org/report"]
    assert dossier["media"]["visual_sources"] == ["ai_generated", "library"]
    assert dossier["media"]["music_tracks"] == ["track.mp3"]
    assert dossier["youtube_declarations"]["contains_synthetic_media"] is True
