from src.pipeline import ai_providers, ai_text, script_writer


def test_numbered_text_order_is_complete_chain(monkeypatch):
    monkeypatch.setattr("src.utils.app_settings.ai_text_provider_order", lambda: ["kie", "ollama"])
    monkeypatch.setattr(ai_providers, "ids_for", lambda capability: ["anthropic", "kie", "openai", "gemini", "ollama"])

    assert ai_providers.ordered_ids("text") == ["kie", "ollama"]


def test_preferred_provider_cannot_override_admin_order(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_providers, "ordered_ids", lambda capability: ["kie", "ollama"])
    monkeypatch.setattr(ai_text, "_kie_complete", lambda *args, **kwargs: (calls.append("kie") or "ok", 0.0))

    result = ai_text.generate_text("test", preferred_provider="gemini")

    assert result == "ok"
    assert calls == ["kie"]


def test_script_usage_receives_channel_and_video_ids(monkeypatch):
    calls = []

    def fake_generate_text(prompt, **kwargs):
        calls.append(kwargs)
        return "Un texte de narration suffisamment long pour franchir la validation minimale. " * 3

    monkeypatch.setattr(script_writer, "any_text_provider_configured", lambda: True)
    monkeypatch.setattr(script_writer, "generate_text", fake_generate_text)

    result = script_writer.generate_daily_script(
        niche="Science",
        recent_titles=[],
        preset_title="Un sujet inédit",
        script_structure={
            "language": "French",
            "parts": [{"name": "partie", "word_count": 100, "guidance": "Expliquer le sujet."}],
            "formatting_rules": [],
            "cta_style": "",
        },
        user_id="user-1",
        channel_id="channel-1",
        video_id="video-1",
    )

    assert result is not None
    assert calls[0]["user_id"] == "user-1"
    assert calls[0]["channel_id"] == "channel-1"
    assert calls[0]["video_id"] == "video-1"
