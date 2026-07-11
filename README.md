# promptgit

[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-blue)](.github/workflows/ci.yml)

**Git for prompts.** Treats prompts as versioned artifacts, splits live traffic
across variants, runs continuous significance testing, and auto-promotes
statistically significant winners — the feature-flagging rigor of a mature
product org, applied to LLM prompt development instead of ad-hoc string edits
in production.

## Why this exists

Most teams change prompts by editing a string in prod and hoping for the
best. This platform treats a prompt change like any other risky production
change: versioned, tested against real traffic, measured for statistical
significance, and rolled back with one click if it's worse.

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│  Dashboard  │────▶│   FastAPI    │─────▶│  PostgreSQL │
│ (Streamlit) │      │     API      │      │             │
└─────────────┘      └──────┬───────┘      └─────────────┘
                            │  cache reads/writes,
                            │  queue push on /events
                            ▼
                      ┌──────────────┐
                      │    Redis     │◀─────────────┐
                      │ (cache+queue)│               │
                      └──────┬───────┘               │
                             │ BLPOP                 │
                             ▼                       │
                      ┌──────────────┐               │
                      │   Metrics    │───────────────┘
                      │    Worker    │  writes snapshots,
                      │(queue-driven)│  auto-promotes winners
                      └──────────────┘
```

- **API** (`api/`) — FastAPI service: registry, experiment engine, auth,
  serving endpoint, results/analysis/trend endpoints.
- **Redis** — cache for serve-time routing metadata, plus the
  `dirty_experiments` queue the metrics worker blocks on.
- **Metrics worker** (`api/app/worker.py`) — recomputes significance
  snapshots as events arrive, evaluates guardrails, and **actually
  auto-promotes winners** (flips the active version, not just flags one).
- **Dashboard** (`dashboard/`) — version history, diffing, experiment
  monitoring with trend charts, one-click promotion.
- **PostgreSQL** — single source of truth. Schema in `api/migrations/`.

## Design decisions worth knowing about

1. **Prompt versions are immutable and append-only.** Every edit creates a
   new `prompt_versions` row; diff, rollback, and audit are all queries or
   pointer-flips over that log, never destructive updates.

2. **Rollback is an O(1) metadata flip**: `prompts.active_version_id` points
   at whichever version is live. `POST /prompts/{slug}/activate` updates
   that pointer and writes an audit-log row in the same transaction.

3. **Template variables are validated at both commit time and serve time.**
   If the declared `template_variables` and the `{var}` placeholders in
   `prompt_text` ever drift apart, commits and serves both fail loudly.

4. **Traffic splitting uses consistent hashing**: `sha256(experiment_id +
   unit_id)` maps into `[0, 1)`, then walks cumulative variant weights.
   Same user always sees the same variant; assignments persist so changing
   weights mid-experiment doesn't reshuffle existing users.

5. **The serving endpoint is fully transparent, cached in Redis** as plain
   JSON (IDs/labels), never ORM objects — safe across processes and DB
   sessions.

6. **The metrics worker is queue-driven, not polling.** `POST /events`
   pushes the experiment ID onto a Redis list; the worker blocks on `BLPOP`.
   A periodic safety-net sweep still covers worker downtime and hold-period
   boundaries during quiet periods.

7. **Statistics**: two-proportion z-test for binary metrics, Mann-Whitney U
   for continuous (doesn't assume normality). **Minimum-detectable-effect is
   tracked alongside p-value**, both live (`/results`) and historically
   (`/snapshots`, written by the worker every processing round) — see
   `api/app/services/stats.py`'s `minimum_detectable_effect` and
   `minimum_detectable_effect_continuous`.

8. **Auto-promotion is real, not just flagged.** `api/app/services/auto_promote.py`
   is the piece that was missing in an earlier pass: once a winner clears
   target sample size, significance (p < 0.05), the 24-hour hold period, and
   no guardrail has fired, the system itself calls the same
   `activate_version()` path a human uses — same audit trail, actor
   `"auto-promotion-system"`. Toggle with `AUTO_PROMOTE_ENABLED=false` to
   fall back to manual-only promotion via the dashboard button.

9. **Guardrails auto-halt experiments** on error-rate spikes (>15%) or a
   variant significantly underperforming baseline (p < 0.01, negative lift).

10. **RBAC**: three roles over API keys (`viewer` < `editor` < `admin`),
    keys stored as SHA-256 hashes only. Promotion and rollback are
    admin-only. `AUTH_DISABLED` is read fresh on every request (not cached
    at import time — an earlier version cached it, which silently broke
    test isolation; see `api/app/auth.py`'s `_auth_disabled()`).

11. **The LLM-judge collector makes a real model call.** `POST
    /events/judge` with `collector: "llm_judge"` calls Claude with the
    rendered prompt, the output, and a rubric, parses back
    `{score, reasoning}`. `task_accuracy` is the second built-in collector
    (exact-match against a reference answer, no API key needed). New
    collectors register into `COLLECTOR_REGISTRY` by name.

12. **Trend lines are real**, not just a data-model placeholder:
    `GET /experiments/{id}/snapshots` returns the full history the worker
    writes, and the dashboard plots mean-value and p-value trends from it.

## Running it

```bash
cp .env.example .env   # then adjust values, or just export the ones you need
docker-compose up --build
```

This alone gives you a **fully seeded, running experiment** — the `seed`
service bootstraps an admin API key and runs the Phase 5 demo
(support-email classifier, 3 prompt strategies, 540 synthetic requests)
against the real authenticated API. `HOLD_PERIOD_HOURS` defaults to `0.02`
(~72s) in this compose file specifically so the experiment auto-promotes to
`completed` within a couple of minutes of startup, with no manual steps.
Override to `24` (or unset) for anything meant to resemble production:

```bash
HOLD_PERIOD_HOURS=24 docker-compose up --build
```

- API: http://localhost:8000 (`/docs` for interactive API docs)
- Dashboard: http://localhost:8501 — paste in the API key the `seed`
  service printed to its logs (`docker-compose logs seed`), or bootstrap
  your own (below)
- Postgres: localhost:5432, Redis: localhost:6379

### Bootstrap your own API key

```bash
cd api && pip install -r requirements.txt
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/prompt_ab \
  python ../scripts/create_api_key.py --name "your-name-admin" --role admin
```

For local convenience (skip auth entirely): `AUTH_DISABLED=true docker-compose up --build`

### LLM-judge collector

Needs a real key: `ANTHROPIC_API_KEY=sk-ant-... docker-compose up --build`.
Without it, `llm_judge` calls fail with a clear 422; `task_accuracy` needs
nothing external.

## Running tests

**Unit tests** (32 tests, no external services needed):
```bash
cd api && pip install -r requirements.txt
cd .. && python -m pytest tests/ -v --ignore=tests/test_integration.py
```

**Integration tests** (16 tests, the real proof — actual FastAPI app via
`TestClient` against live Postgres + Redis, no mocking):
```bash
# needs a reachable Postgres with migrations 001-003 applied, and Redis
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/prompt_ab_test \
REDIS_URL=redis://localhost:6379/0 \
  python -m pytest tests/test_integration.py -v
```
(Skips cleanly with a clear reason if either service isn't reachable,
rather than failing red for infra reasons.)

**Everything together** (48 tests):
```bash
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/prompt_ab_test \
REDIS_URL=redis://localhost:6379/0 \
  python -m pytest tests/ -v
```

The integration suite covers: full versioning/diff/rollback against a real
DB, traffic-split consistency via actual repeated HTTP calls (not just the
hash function in isolation), the complete experiment lifecycle from create
through auto-promotion with a real active-version flip, guardrail halting,
both metric collectors, and RBAC enforcement (401/403 against real
dependency injection). Writing these caught two real bugs that the 32 unit
tests missed:

- `winner.py` crashed formatting a `None` relative-lift value, which is
  mathematically legitimate (undefined) when a baseline's success rate is
  exactly 0% — a real, plausible scenario, not an edge case worth ignoring.
- `AUTH_DISABLED` was cached as a module-level constant at first import, so
  toggling the env var after the fact silently had no effect — a bug that
  would also bite any process using reload/multi-import, not just tests.

Both are fixed and each has its own regression test now (`test_winner.py`,
`api/app/auth.py`'s `_auth_disabled()`).

## API surface

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/keys` | admin | Issue a new API key |
| GET | `/auth/keys` | admin | List keys |
| DELETE | `/auth/keys/{id}` | admin | Revoke a key |
| GET | `/auth/whoami` | viewer | Identify the calling key |
| POST | `/prompts` | editor | Create a new logical prompt |
| POST | `/prompts/{slug}/versions` | editor | Commit a new version |
| GET | `/prompts/{slug}/versions` | viewer | List version history |
| GET | `/prompts/{slug}/active` | viewer | Get currently active version |
| GET | `/prompts/{slug}/diff?from_=&to=` | viewer | Diff two versions |
| POST | `/prompts/{slug}/activate` | admin | Activate/rollback (audit-logged) |
| GET | `/prompts/{slug}/audit-log` | viewer | Full change history |
| POST | `/prompts/{slug}/render` | viewer | Sanity-check render a version |
| POST | `/experiments` | editor | Create an experiment (draft) |
| POST | `/experiments/{id}/start` | editor | Start traffic splitting |
| POST | `/experiments/{id}/pause` | editor | Pause an experiment |
| GET | `/experiments/{id}/results` | viewer | Live stats, significance, MDE, winner status |
| GET | `/experiments/{id}/snapshots` | viewer | Trend-line history (mean, p-value, MDE over time) |
| POST | `/experiments/{id}/promote` | admin | Manual promote (fallback if auto-promote disabled) |
| POST | `/serve/{slug}` | viewer | **The one endpoint your app code calls** |
| POST | `/events` | editor | Record a metric event |
| POST | `/events/judge` | editor | Run a pluggable collector (LLM-judge / task-accuracy) |

## Code quality

```bash
pip install ruff black pre-commit
ruff check api/ scripts/ tests/       # lint
black --check api/ scripts/ tests/    # format check
pre-commit install                    # optional: run both automatically on every commit
```

Both are enforced in CI (`.github/workflows/ci.yml`) alongside the full test
suite — every push/PR runs lint, format check, and all 48 tests against real
Postgres + Redis service containers, not mocks.

## What's not implemented (honest scope notes)

- The `docker-compose.yml` orchestration itself (service health-check
  ordering, the `seed` service's container networking) has been validated
  for YAML syntax and equivalent application logic (tested against real
  Postgres/Redis installed directly, and CI's service-container Postgres/
  Redis) but not run through an actual `docker-compose up` end to end — no
  Docker daemon was available while building this. Worth a real run before
  fully trusting the one-shot `seed` container's output-parsing shell script.
- RBAC is three flat roles, not per-prompt/per-experiment ACLs.
- No key expiry/rotation policy beyond manual revocation.
- Redis is a single instance with no HA in `docker-compose.yml`.
- The LLM-judge rubric is single free-text scored 0.0–1.0, not a structured
  multi-criteria rubric.
- No rate limiting on the API.
- MDE is computed with a normal-approximation formula assuming roughly
  equal variance/sample size across arms — good for dashboard guidance, not
  a substitute for a proper power analysis before launching a real
  experiment with a strict pre-registered sample size.
