"""Authenticated encryption for provider credentials, independent of API keys."""
import os
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

PREFIX = 'enc:v1:'


def cipher():
    secret = os.environ.get('PROVIDER_KEY_ENCRYPTION_KEY', '')
    if not secret:
        raise RuntimeError('PROVIDER_KEY_ENCRYPTION_KEY must be configured on API and workers.')
    try:
        return Fernet(secret.encode('ascii'))
    except (ValueError, UnicodeError):
        raise RuntimeError('PROVIDER_KEY_ENCRYPTION_KEY must be a dedicated Fernet key.') from None


def encrypt(value):
    return PREFIX + cipher().encrypt(value.encode('utf-8')).decode('ascii')


def decrypt(value):
    if not value.startswith(PREFIX):
        raise RuntimeError('Provider credentials require the encryption migration before use.')
    try:
        return cipher().decrypt(value[len(PREFIX):].encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError):
        raise RuntimeError('Provider credential cannot be decrypted with the configured key.') from None


class ProviderSecret(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return decrypt(value) if value is not None else None


def migrate(connection):
    """Run only with all old API/worker processes stopped; transaction is atomic."""
    from sqlalchemy import text
    cipher()  # Fail before schema or data changes if the secret is missing.
    if connection.dialect.name == 'postgresql':
        connection.execute(text('LOCK TABLE huggingface_accounts IN ACCESS EXCLUSIVE MODE'))
        connection.execute(text('ALTER TABLE huggingface_accounts ALTER COLUMN token TYPE TEXT'))
    rows = connection.execute(text('SELECT id, token FROM huggingface_accounts')).all()
    count = 0
    for identifier, value in rows:
        if value.startswith(PREFIX):
            decrypt(value)  # Verify the deployment key before accepting the migration.
        else:
            connection.execute(text('UPDATE huggingface_accounts SET token=:token WHERE id=:id'), {'id': identifier, 'token': encrypt(value)})
            count += 1
    return count


def preflight(connection):
    """Read-only startup gate: reject missing secrets, legacy rows or old schema."""
    from sqlalchemy import inspect, text
    cipher()
    inspector = inspect(connection)
    if not inspector.has_table('huggingface_accounts'):
        return 0  # A fresh database will be initialized by the application.
    column = next(c for c in inspector.get_columns('huggingface_accounts') if c['name'] == 'token')
    if connection.dialect.name == 'postgresql' and getattr(column['type'], 'length', None):
        raise RuntimeError('Provider credential schema requires the encryption migration.')
    count = 0
    for value in connection.execute(text('SELECT token FROM huggingface_accounts')).scalars():
        decrypt(value)
        count += 1
    return count
