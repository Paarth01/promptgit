# Prompt Versioning & A/B Testing Platform

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
│  Dashboard  │────▶│   FastAPI     │────▶│  PostgreSQL │
│ (Streamlit) │      │     API      │      │             │
└─────────────┘      └──────────────┘      └─────────────┘
                             ▲                     ▲
                             │                     │
                      ┌──────────────┐             │
                      │   Metrics    │─────────────┘
                      │    Worker    │
                      │  (async loop)│
                      └──────────────┘
```

- **API** (`api/`) — FastAPI service exposing the registry, experiment
  engine, serving endpoint, and results/analysis endpoints.
- **Metrics worker** (`api/app/worker.py`) — polls running experiments,
  computes significance snapshots for dashboard trend lines, evaluates
  guardrails, and flags winners as they emerge.
- **Dashboard** (`dashboard/`) — Streamlit app for browsing version history,
  diffing versions, creating/monitoring experiments, and one-click promotion.
- **PostgreSQL** — single source of truth. Schema in `api/migrations/001_init.sql`.

## Design decisions worth knowing about

1. **Prompt versions are immutable and append-only.** A "prompt" (`prompts`
   table) is a stable logical identity; every edit creates a new row in
   `prompt_versions`. This makes diff, rollback, and audit trivial — they're
   just queries or pointer-flips over a log, never destructive updates.

2. **Rollback is an O(1) metadata flip**, not a redeploy: `prompts.active_version_id`
   points at whichever version is live. `POST /prompts/{slug}/activate` updates
   that pointer and writes an audit-log row in the same transaction.

3. **Template variables are validated at both commit time and serve time.**
   `template_variables` is declared on the version, separate from the raw
   `{var}` placeholders in `prompt_text`. If they ever drift apart — someone
   edits the text but forgets to update the declared variables — commits and
   serves both fail loudly instead of silently sending a malformed prompt.

4. **Traffic splitting uses consistent hashing**, not random assignment per
   request: `sha256(experiment_id + unit_id)` maps deterministically into
   `[0, 1)`, then walks cumulative variant weights. Same user always sees the
   same variant. Assignments are also persisted, so changing traffic weights
   mid-experiment doesn't reshuffle users who are already in it.

5. **The serving endpoint (`POST /serve/{slug}`) is fully transparent.**
   Callers never know or care whether they got the prompt's plain active
   version or got routed into a running experiment — that resolution happens
   entirely server-side, with a short-TTL cache so the hot path doesn't take
   a DB round-trip per inference call.

6. **Statistics**: binary metrics (task success/conversion) use a
   two-proportion z-test; continuous metrics default to Mann-Whitney U
   (non-parametric — doesn't assume LLM-judge scores or latency are normally
   distributed). See `api/app/services/stats.py`.

7. **Auto-promotion requires four things simultaneously**: target sample
   size reached, statistical significance (p < 0.05) against baseline, a
   24-hour hold period cleared (protects against day-of-week/time-of-day
   confounds in early data — configurable via `HOLD_PERIOD_HOURS` for demo
   purposes), and no guardrail having fired. See `api/app/services/winner.py`.

8. **Guardrails auto-halt experiments** on error-rate spikes (>15%) or a
   variant significantly underperforming baseline (p < 0.01 and negative
   lift) — checked both synchronously on every `/results` call and by the
   background worker.

## Running it

```bash
docker-compose up --build
```

- API: http://localhost:8000 (interactive docs at `/docs`)
- Dashboard: http://localhost:8501
- Postgres: localhost:5432 (user/pass: postgres/postgres, db: prompt_ab)

The schema in `api/migrations/001_init.sql` is auto-applied on first Postgres
boot via the `docker-entrypoint-initdb.d` mount.

### Load the demo dataset

The Phase 5 demo is a support-email classifier with three genuinely
different prompting strategies (zero-shot baseline, few-shot, chain-of-thought),
run against 540 synthetic requests:

```bash
pip install requests
python scripts/seed_demo.py --api http://localhost:8000
```

To see the full auto-promote flow without waiting 24 real hours, set a short
hold period before starting the stack:

```bash
HOLD_PERIOD_HOURS=0.01 docker-compose up --build
```

The synthetic ground-truth success rates (few-shot 89% > CoT 83% > baseline
74%) are picked to be realistic and to produce a clear, statistically
significant winner — few-shot examples outperforming both a bare instruction
and a heavier chain-of-thought approach is a genuinely common real-world result.

## Running tests

```bash
cd api && pip install -r requirements.txt
cd .. && python -m pytest tests/ -v
```

15 tests cover: schema-drift/missing-variable validation in the render path,
consistent-hash determinism and distribution uniformity, and statistical
correctness (significance detection, guardrail triggers, z-test symmetry).

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/prompts` | Create a new logical prompt |
| POST | `/prompts/{slug}/versions` | Commit a new version |
| GET | `/prompts/{slug}/versions` | List version history |
| GET | `/prompts/{slug}/active` | Get currently active version |
| GET | `/prompts/{slug}/diff?from_=&to=` | Diff two versions |
| POST | `/prompts/{slug}/activate` | Activate/rollback (audit-logged) |
| GET | `/prompts/{slug}/audit-log` | Full change history |
| POST | `/prompts/{slug}/render` | Sanity-check render a version |
| POST | `/experiments` | Create an experiment (draft) |
| POST | `/experiments/{id}/start` | Start traffic splitting |
| POST | `/experiments/{id}/pause` | Pause an experiment |
| GET | `/experiments/{id}/results` | Live stats, significance, winner status |
| POST | `/experiments/{id}/promote` | One-click promote winner |
| POST | `/serve/{slug}` | **The one endpoint your app code calls** |
| POST | `/events` | Record a metric event for a served variant |

## What's not implemented (honest scope notes)

- The metrics worker is a polling loop, not a task queue — appropriate for
  the demo's scale, documented as the natural next step at real scale
  (trigger snapshot computation from `/events` via a queue instead of a timer).
- The in-process TTL cache for serving resolution is per-process; a
  multi-instance deployment would swap this for Redis.
- No auth/RBAC on the API — everything is `actor: str` passed by the caller,
  fine for a portfolio demo, not for production multi-tenant use.
- Custom LLM-judge metric collection is supported by schema
  (`custom_metrics` JSONB) but no actual LLM-judge caller is wired up — the
  seed script simulates outcomes directly rather than calling a real model.
