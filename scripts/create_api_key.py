"""Creates the first admin API key directly against the database.
Run once after the schema is migrated, before anyone can use the
key-issuing endpoint (which itself requires an admin key — this script
breaks that chicken-and-egg problem).

Usage:
    DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/prompt_ab \\
        python scripts/create_api_key.py --name "paarth-admin" --role admin
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.auth import generate_key, hash_key  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.db_models import ApiKey  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=["viewer", "editor", "admin"], default="admin")
    parser.add_argument("--created-by", default="bootstrap-script")
    args = parser.parse_args()

    raw_key = generate_key()
    db = SessionLocal()
    try:
        db.add(
            ApiKey(
                name=args.name,
                key_hash=hash_key(raw_key),
                role=args.role,
                created_by=args.created_by,
            )
        )
        db.commit()
    finally:
        db.close()

    print(f"Created {args.role} key '{args.name}'.")
    print(f"API key (shown once, store it now): {raw_key}")
    print(f"\nUse it as: -H 'X-API-Key: {raw_key}'")


if __name__ == "__main__":
    main()
