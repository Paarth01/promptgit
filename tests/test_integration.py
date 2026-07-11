"""Integration tests — the real thing, not unit tests in disguise.

These spin up the actual FastAPI app (via TestClient) against a live
Postgres + Redis, and drive it exactly the way a real client would: HTTP
requests in, HTTP responses out. No mocking of the DB, no monkeypatched
services. This is what proves versioning, traffic-split consistency, metric
recording, and significance/auto-promotion actually work together, not just
that each piece is correct in isolation.

Requires:
  - DATABASE_URL pointing at a Postgres with migrations 001-003 applied
  - REDIS_URL pointing at a reachable Redis
  - AUTH_DISABLED=true (set by this module before importing the app, so
    these tests don't need to bootstrap API keys to exercise the core flows;
    a dedicated auth-enabled test class covers RBAC enforcement separately)

If either service isn't reachable, the whole module is skipped rather than
failing noisily — these are integration tests, they're supposed to need
real infrastructure, and CI/sandbox environments without it shouldn't see
red for a reason unrelated to the code.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/prompt_ab_test",
)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ["AUTH_DISABLED"] = "true"
os.environ["HOLD_PERIOD_HOURS"] = "0.0003"  # ~1 second, so tests don't wait 24h
os.environ["AUTO_PROMOTE_ENABLED"] = "true"


def _services_available() -> bool:
    try:
        import redis as redis_lib
        import sqlalchemy

        engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"])
        with engine.connect():
            pass
        redis_lib.from_url(os.environ["REDIS_URL"]).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="requires live Postgres + Redis (see DATABASE_URL / REDIS_URL)",
)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_serve_cache():
    """The serving cache has a 5s TTL; force-clear it before each test so
    tests aren't order-dependent on cache staleness."""
    from app.services.serving import invalidate_cache

    invalidate_cache()
    yield


def unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── Registry: versioning, diff, rollback ────────────────────────────────


class TestRegistryIntegration:
    def test_create_prompt_and_first_version_auto_activates(self, client):
        slug = unique_slug("greeting")
        r = client.post("/prompts", json={"slug": slug, "description": "test prompt"})
        assert r.status_code == 201

        r = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "Hello {name}!",
                "template_variables": ["name"],
                "commit_message": "v1",
                "created_by": "integration-test",
            },
        )
        assert r.status_code == 201
        v1 = r.json()

        # First version should auto-activate even without activate=true.
        r = client.get(f"/prompts/{slug}/active")
        assert r.status_code == 200
        assert r.json()["id"] == v1["id"]

    def test_schema_drift_rejected_at_commit(self, client):
        slug = unique_slug("drift")
        client.post("/prompts", json={"slug": slug})
        r = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "Hello {name}!",
                "template_variables": ["wrong_var"],  # doesn't match {name}
                "commit_message": "bad commit",
                "created_by": "integration-test",
            },
        )
        assert r.status_code == 422

    def test_rollback_flips_active_pointer_and_audits(self, client):
        slug = unique_slug("rollback")
        client.post("/prompts", json={"slug": slug})
        r1 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "v1 text",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        )
        v1_id = r1.json()["id"]
        r2 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "v2 text",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
                "activate": True,
            },
        )
        v2_id = r2.json()["id"]

        active = client.get(f"/prompts/{slug}/active").json()
        assert active["id"] == v2_id

        r = client.post(
            f"/prompts/{slug}/activate",
            json={
                "version_id": v1_id,
                "actor": "test",
                "reason": "rolling back",
            },
        )
        assert r.status_code == 200
        assert r.json()["active_version_id"] == v1_id

        audit = client.get(f"/prompts/{slug}/audit-log").json()
        actions = [a["action"] for a in audit]
        assert "rollback" in actions

    def test_diff_shows_text_changes(self, client):
        slug = unique_slug("diff")
        client.post("/prompts", json={"slug": slug})
        client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "line one\nline two",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
            },
        )
        client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "line one\nline three",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
            },
        )
        r = client.get(f"/prompts/{slug}/diff", params={"from_": 1, "to": 2})
        assert r.status_code == 200
        body = r.json()
        assert "-line two" in body["prompt_text_diff"]
        assert "+line three" in body["prompt_text_diff"]


# ── Serving: transparent resolution, traffic-split consistency ─────────


class TestServingIntegration:
    def test_serve_resolves_active_version_with_no_experiment(self, client):
        slug = unique_slug("serve-simple")
        client.post("/prompts", json={"slug": slug})
        client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "Static reply for {topic}",
                "template_variables": ["topic"],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        )
        r = client.post(f"/serve/{slug}", json={"unit_id": "u1", "context": {"topic": "billing"}})
        assert r.status_code == 200
        body = r.json()
        assert body["resolved_prompt_text"] == "Static reply for billing"
        assert body["experiment_id"] is None

    def test_serve_missing_context_variable_fails_cleanly(self, client):
        slug = unique_slug("serve-missing-var")
        client.post("/prompts", json={"slug": slug})
        client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "Need {required_field}",
                "template_variables": ["required_field"],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        )
        r = client.post(f"/serve/{slug}", json={"unit_id": "u1", "context": {}})
        assert r.status_code == 422

    def test_serve_same_unit_id_gets_same_variant_repeatedly(self, client):
        """The core traffic-split consistency guarantee, proven against the
        real HTTP endpoint and real persisted assignments — not just the
        pure hash function in isolation."""
        slug = unique_slug("sticky")
        client.post("/prompts", json={"slug": slug})
        v1 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "baseline text",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        ).json()
        v2 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "variant text",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
            },
        ).json()

        exp = client.post(
            "/experiments",
            json={
                "prompt_slug": slug,
                "name": "sticky test",
                "primary_metric": "success",
                "metric_type": "binary",
                "target_sample_size": 1000,
                "created_by": "test",
                "variants": [
                    {
                        "label": "baseline",
                        "prompt_version_id": v1["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": True,
                    },
                    {
                        "label": "variant",
                        "prompt_version_id": v2["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": False,
                    },
                ],
            },
        ).json()
        client.post(f"/experiments/{exp['id']}/start")

        results_for_unit = set()
        for _ in range(5):
            r = client.post(f"/serve/{slug}", json={"unit_id": "sticky-user-1", "context": {}})
            results_for_unit.add(r.json()["variant_label"])
        assert len(results_for_unit) == 1, "same unit_id must always resolve to the same variant"

    def test_serve_distributes_across_both_variants_for_many_units(self, client):
        slug = unique_slug("distribution")
        client.post("/prompts", json={"slug": slug})
        v1 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "a",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        ).json()
        v2 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "b",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
            },
        ).json()
        exp = client.post(
            "/experiments",
            json={
                "prompt_slug": slug,
                "name": "distribution test",
                "primary_metric": "success",
                "metric_type": "binary",
                "target_sample_size": 1000,
                "created_by": "test",
                "variants": [
                    {"label": "a", "prompt_version_id": v1["id"], "traffic_weight": 0.5, "is_baseline": True},
                    {
                        "label": "b",
                        "prompt_version_id": v2["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": False,
                    },
                ],
            },
        ).json()
        client.post(f"/experiments/{exp['id']}/start")

        labels_seen = set()
        for i in range(40):
            r = client.post(f"/serve/{slug}", json={"unit_id": f"user-{i}", "context": {}})
            labels_seen.add(r.json()["variant_label"])
        assert labels_seen == {"a", "b"}, "40 distinct users across a 50/50 split should hit both variants"


# ── Full experiment lifecycle: metrics, significance, auto-promotion ────


class TestExperimentLifecycleIntegration:
    def test_full_lifecycle_reaches_auto_promoted_completion(self, client):
        """The end-to-end proof: create -> start -> serve+record events past
        target sample size -> wait out the (test-shortened) hold period ->
        results processing -> experiment auto-promotes and the prompt's
        active version actually changes."""
        slug = unique_slug("lifecycle")
        client.post("/prompts", json={"slug": slug})
        baseline_v = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "baseline",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        ).json()
        winner_v = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "winner",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
            },
        ).json()

        exp = client.post(
            "/experiments",
            json={
                "prompt_slug": slug,
                "name": "lifecycle test",
                "primary_metric": "success",
                "metric_type": "binary",
                "target_sample_size": 40,
                "created_by": "test",
                "variants": [
                    {
                        "label": "baseline",
                        "prompt_version_id": baseline_v["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": True,
                    },
                    {
                        "label": "winner",
                        "prompt_version_id": winner_v["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": False,
                    },
                ],
            },
        ).json()
        client.post(f"/experiments/{exp['id']}/start")

        variants = {v["label"]: v["id"] for v in exp["variants"]}

        # Serve + record a clear, unambiguous effect: baseline always fails,
        # winner always succeeds. 110 requests split ~50/50 comfortably
        # clears each variant's n=40 target even with hash imbalance, and
        # the effect size is large enough to be significant well past
        # p < 0.05 -- no flaky statistics here.
        for i in range(110):
            served = client.post(
                f"/serve/{slug}", json={"unit_id": f"lifecycle-user-{i}", "context": {}}
            ).json()
            success = 1 if served["variant_label"] == "winner" else 0
            client.post(
                "/events",
                json={
                    "unit_id": f"lifecycle-user-{i}",
                    "variant_id": served["variant_id"],
                    "primary_metric_value": success,
                    "is_error": False,
                },
            )

        # Hold period is ~1s in this test env; wait it out.
        time.sleep(1.5)

        # In production the worker's queue processing does this; here we
        # call /results directly, which triggers the same maybe_auto_promote
        # path -- proving the logic itself, independent of queue timing.
        r = client.get(f"/experiments/{exp['id']}/results")
        assert r.status_code == 200
        results = r.json()

        assert results["winner_variant_id"] == variants["winner"]
        assert results["status"] == "completed", (
            f"expected auto-promotion to complete the experiment, got status={results['status']!r}, "
            f"reason={results['winner_reason']!r}"
        )

        # The real proof: production traffic actually changed.
        active = client.get(f"/prompts/{slug}/active").json()
        assert active["id"] == winner_v["id"], "auto-promotion must actually flip the active version"

        audit = client.get(f"/prompts/{slug}/audit-log").json()
        assert any(a["actor"] == "auto-promotion-system" for a in audit)

    def test_guardrail_halts_on_error_spike(self, client):
        slug = unique_slug("guardrail")
        client.post("/prompts", json={"slug": slug})
        v1 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "a",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        ).json()
        v2 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "b",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
            },
        ).json()
        exp = client.post(
            "/experiments",
            json={
                "prompt_slug": slug,
                "name": "guardrail test",
                "primary_metric": "success",
                "metric_type": "binary",
                "target_sample_size": 1000,
                "created_by": "test",
                "variants": [
                    {"label": "a", "prompt_version_id": v1["id"], "traffic_weight": 0.5, "is_baseline": True},
                    {
                        "label": "b",
                        "prompt_version_id": v2["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": False,
                    },
                ],
            },
        ).json()
        client.post(f"/experiments/{exp['id']}/start")
        variants = {v["label"]: v["id"] for v in exp["variants"]}

        for i in range(30):
            client.post(
                "/events",
                json={
                    "unit_id": f"guard-user-{i}",
                    "variant_id": variants["b"],
                    "primary_metric_value": 1,
                    "is_error": True,  # 100% error rate on variant b
                },
            )
        for i in range(30):
            client.post(
                "/events",
                json={
                    "unit_id": f"guard-baseline-{i}",
                    "variant_id": variants["a"],
                    "primary_metric_value": 1,
                    "is_error": False,
                },
            )

        r = client.get(f"/experiments/{exp['id']}/results")
        assert r.json()["status"] == "stopped_guardrail"


# ── Custom metric collectors ─────────────────────────────────────────────


class TestCollectorIntegration:
    def test_task_accuracy_judge_event_records_correctly(self, client):
        slug = unique_slug("judge")
        client.post("/prompts", json={"slug": slug})
        v1 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "classify: {text}",
                "template_variables": ["text"],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        ).json()
        v2 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "classify carefully: {text}",
                "template_variables": ["text"],
                "commit_message": "v2",
                "created_by": "test",
            },
        ).json()
        exp = client.post(
            "/experiments",
            json={
                "prompt_slug": slug,
                "name": "judge test",
                "primary_metric": "accuracy",
                "metric_type": "binary",
                "target_sample_size": 100,
                "created_by": "test",
                "variants": [
                    {"label": "a", "prompt_version_id": v1["id"], "traffic_weight": 0.5, "is_baseline": True},
                    {
                        "label": "b",
                        "prompt_version_id": v2["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": False,
                    },
                ],
            },
        ).json()
        client.post(f"/experiments/{exp['id']}/start")
        variant_id = exp["variants"][0]["id"]

        r = client.post(
            "/events/judge",
            json={
                "unit_id": "judge-user-1",
                "variant_id": variant_id,
                "model_output": "billing",
                "reference_answer": "billing",
                "collector": "task_accuracy",
            },
        )
        assert r.status_code == 201
        assert r.json()["primary_metric_value"] == 1.0

    def test_llm_judge_without_api_key_fails_clearly(self, client, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        slug = unique_slug("judge-noauth")
        client.post("/prompts", json={"slug": slug})
        v1 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "x",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
        ).json()
        v2 = client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "y",
                "template_variables": [],
                "commit_message": "v2",
                "created_by": "test",
            },
        ).json()
        exp = client.post(
            "/experiments",
            json={
                "prompt_slug": slug,
                "name": "t",
                "primary_metric": "m",
                "metric_type": "binary",
                "target_sample_size": 10,
                "created_by": "test",
                "variants": [
                    {"label": "a", "prompt_version_id": v1["id"], "traffic_weight": 0.5, "is_baseline": True},
                    {
                        "label": "b",
                        "prompt_version_id": v2["id"],
                        "traffic_weight": 0.5,
                        "is_baseline": False,
                    },
                ],
            },
        ).json()
        client.post(f"/experiments/{exp['id']}/start")

        r = client.post(
            "/events/judge",
            json={
                "unit_id": "u1",
                "variant_id": exp["variants"][0]["id"],
                "model_output": "some output",
                "collector": "llm_judge",
            },
        )
        assert r.status_code == 422
        assert "ANTHROPIC_API_KEY" in r.json()["detail"]


# ── RBAC enforcement (auth NOT disabled for this class) ──────────────────


class TestAuthIntegration:
    @pytest.fixture(scope="class")
    def auth_client(self):
        """A separate TestClient with AUTH_DISABLED actually off, to prove
        role enforcement works against the real dependency-injection path.
        (auth.py reads AUTH_DISABLED fresh on every request, so flipping the
        env var here takes effect immediately — no module reload needed.)"""
        os.environ["AUTH_DISABLED"] = "false"
        from fastapi.testclient import TestClient
        from app.main import app

        yield TestClient(app)
        os.environ["AUTH_DISABLED"] = "true"

    def test_missing_api_key_rejected(self, auth_client):
        r = auth_client.get("/prompts")
        assert r.status_code == 401

    def test_invalid_api_key_rejected(self, auth_client):
        r = auth_client.get("/prompts", headers={"X-API-Key": "pak_not_a_real_key"})
        assert r.status_code == 401

    def test_viewer_key_cannot_create_prompt(self, auth_client):
        from app.auth import generate_key, hash_key
        from app.database import SessionLocal
        from app.db_models import ApiKey

        raw_key = generate_key()
        db = SessionLocal()
        db.add(ApiKey(name="test-viewer", key_hash=hash_key(raw_key), role="viewer", created_by="test"))
        db.commit()
        db.close()

        r = auth_client.post(
            "/prompts",
            json={"slug": unique_slug("should-fail")},
            headers={"X-API-Key": raw_key},
        )
        assert r.status_code == 403

    def test_editor_key_can_create_prompt_but_not_activate(self, auth_client):
        from app.auth import generate_key, hash_key
        from app.database import SessionLocal
        from app.db_models import ApiKey

        raw_key = generate_key()
        db = SessionLocal()
        db.add(ApiKey(name="test-editor", key_hash=hash_key(raw_key), role="editor", created_by="test"))
        db.commit()
        db.close()

        slug = unique_slug("editor-flow")
        r = auth_client.post("/prompts", json={"slug": slug}, headers={"X-API-Key": raw_key})
        assert r.status_code == 201

        r = auth_client.post(
            f"/prompts/{slug}/versions",
            json={
                "prompt_text": "x",
                "template_variables": [],
                "commit_message": "v1",
                "created_by": "test",
                "activate": True,
            },
            headers={"X-API-Key": raw_key},
        )
        assert r.status_code == 201
        v1_id = r.json()["id"]

        r = auth_client.post(
            f"/prompts/{slug}/activate",
            json={
                "version_id": v1_id,
                "actor": "test",
                "reason": "trying to rollback as editor",
            },
            headers={"X-API-Key": raw_key},
        )
        assert r.status_code == 403  # activate/rollback is admin-only
