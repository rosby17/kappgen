import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.db.models import AppSetting, HuggingFaceAccount
from src.utils import provider_keys as keys


@pytest.fixture
def registry(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv('PROVIDER_KEY_ENCRYPTION_KEY', Fernet.generate_key().decode())
    engine = create_engine('sqlite://')
    AppSetting.__table__.create(engine)
    HuggingFaceAccount.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(keys, 'SessionLocal', factory)
    monkeypatch.setattr(keys, 'environment_keys', lambda provider: ['env-key'] if provider == 'openai' else [])
    return factory


def test_imported_key_is_real_and_deleted_key_never_resurrects(registry):
    first = keys.accounts('openai')
    assert len(first) == 1 and first[0]['id']
    db = registry()
    row = db.get(HuggingFaceAccount, first[0]['id'])
    assert row.status == 'unverified'
    db.delete(row); db.commit(); db.close()
    assert keys.accounts('openai') == []


def test_disabled_pool_never_falls_back_to_environment(registry):
    first = keys.accounts('openai')
    db = registry(); row = db.get(HuggingFaceAccount, first[0]['id']); row.is_enabled = False
    db.commit(); db.close()
    assert keys.key('openai') == ''


def test_round_robin_and_modification_take_effect_on_next_call(registry):
    keys.accounts('openai')
    db = registry(); db.add(HuggingFaceAccount(provider='openai', token='second', status='unverified'));db.commit();db.close()
    assert keys.run('openai', lambda token: token) == 'second'
    assert keys.run('openai', lambda token: token) == 'env-key'
    db=registry();r=keys.find_token(db, 'second');r.token='replacement';db.commit();db.close()
    assert keys.run('openai', lambda token: token) == 'replacement'


def test_auth_failure_tries_next_key_and_does_not_expose_secret(registry):
    keys.accounts('openai')
    db=registry();db.add(HuggingFaceAccount(provider='openai',token='bad-secret',status='unverified'));db.commit();db.close()
    def call(token):
        if token == 'bad-secret':
            response=httpx.Response(401,request=httpx.Request('GET','https://example.test/?key='+token))
            response.raise_for_status()
        return 'success'
    assert keys.run('openai',call) == 'success'
    db=registry();bad=keys.find_token(db, 'bad-secret')
    assert bad.status == 'invalid' and 'bad-secret' not in bad.last_error
    db.close()


def test_probe_receives_exact_requested_key(monkeypatch):
    from src.utils import provider_status
    monkeypatch.setattr(provider_status, '_check_openai', lambda: {'status':'ok' if provider_status._get_effective_key('openai','wrong-env') == 'requested' else 'error'})
    assert keys.check('openai','requested') == ('active',None)


def test_refresh_all_persists_each_key_and_aggregates_provider_state(registry, monkeypatch):
    keys.accounts('openai')
    db = registry()
    db.add(HuggingFaceAccount(provider='openai', token='second-key', status='unverified'))
    db.add(HuggingFaceAccount(provider='groq', token='quota-key', status='unverified'))
    db.commit(); db.close()
    monkeypatch.setattr(keys, 'check', lambda provider, token: ('active', None) if provider == 'openai' else ('quota_exhausted', 'Solde insuffisant.'))
    result = keys.refresh_all()
    summary = {row['id']: row for row in result['providers']}
    assert result['checked'] == 3
    assert summary['openai']['status'] == 'ok'
    assert summary['groq']['status'] == 'quota_exhausted'
    assert summary['anthropic']['status'] == 'not_configured'
    db = registry()
    assert {row.status for row in db.query(HuggingFaceAccount).filter_by(provider='openai')} == {'active'}
    assert db.query(HuggingFaceAccount).filter_by(provider='groq').one().status == 'quota_exhausted'
    db.close()


def test_network_failure_is_not_invalid_key(registry):
    with pytest.raises(RuntimeError):
        keys.run('openai',lambda token: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    db=registry(); assert db.query(HuggingFaceAccount).one().status == 'error';db.close()


def test_env_mirror_edit_delete_and_unrelated_values(registry, monkeypatch, tmp_path):
    path=tmp_path/'worker.env'
    path.write_text('OTHER_SETTING=keep\nOPENAI_API_KEY="obsolete"\n')
    monkeypatch.setenv('PROVIDER_KEYS_ENV_FILE',str(path))
    keys.accounts('openai')
    assert keys.sync_environment_file('openai') == 'synced'
    assert 'obsolete' not in path.read_text() and 'OTHER_SETTING=keep' in path.read_text()
    db=registry();db.query(HuggingFaceAccount).delete();db.commit();db.close()
    assert keys.sync_environment_file('openai') == 'synced'
    assert 'OPENAI_API_KEY=""' in path.read_text()
    assert keys.accounts('openai') == []


def test_openai_client_uses_rotating_database_keys(registry, monkeypatch):
    from src.pipeline import ai_text
    from unittest.mock import Mock
    keys.accounts('openai')
    db=registry();db.add(HuggingFaceAccount(provider='openai',token='other',status='unverified'));db.commit();db.close()
    seen=[]
    def post(url,**kwargs):
        seen.append(kwargs['headers']['Authorization'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'Bonjour'}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr(ai_text.httpx,'post',post)
    monkeypatch.setattr(ai_text,'log_usage',Mock())
    ai_text._openai_complete('hello',10,{})
    ai_text._openai_complete('hello',10,{})
    assert seen == ['Bearer other','Bearer env-key']


def test_different_worker_environment_imports_only_new_credentials(registry, monkeypatch):
    first = keys.accounts('openai')[0]
    db = registry(); db.delete(db.get(HuggingFaceAccount, first['id'])); db.commit(); db.close()
    monkeypatch.setattr(keys, 'environment_keys', lambda provider: ['env-key', 'worker-key'])
    assert [r['token'] for r in keys.accounts('openai')] == ['worker-key']


def test_gemini_probe_checks_exact_key_and_classifies_unauthorized(monkeypatch):
    def get(url, **kwargs):
        assert kwargs['params']['key'] == 'specific-key'
        return httpx.Response(401, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    assert keys.check('gemini', 'specific-key')[0] == 'invalid'


def test_admin_crud_changes_runtime_registry(registry, monkeypatch):
    from src.api.routes import admin
    monkeypatch.setattr(keys, 'check', lambda provider, token: ('active', None))
    monkeypatch.delenv('PROVIDER_KEYS_ENV_FILE', raising=False)
    db = registry()
    added = admin.add_hf_account(admin.HfAccountPayload(provider='openai', token='ui-added'), admin=None, db=db)
    assert added['environment_sync'] == 'not_configured'
    assert 'token' not in added
    updated = admin.update_hf_account(added['id'], payload=admin.ProviderKeyUpdate(token='ui-edited'), admin=None, db=db)
    assert updated['status'] == 'active'
    assert 'ui-edited' in [r['token'] for r in keys.accounts('openai')]
    admin.update_hf_account(added['id'], payload=admin.ProviderKeyUpdate(is_enabled=False), admin=None, db=db)
    assert 'ui-edited' not in [r['token'] for r in keys.accounts('openai')]
    admin.delete_hf_account(added['id'], admin=None, db=db)
    assert db.get(HuggingFaceAccount, added['id']) is None
    db.close()


def test_database_contains_only_authenticated_ciphertext(registry):
    from sqlalchemy import text
    from src.utils.provider_secret_storage import decrypt
    keys.accounts('openai')
    db = registry()
    stored = db.execute(text('SELECT token FROM huggingface_accounts')).scalar_one()
    assert stored.startswith('enc:v1:') and 'env-key' not in stored
    assert decrypt(stored) == 'env-key'
    db.close()


def test_missing_or_wrong_encryption_key_fails_closed(registry, monkeypatch):
    from cryptography.fernet import Fernet
    from src.utils.provider_secret_storage import encrypt, decrypt
    encrypted = encrypt('secret-value')
    monkeypatch.delenv('PROVIDER_KEY_ENCRYPTION_KEY')
    with pytest.raises(RuntimeError):
        encrypt('secret-value')
    monkeypatch.setenv('PROVIDER_KEY_ENCRYPTION_KEY', Fernet.generate_key().decode())
    with pytest.raises(RuntimeError):
        decrypt(encrypted)
    with pytest.raises(RuntimeError):
        decrypt('legacy-plaintext')


def test_migration_is_idempotent_and_preserves_values(registry):
    from sqlalchemy import text
    from src.utils.provider_secret_storage import migrate, decrypt
    db = registry()
    db.execute(text("INSERT INTO huggingface_accounts (id, provider, token, status, is_enabled) VALUES ('legacy', 'openai', 'old-secret', 'active', true)"))
    db.commit()
    with db.bind.begin() as connection:
        assert migrate(connection) == 1
        assert migrate(connection) == 0
        stored = connection.execute(text('SELECT token FROM huggingface_accounts')).scalar_one()
        assert decrypt(stored) == 'old-secret'
    db.close()


def test_startup_preflight_rejects_legacy_and_wrong_key(registry, monkeypatch):
    from sqlalchemy import text
    from cryptography.fernet import Fernet
    from src.utils.provider_secret_storage import preflight, migrate
    db = registry()
    with db.bind.begin() as connection:
        connection.execute(text("INSERT INTO huggingface_accounts (id, provider, token, status, is_enabled) VALUES ('legacy', 'openai', 'old-secret', 'active', true)"))
        with pytest.raises(RuntimeError):
            preflight(connection)
        assert connection.execute(text('SELECT token FROM huggingface_accounts')).scalar_one() == 'old-secret'
        migrate(connection)
        assert preflight(connection) == 1
        monkeypatch.setenv('PROVIDER_KEY_ENCRYPTION_KEY', Fernet.generate_key().decode())
        with pytest.raises(RuntimeError):
            preflight(connection)
    db.close()


def test_migration_rolls_back_on_mismatched_existing_ciphertext(registry, monkeypatch):
    from sqlalchemy import text
    from cryptography.fernet import Fernet
    from src.utils.provider_secret_storage import encrypt, migrate
    db = registry()
    old_ciphertext = encrypt('different-key-secret')
    with db.bind.begin() as connection:
        for identifier, value in [('first', 'plaintext-secret'), ('second', old_ciphertext)]:
            connection.execute(text("INSERT INTO huggingface_accounts (id, provider, token, status, is_enabled) VALUES (:id, 'openai', :token, 'active', true)"), {'id': identifier, 'token': value})
    monkeypatch.setenv('PROVIDER_KEY_ENCRYPTION_KEY', Fernet.generate_key().decode())
    with pytest.raises(RuntimeError):
        with db.bind.begin() as connection:
            migrate(connection)
    with db.bind.connect() as connection:
        assert connection.execute(text("SELECT token FROM huggingface_accounts WHERE id='first'")).scalar_one() == 'plaintext-secret'
    db.close()
