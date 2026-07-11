"""API-key based auth with a simple role hierarchy: viewer < editor < admin.

Design: keys are opaque random tokens the client sends via the `X-API-Key`
header. We store only sha256(key) in the DB, never the plaintext — so a DB
leak doesn't leak usable credentials. This is intentionally simple (no
sessions, no JWTs, no expiry beyond manual revocation) because the platform
is an internal engineering tool, not a consumer-facing product; the goal is
"you can't hit the API without a credential and mutating routes need the
right role," not enterprise SSO.

Role hierarchy:
  - viewer: read-only (GET everything)
  - editor: viewer + create prompts/versions, create/start/pause experiments,
            record events/judge-events
  - admin:  editor + activate/rollback versions, promote experiment winners,
            issue new API keys
"""

import hashlib
import os
import secrets

from app.database import get_db
from app.db_models import ApiKey
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


def _auth_disabled() -> bool:
    """Read fresh on every call rather than caching at import time. A
    module-level constant here would get 'locked in' to whatever
    os.environ held at first import of this module — surprising in any
    scenario involving reload or multi-module import ordering (this is
    exactly what caused test_integration.py's AUTH_DISABLED toggling to
    silently not take effect until the discovery of this bug)."""
    return os.environ.get("AUTH_DISABLED", "false").lower() == "true"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return "pak_" + secrets.token_urlsafe(32)  # "prompt-ab key"


class _DevKey:
    """Stand-in principal used only when AUTH_DISABLED=true."""

    role = "admin"
    name = "dev-mode"


def require_role(min_role: str):
    """Returns a FastAPI dependency that enforces the caller's key has at
    least `min_role` privilege."""
    if min_role not in ROLE_RANK:
        raise ValueError(f"unknown role '{min_role}'")

    def dependency(
        x_api_key: str | None = Header(default=None),
        db: Session = Depends(get_db),
    ) -> ApiKey:
        if _auth_disabled():
            return _DevKey()

        if not x_api_key:
            raise HTTPException(401, "missing X-API-Key header")

        key_hash = hash_key(x_api_key)
        key_row = db.query(ApiKey).filter(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None)).first()
        if not key_row:
            raise HTTPException(401, "invalid or revoked API key")

        if ROLE_RANK[key_row.role] < ROLE_RANK[min_role]:
            raise HTTPException(403, f"role '{key_row.role}' insufficient; requires '{min_role}' or higher")
        return key_row

    return dependency


# Convenience dependencies for route signatures.
require_viewer = require_role("viewer")
require_editor = require_role("editor")
require_admin = require_role("admin")
