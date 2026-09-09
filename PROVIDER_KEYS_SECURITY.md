# Provider credential storage

Provider API keys use authenticated Fernet encryption before SQL writes. The
API returns only a masked preview; decryption happens inside API/worker memory.
`PROVIDER_KEY_ENCRYPTION_KEY` must be a dedicated randomly generated Fernet key,
shared by API and workers and stored outside the database and Git, preferably
in the deployment secret store. There is no provider-key fallback. Missing or
incorrect encryption keys fail closed. Back up the encryption key separately;
losing it makes the stored credentials unusable.

## Deployment migration (coordinated maintenance required)

1. Back up the database to protected storage; legacy backups contain plaintext
   provider credentials and must be access-controlled and expired appropriately.
2. Provision one Fernet key in the secret settings of every API and worker.
   Generate it with `cryptography.fernet.Fernet.generate_key()` in a secure
   administrative session; never paste the generated value into logs or Git.
3. Stop all API and worker processes running the old code. Do not perform a
   rolling deployment: old processes cannot read encrypted rows.
4. Using the new image and the provisioned secret, run
   `python -m scripts.encrypt_provider_keys`. It uses a transaction, widens the
   PostgreSQL column, encrypts plaintext rows and validates existing ciphertext.
5. Run `python -m scripts.encrypt_provider_keys --check` from each service
   configuration. This read-only check verifies the schema and decryptability
   without displaying values. The container entrypoint also runs this check
   and refuses to start on failure.
6. Start only the new API and workers; check masked listings and a provider call.
   To roll back after migration, use a coordinated database restore and previous
   image; never point the previous image at encrypted rows.

Runtime rotation selects another existing provider key; this is distinct from
revoking and replacing a compromised key at its provider. Encryption-key rotation
requires a separate coordinated re-encryption procedure; do not simply replace
this environment value.

## Environment file mirror

Leave `PROVIDER_KEYS_ENV_FILE` unset unless a plaintext mirror is explicitly
required. A mirror duplicates secrets in plaintext despite database encryption.
If enabled, restrict its parent directory and backups to the service account;
files are written atomically with mode 0600. It does not update Coolify settings
or process environments. The database remains the runtime source of truth.

## Protection boundary

This change protects credentials in a database-only disclosure. It cannot
protect them from an attacker controlling a running API/worker and its decryption
secret. Restrict database network access and privileges, protect administrator
accounts, use HTTPS, secure backups, and limit provider key scopes and spending.
Production infrastructure protections require their own verification.
