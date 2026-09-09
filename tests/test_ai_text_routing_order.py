from src.pipeline import ai_providers, ai_text, script_writer
from src.utils import provider_status
import pytest


def test_numbered_text_order_is_complete_chain(monkeypatch):
    monkeypatch.setattr("src.utils.app_settings.ai_text_provider_order", lambda: ["kie", "ollama"])
    monkeypatch.setattr(ai_providers, "ids_for", lambda capability: ["anthropic", "kie", "openai", "gemini", "ollama"])

    assert ai_providers.ordered_ids("text") == ["kie", "ollama"]


def test_preferred_provider_cannot_override_admin_order(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_providers, "ordered_ids", lambda capability: ["kie", "ollama"])
    monkeypatch.setattr("src.utils.app_settings.selected_task_model", lambda *args: None)
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


def test_kie_health_rejects_internal_unauthorized_response(monkeypatch):
    class Response:
        status_code = 200
        text = '{"code":401}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 401, "msg": "Unauthorized"}

    monkeypatch.setattr(provider_status, "_get_effective_key", lambda *args: "secret")
    monkeypatch.setattr("src.utils.app_settings.selected_task_model", lambda *args: "claude-sonnet-5")
    monkeypatch.setattr(provider_status.httpx, "post", lambda *args, **kwargs: Response())

    result = provider_status._check_kie()

    assert result["status"] == "error"
    assert "Authentification" in result["detail"]


@pytest.mark.parametrize(
    "model,path,response",
    [
        ("claude-sonnet-5", "/claude/v1/messages", {"content": [{"type": "text", "text": "Claude"}]}),
        ("gpt-5-6-luna", "/codex/v1/responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "GPT"}]}]}),
        ("gemini-3-8-flash", "/gemini/v1/models/gemini-3-8-flash:streamGenerateContent", {"candidates": [{"content": {"parts": [{"text": "Gemini"}]}}]}),
        ("grok-4-6", "/grok/v1/responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Grok"}]}]}),
    ],
)
def test_kie_selected_model_controls_endpoint_and_payload(monkeypatch, model, path, response):
    request = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return response

    def fake_post(url, **kwargs):
        request.update(url=url, payload=kwargs["json"])
        return Response()

    monkeypatch.setattr("src.pipeline.images._provider_accounts_from_db", lambda *args, **kwargs: [{"id": "key-1", "token": "secret"}])
    monkeypatch.setattr("src.pipeline.images._mark_provider_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_text.httpx, "post", fake_post)
    monkeypatch.setattr(ai_text, "log_usage", lambda *args, **kwargs: None)

    text, _ = ai_text._kie_complete("Bonjour", 50, {}, selected_model=model)

    assert request["url"].endswith(path)
    assert request["payload"].get("model", model) == model
    if model.startswith("gpt-"):
        assert request["payload"]["max_output_tokens"] == 50
    assert text
