"""Database-backed provider credentials shared by API and workers.

Environment credentials are imported once per credential fingerprint. A persistent marker
prevents a deleted or disabled credential from returning after a restart.
"""
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from hashlib import sha256
from src import config
from src.db.session import SessionLocal
from src.db.models import AppSetting, HuggingFaceAccount

ENV_NAMES = {
    'huggingface': ['HUGGINGFACE_API_KEY', 'HUGGINGFACE_API_KEYS'],
    'fal': ['FAL_API_KEY'], 'gemini': ['GEMINI_API_KEY', 'GEMINI_API_KEYS'],
    'anthropic': ['ANTHROPIC_API_KEY'], 'kie': ['KIE_API_KEY'],
    'openai': ['OPENAI_API_KEY'], 'deepseek': ['DEEPSEEK_API_KEY'],
    'groq': ['GROQ_API_KEY'], 'xai': ['XAI_API_KEY'],
    'izivoice': ['IZIVOICE_API_KEY'], 'ai33pro': ['AI33PRO_API_KEY'],
    'ollama': ['OLLAMA_BASE_URL'], 'openrouter': ['OPENROUTER_API_KEY'],
}
MANAGED_PROVIDERS = tuple(provider for provider in ENV_NAMES if provider != 'openrouter')
_current = ContextVar('provider_key_selection', default={})


def environment_keys(provider):
    result = []
    for name in ENV_NAMES.get(provider, []):
        value = getattr(config, name, '')
        values = value if isinstance(value, list) else str(value or '').split(',')
        for key in values:
            if key.strip() and key.strip() not in result:
                result.append(key.strip())
    return result


def find_token(db, token, exclude_id=None):
    # Randomized ciphertext cannot be compared in SQL. This small admin pool
    # is decrypted server-side only; never return plaintext from the API.
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text
        db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': 'provider-key-deduplication'})
    return next((row for row in db.query(HuggingFaceAccount).all()
                 if row.id != exclude_id and row.token == token), None)


def ensure_imported(db, provider):
    marker = 'provider_keys_managed:' + provider
    # Serialize migration across API/worker processes without exposing keys.
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text
        db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': marker})
    for key in environment_keys(provider):
        imported = 'provider_key_import:' + provider + ':' + sha256(key.encode()).hexdigest()
        if db.query(AppSetting).filter(AppSetting.key == imported).first():
            continue
        db.add(AppSetting(key=imported, value='true'))
        existing = find_token(db, key)
        if existing:
            if existing.provider != provider:
                raise RuntimeError('Une clé environnement est déjà associée à un autre fournisseur.')
            continue
        db.add(HuggingFaceAccount(provider=provider, token=key, label='Importée de l’environnement', status='unverified'))
    if not db.query(AppSetting).filter(AppSetting.key == marker).first():
        db.add(AppSetting(key=marker, value='true'))
    db.flush()


def accounts(provider, reserve=True):
    chosen = _current.get().get(provider)
    if chosen is not None:
        return [chosen]
    db = SessionLocal()
    try:
        ensure_imported(db, provider)
        query = db.query(HuggingFaceAccount).filter(
            HuggingFaceAccount.provider == provider, HuggingFaceAccount.is_enabled == True,
            HuggingFaceAccount.status != 'invalid',
        ).order_by(HuggingFaceAccount.last_used_at.asc().nullsfirst(), HuggingFaceAccount.id.asc())
        if reserve:
            query = query.with_for_update()
        rows = query.all()
        result = [{'id': r.id, 'token': r.token} for r in rows]
        if rows and reserve:
            rows[0].last_used_at = datetime.utcnow()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def key(provider):
    rows = accounts(provider, reserve=False)
    return rows[0]['token'] if rows else ''


def error_status(exc):
    response = getattr(exc, 'response', None)
    code = getattr(exc, 'status_code', None) or getattr(response, 'status_code', None)
    if code == 401:
        return 'invalid', 'Authentification refusée (HTTP 401).'
    if code == 403:
        return 'forbidden', 'Accès refusé (HTTP 403) ; droits du compte ou du modèle à vérifier.'
    if code == 402:
        return 'quota_exhausted', 'Solde insuffisant (HTTP 402).'
    if code == 429:
        return 'rate_limited', 'Limite de requêtes ou quota atteint (HTTP 429).'
    return 'error', f'Échec du service{f" (HTTP {code})" if code else ""}. Réessayez la vérification.'


def mark(account_id, status, error=None):
    if not account_id:
        return
    db = SessionLocal()
    try:
        row = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.id == account_id).first()
        if row:
            row.status = status
            row.last_checked_at = datetime.utcnow()
            row.last_used_at = datetime.utcnow()
            # Never store raw exception URLs or headers, which can include credentials.
            row.last_error = None if status == 'active' else (error or 'Vérification nécessaire.')
        db.commit()
    finally:
        db.close()


def run(provider, call):
    if provider in _current.get():
        return call(_current.get()[provider]['token'])
    candidates = accounts(provider)
    if not candidates:
        raise RuntimeError(f'Aucune clé utilisable pour {provider}. Configurez Ressources.')
    last = None
    for account in candidates:
        # Deletion/disable between attempts takes effect before the next call.
        db = SessionLocal()
        try:
            row = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.id == account['id'], HuggingFaceAccount.is_enabled == True).first()
            if not row or row.token != account['token']:
                continue
        finally:
            db.close()
        token = _current.set({**_current.get(), provider: account})
        try:
            result = call(account['token'])
            mark(account['id'], 'active')
            return result
        except Exception as exc:
            last = error_status(exc)
            mark(account['id'], *last)
        finally:
            _current.reset(token)
    raise RuntimeError(f'Toutes les clés {provider} ont échoué : {last[1] if last else "aucune clé active"}')


def rotating(provider):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return run(provider, lambda selected: fn(*args, **kwargs))
        return wrapped
    return decorate


def check(provider, token):
    """Run the existing provider probe against this exact key, never a sibling."""
    from src.utils import provider_status
    if provider in ('huggingface', 'gemini'):
        import httpx
        try:
            if provider == 'huggingface':
                response = httpx.get('https://huggingface.co/api/whoami-v2', headers={'Authorization': f'Bearer {token}'}, timeout=15)
            else:
                response = httpx.get('https://generativelanguage.googleapis.com/v1beta/models', params={'key': token}, timeout=15)
            response.raise_for_status()
            return 'active', None
        except Exception as exc:
            return error_status(exc)
    fn = getattr(provider_status, '_check_' + provider, None)
    if fn is None:
        return 'unverified', 'Statut vérifié au prochain appel réel.'
    selected = _current.set({**_current.get(), provider: {'id': None, 'token': token}})
    try:
        result = fn()
        status = {'ok': 'active', 'not_configured': 'unverified', 'quota_exhausted': 'quota_exhausted', 'invalid': 'invalid', 'forbidden': 'forbidden', 'rate_limited': 'rate_limited'}.get(result.get('status'), 'error')
        # Probe implementations may include upstream URLs; redact the exact key.
        detail = (result.get('detail') or '').replace(token, '[masqué]')
        return status, None if status == 'active' else detail[:300]
    finally:
        _current.reset(selected)


def refresh_all():
    """Re-check every configured key and return one truthful state per provider.

    This is deliberately an administrator action: a few providers use a tiny
    real request to prove that their configured model can still be called.
    """
    db = SessionLocal()
    try:
        for provider in MANAGED_PROVIDERS:
            ensure_imported(db, provider)
        db.commit()
        rows = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.is_enabled == True).all()
        checked = 0
        for row in rows:
            status, detail = check(row.provider, row.token)
            row.status = status
            row.last_error = None if status == 'active' else detail
            row.last_checked_at = datetime.utcnow()
            checked += 1
        db.commit()
        return {'checked': checked, 'providers': provider_summaries(db)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def provider_summaries(db=None):
    """Aggregate the latest per-key checks for routing badges.

    Green is emitted only if at least one enabled key completed a successful
    probe. Grey means no enabled key is configured or has not been checked.
    Any checked failure is red in the UI.
    """
    own_session = db is None
    db = db or SessionLocal()
    try:
        for provider in MANAGED_PROVIDERS:
            ensure_imported(db, provider)
        if own_session:
            db.commit()
        summaries = []
        for provider in MANAGED_PROVIDERS:
            rows = db.query(HuggingFaceAccount).filter(
                HuggingFaceAccount.provider == provider,
                HuggingFaceAccount.is_enabled == True,
            ).all()
            if not rows:
                status, detail = 'not_configured', 'Aucune clé active configurée.'
            elif any(row.status == 'active' for row in rows):
                status, detail = 'ok', 'Au moins une clé vérifiée peut traiter les requêtes.'
            elif any(row.status == 'unverified' for row in rows):
                status, detail = 'not_configured', 'Clé configurée, vérification requise.'
            elif any(row.status in ('quota_exhausted', 'rate_limited') for row in rows):
                status, detail = 'quota_exhausted', 'Aucune clé active avec quota disponible.'
            else:
                status, detail = 'error', 'Aucune clé active ne répond au contrôle.'
            summaries.append({'id': provider, 'status': status, 'configured': bool(rows), 'detail': detail})
        return summaries
    finally:
        if own_session:
            db.close()


def sync_environment_file(provider):
    """Mirror managed credentials to an explicitly mounted VPS env file.

    Container process environments are immutable. Runtime always reads the DB;
    the mirror is for subsequent process starts, not an alternative fallback.
    """
    import os
    import tempfile
    import re
    from pathlib import Path
    path = os.getenv('PROVIDER_KEYS_ENV_FILE', '').strip()
    if not path:
        return 'not_configured'
    db = SessionLocal()
    temp_path = None
    try:
        from sqlalchemy import text
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key':'provider-env-file-sync'})
        tokens = [r.token for r in db.query(HuggingFaceAccount).filter_by(provider=provider, is_enabled=True).order_by(HuggingFaceAccount.created_at).all()]
        names = ENV_NAMES[provider]
        if provider == 'ai33pro':
            names = names + ['AI_IMAGE_PROVIDER_API_KEY']
        target = Path(path)
        old = target.read_text() if target.exists() else ''
        kept = [line for line in old.splitlines() if not any(re.match(r'^\s*(?:export\s+)?'+re.escape(name)+r'\s*=', line) for name in names)]
        for name in names:
            value = ','.join(tokens) if name.endswith('_KEYS') else (tokens[0] if tokens else '')
            value = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
            kept.append(f'{name}="{value}"')
        fd, temp_path = tempfile.mkstemp(prefix='.provider-env-', dir=target.parent)
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(kept)+'\n')
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
        temp_path = None
        db.commit()
        return 'synced'
    except OSError:
        return 'error'
    finally:
        if temp_path:
            os.unlink(temp_path)
        db.close()
