from types import SimpleNamespace

from src.pipeline.youtube_compliance import evaluate_youtube_compliance


def video(script, title="Un titre YouTube suffisamment précis", description="Une description complète " * 8, id="new"):
    return SimpleNamespace(id=id, script_text=script, title=title, youtube_description=description)


def channel(niche="Histoire", description="Récits historiques documentés"):
    return SimpleNamespace(niche=niche, description=description)


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
