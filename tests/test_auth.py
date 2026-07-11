import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.auth import ROLE_RANK, generate_key, hash_key  # noqa: E402


def test_role_hierarchy_ordering():
    assert ROLE_RANK["viewer"] < ROLE_RANK["editor"] < ROLE_RANK["admin"]


def test_hash_key_deterministic():
    k = "pak_abc123"
    assert hash_key(k) == hash_key(k)


def test_hash_key_differs_for_different_keys():
    assert hash_key("pak_abc123") != hash_key("pak_xyz789")


def test_generate_key_format_and_uniqueness():
    keys = {generate_key() for _ in range(100)}
    assert len(keys) == 100  # no collisions
    assert all(k.startswith("pak_") for k in keys)


def test_hash_never_stores_plaintext_recoverable():
    # sha256 output should not contain the raw key substring
    raw = "pak_supersecretvalue"
    assert raw not in hash_key(raw)
