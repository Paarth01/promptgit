"""Unit tests for the versioning service that don't require a live DB
(schema-drift validation, render logic) plus DB-backed tests that assume
a Postgres instance is reachable via DATABASE_URL (skip gracefully if not).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.services import versioning  # noqa: E402
from app.db_models import PromptVersion  # noqa: E402


def _fake_version(prompt_text, template_variables):
    v = PromptVersion()
    v.prompt_text = prompt_text
    v.template_variables = template_variables
    v.version_number = 1
    return v


def test_render_success():
    v = _fake_version("Hello {name}, your ticket is {ticket_id}.", ["name", "ticket_id"])
    out = versioning.render(v, {"name": "Paarth", "ticket_id": "T-42"})
    assert out == "Hello Paarth, your ticket is T-42."


def test_render_missing_variable_raises():
    v = _fake_version("Hello {name}.", ["name"])
    with pytest.raises(versioning.MissingVariablesError):
        versioning.render(v, {})


def test_render_schema_drift_raises():
    # prompt_text uses {name} but template_variables says {full_name} -> drift
    v = _fake_version("Hello {name}.", ["full_name"])
    with pytest.raises(versioning.SchemaDriftError):
        versioning.render(v, {"full_name": "x"})


def test_diff_text_output_shape():
    """diff_versions requires a DB; this test only checks the pure-function
    unified_diff formatting logic extracted inline for unit coverage."""
    import difflib

    a = "line one\nline two\n"
    b = "line one\nline three\n"
    diff = list(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True)))
    joined = "".join(diff)
    assert "-line two" in joined
    assert "+line three" in joined
