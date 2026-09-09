from unittest.mock import Mock
import httpx
import pytest
from src.pipeline import ai33_provider
from src.db import session


@pytest.fixture
def clock_db(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ai33_provider.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(ai33_provider.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = None
    monkeypatch.setattr(session, 'SessionLocal', lambda: db)
    return now, db


def test_missing_webhook_recovers_completed_task(clock_db):
    client = Mock()
    client.get.return_value = httpx.Response(200, json={'status': 'done', 'metadata': {'json_url': 'https://audio.test/transcript.json'}}, request=httpx.Request('GET', 'https://api.ai33.pro/v1/task/task'))
    result = ai33_provider.await_stt_webhook_result('task', client=client, api_key='test-key')
    assert result['json_url'].endswith('transcript.json')
    assert clock_db[0][0] == 16
    assert client.get.call_args.kwargs['headers'] == {'xi-api-key': 'test-key'}


def test_busy_poll_keeps_waiting_for_webhook(clock_db):
    now, db = clock_db
    client = Mock()
    client.get.side_effect = httpx.ConnectError('busy')
    db.query.return_value.filter.return_value.first.side_effect = lambda: Mock(status='done', payload={'metadata': {'text': 'Bonjour'}}) if now[0] >= 20 else None
    assert ai33_provider.await_stt_webhook_result('task', client=client) == {'text': 'Bonjour'}


def test_poll_latency_counts_toward_deadline(clock_db):
    now, _ = clock_db
    client = Mock()
    def slow_get(*args, **kwargs):
        now[0] += kwargs['timeout']
        raise httpx.ReadTimeout('slow')
    client.get.side_effect = slow_get
    with pytest.raises(TimeoutError):
        ai33_provider.await_stt_webhook_result('task', timeout=20, client=client)
    assert now[0] == 20


def test_webhook_ready_never_polls_provider(clock_db):
    _, db = clock_db
    db.query.return_value.filter.return_value.first.return_value = Mock(status='done', payload={'metadata': {'text': 'Bonjour'}})
    client = Mock()
    assert ai33_provider.await_stt_webhook_result('task', client=client) == {'text': 'Bonjour'}
    client.get.assert_not_called()
