from unittest.mock import Mock
import httpx
import pytest

from src.pipeline import voiceover, music, images
from src.utils import app_settings, media_providers, provider_status, provider_keys


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(provider_keys, 'key', lambda p, env='': {'ai33pro': 'ai33-db-key', 'izivoice': 'izi-db-key'}.get(p, ''))
    monkeypatch.setattr(app_settings, 'paid_apis_disabled', lambda: False)


@pytest.mark.parametrize('provider', ['ai33pro', 'izivoice'])
def test_tts_uses_saved_source_even_with_old_personal_key(monkeypatch, tmp_path, keys, provider):
    monkeypatch.setattr(app_settings, 'voiceover_provider_order', lambda: [provider])
    calls = []
    def tts(client, script, voice, settings, key):
        calls.append(key)
        return voice, 'https://audio.test/voice.mp3'
    monkeypatch.setattr(voiceover, '_tts_via_ai33', tts if provider == 'ai33pro' else Mock(side_effect=AssertionError('wrong source')))
    monkeypatch.setattr(voiceover, '_tts_via_izivoice', tts if provider == 'izivoice' else Mock(side_effect=AssertionError('wrong source')))
    monkeypatch.setattr(httpx.Client, 'get', lambda self, url, **kw: httpx.Response(200, content=b'audio', request=httpx.Request('GET', url)))
    monkeypatch.setattr(voiceover, 'get_audio_duration', lambda p: 10)
    monkeypatch.setattr(voiceover, 'log_usage', lambda *a, **kw: None)
    path, _ = voiceover.generate_voiceover('Bonjour', tmp_path / 'voice.mp3', voice_id='v', api_key='old-personal-key', transcribe=False)
    assert path.read_bytes() == b'audio'
    assert calls == ['ai33-db-key' if provider == 'ai33pro' else 'izi-db-key']


def test_transcription_obeys_kappgen_selection(monkeypatch, tmp_path, keys):
    monkeypatch.setattr(app_settings, 'voiceover_provider_order', lambda: ['ai33pro'])
    transcribe = Mock(return_value={'text': 'bonjour'})
    monkeypatch.setattr(voiceover, 'transcribe_audio_izivoice', transcribe)
    voiceover.generate_transcript_for_audio(tmp_path / 'audio.mp3', api_key='old-izi-key')
    assert transcribe.call_args.kwargs['provider'] == 'ai33pro'
    assert transcribe.call_args.kwargs['api_key'] == 'ai33-db-key'


@pytest.mark.parametrize('order', [[], ['ai33pro']])
def test_no_implicit_izivoice_fallback(monkeypatch, tmp_path, order):
    monkeypatch.setattr(app_settings, 'voiceover_provider_order', lambda: order)
    monkeypatch.setattr(app_settings, 'music_provider_order', lambda: order)
    monkeypatch.setattr(provider_keys, 'key', lambda p, env='': 'old-izi-key' if p == 'izivoice' else '')
    assert voiceover._configured_providers('old-personal-key') == []
    assert music._configured_music_providers() == []
    with pytest.raises(RuntimeError, match='générateur vocal'):
        voiceover.generate_voiceover('Bonjour', tmp_path / 'audio.mp3', api_key='old-personal-key')


@pytest.mark.parametrize('provider', ['ai33pro', 'izivoice'])
def test_music_dispatch_and_key_pool(monkeypatch, tmp_path, keys, provider):
    monkeypatch.setattr(app_settings, 'music_provider_order', lambda: [provider])
    target = tmp_path / 'music.mp3'
    for p, name in [('ai33pro', '_music_via_ai33'), ('izivoice', '_music_via_izivoice')]:
        monkeypatch.setattr(music, name, Mock(return_value=target) if p == provider else Mock(side_effect=AssertionError('wrong source')))
    assert music.generate_music_izivoice('piano', 30, target) == target


def test_background_music_does_not_require_izivoice(monkeypatch, tmp_path, keys):
    monkeypatch.setattr(app_settings, 'music_provider_order', lambda: ['ai33pro'])
    monkeypatch.setattr(music, 'IZIVOICE_API_KEY', '')
    monkeypatch.setattr(music, 'ASSETS_PATH', tmp_path)
    render = Mock(return_value=tmp_path / 'result.mp3')
    monkeypatch.setattr(music, 'generate_music_izivoice', render)
    assert music.get_background_music_track({'mode': 'ai_generate', 'ai_prompt': 'piano'}, 30) == tmp_path / 'result.mp3'
    render.assert_called_once()


@pytest.mark.parametrize('provider', ['ai33pro', 'izivoice'])
def test_thumbnail_obeys_selected_source(monkeypatch, tmp_path, provider):
    target = tmp_path / 'image.png'
    monkeypatch.setattr(images, '_generate_with_key_pool', lambda p, key, fn: fn(p + '-key'))
    for p, name in [('ai33pro', '_generate_with_ai33pro_image'), ('izivoice', 'generate_ai_image')]:
        monkeypatch.setattr(images, name, Mock(return_value=target) if p == provider else Mock(side_effect=AssertionError('wrong source')))
    assert images.generate_thumbnail_image('p', target, None, provider_order=[provider]) == (target, provider)


def test_empty_thumbnail_order_does_not_call_izivoice(tmp_path):
    with pytest.raises(RuntimeError, match='All thumbnail providers'):
        images.generate_thumbnail_image('p', tmp_path / 'image.png', None, provider_order=[])


@pytest.mark.parametrize('reader', [app_settings.voiceover_provider_order, app_settings.music_provider_order, app_settings.thumbnail_provider_order, app_settings.scene_image_provider_order])
def test_kappgen_default_preserves_explicit_izivoice_selection(monkeypatch, reader):
    monkeypatch.setattr(app_settings, 'paid_apis_disabled', lambda: False)
    monkeypatch.setattr(app_settings, 'get_setting', lambda key, default=None: None)
    assert reader() == ['ai33pro']
    monkeypatch.setattr(app_settings, 'get_setting', lambda key, default=None: '["izivoice"]')
    assert reader() == ['izivoice']
    monkeypatch.setattr(app_settings, 'get_setting', lambda key, default=None: '[]')
    assert reader() == []


def test_maintenance_cannot_be_bypassed_by_personal_key(monkeypatch, keys):
    monkeypatch.setattr(app_settings, 'paid_apis_disabled', lambda: True)
    assert voiceover._configured_providers('personal-izi-key') == []
    assert music._configured_music_providers() == []
