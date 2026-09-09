from unittest.mock import Mock
import pytest
from src.utils import ollama, provider_status
from src.pipeline import ai_text, images, vision


def test_access_headers_require_pair(monkeypatch):
    monkeypatch.setattr(ollama.config, 'OLLAMA_ACCESS_CLIENT_ID', 'client')
    monkeypatch.setattr(ollama.config, 'OLLAMA_ACCESS_CLIENT_SECRET', '')
    with pytest.raises(ValueError):
        ollama.request_headers()
    monkeypatch.setattr(ollama.config, 'OLLAMA_ACCESS_CLIENT_SECRET', 'secret')
    assert ollama.request_headers()['CF-Access-Client-Secret'] == 'secret'


@pytest.mark.parametrize('content,reasoning,expected', [
    ('<think>private</think>Bonjour', '', 'Bonjour'),
    ('<think>unfinished', '', None),
    ('', 'private reasoning', None),
])
def test_generation_never_returns_reasoning(monkeypatch, content, reasoning, expected):
    monkeypatch.setattr(images, '_provider_accounts_from_db', lambda *a, **k: [{'id': None, 'token': 'http://localhost:11434'}])
    monkeypatch.setattr(images, '_mark_provider_account', lambda *a: None)
    monkeypatch.setattr(ai_text, 'log_usage', Mock())
    response = Mock()
    response.json.return_value = {'message': {'content': content, 'reasoning': reasoning}}
    post = Mock(return_value=response)
    monkeypatch.setattr(ai_text.httpx, 'post', post)
    if expected:
        assert ai_text._ollama_complete('hello', 10, {}) == (expected, 0.0)
        assert post.call_args.args[0].endswith('/api/chat')
        assert post.call_args.kwargs['json']['options']['num_predict'] == 10
        assert post.call_args.kwargs['json']['think'] is False
    else:
        with pytest.raises(RuntimeError):
            ai_text._ollama_complete('hello', 10, {})


def test_missing_model_is_not_healthy(monkeypatch):
    monkeypatch.setattr(provider_status, '_get_effective_key', lambda *a: 'http://localhost:11434')
    response = Mock()
    response.json.return_value = {'models': []}
    monkeypatch.setattr(provider_status.httpx, 'get', Mock(return_value=response))
    assert provider_status._check_ollama()['status'] == 'error'


def test_vision_forwards_images(monkeypatch):
    complete = Mock(return_value=('description', 0.0))
    monkeypatch.setattr(ai_text, '_ollama_complete', complete)
    payload = [(b'image', 'image/png')]
    assert vision._VISION_PROVIDERS['ollama'](payload, 'describe') == 'description'
    assert complete.call_args.kwargs['images'] == payload


def test_native_vision_request_uses_vision_model_and_base64(monkeypatch):
    monkeypatch.setattr(images, '_provider_accounts_from_db', lambda *a, **k: [{'id': None, 'token': 'http://localhost:11434'}])
    monkeypatch.setattr(images, '_mark_provider_account', Mock())
    monkeypatch.setattr(ollama.config, 'OLLAMA_VISION_MODEL', 'vision-model')
    monkeypatch.setattr(ollama, 'request_headers', lambda: {'CF-Access-Client-Id': 'client', 'CF-Access-Client-Secret': 'test-secret'})
    log = Mock()
    monkeypatch.setattr(ai_text, 'log_usage', log)
    response = Mock()
    response.json.return_value = {'message': {'content': 'Une image'}, 'prompt_eval_count': 12, 'eval_count': 3}
    post = Mock(return_value=response)
    monkeypatch.setattr(ai_text.httpx, 'post', post)
    assert ai_text._ollama_complete('describe', 20, {'operation': 'vision'}, images=[(b'image', 'image/png')]) == ('Une image', 0.0)
    payload = post.call_args.kwargs['json']
    assert payload['model'] == 'vision-model'
    assert payload['messages'][0]['images'] == ['aW1hZ2U=']
    assert post.call_args.kwargs['headers']['CF-Access-Client-Id'] == 'client'
    assert log.call_args.args[2:5] == (15, 'tokens', 0.0)
