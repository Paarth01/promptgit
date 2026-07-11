-- Adds API-key based auth with three roles: viewer < editor < admin.
-- Keys are stored as sha256 hashes, never plaintext.

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    key_hash        TEXT UNIQUE NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('viewer', 'editor', 'admin')),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_hash ON api_keys(key_hash) WHERE revoked_at IS NULL;
