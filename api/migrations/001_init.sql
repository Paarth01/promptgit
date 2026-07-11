-- Prompt Versioning & A/B Testing Platform — initial schema
-- Design notes:
--   * prompt_versions is append-only/immutable. Never UPDATE a version row.
--   * "active" state lives on prompts.active_version_id (single pointer flip = rollback).
--   * experiments reference prompt_versions directly, so a variant's text is frozen
--     even if someone later edits the "current" version of that prompt slot.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- ─────────────────────────────────────────────────────────────────────────
-- PHASE 1: PROMPT REGISTRY
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE prompts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                TEXT UNIQUE NOT NULL,
    description         TEXT,
    active_version_id   UUID,  -- FK added after prompt_versions exists
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prompt_versions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id           UUID NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version_number      INT NOT NULL,
    prompt_text         TEXT NOT NULL,
    few_shot_examples   JSONB NOT NULL DEFAULT '[]',
    params              JSONB NOT NULL DEFAULT '{}',
    template_variables  TEXT[] NOT NULL DEFAULT '{}',
    commit_message      TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (prompt_id, version_number)
);

ALTER TABLE prompts
    ADD CONSTRAINT fk_active_version
    FOREIGN KEY (active_version_id) REFERENCES prompt_versions(id);

CREATE TABLE prompt_audit_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id           UUID NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    action              TEXT NOT NULL CHECK (action IN ('create_version','activate','rollback')),
    from_version_id     UUID REFERENCES prompt_versions(id),
    to_version_id       UUID REFERENCES prompt_versions(id),
    actor               TEXT NOT NULL,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_versions_prompt_id ON prompt_versions(prompt_id);
CREATE INDEX idx_audit_prompt_id ON prompt_audit_log(prompt_id);

-- ─────────────────────────────────────────────────────────────────────────
-- PHASE 2: EXPERIMENT ENGINE
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE experiments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id           UUID NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    primary_metric      TEXT NOT NULL,       -- e.g. 'task_success', 'latency_ms', 'quality_score'
    metric_type         TEXT NOT NULL CHECK (metric_type IN ('binary','continuous')),
    target_sample_size  INT NOT NULL,
    min_detectable_effect NUMERIC,           -- MDE as a fraction, e.g. 0.05 = 5pp
    status              TEXT NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft','running','paused','stopped_guardrail','completed')),
    winner_variant_id   UUID,                -- FK added after variants exists
    hold_until          TIMESTAMPTZ,         -- 24h data-quality hold before auto-promote is allowed
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    stopped_at          TIMESTAMPTZ
);

CREATE TABLE experiment_variants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id       UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    prompt_version_id   UUID NOT NULL REFERENCES prompt_versions(id),
    label               TEXT NOT NULL,        -- e.g. 'baseline', 'variant_b'
    traffic_weight      NUMERIC NOT NULL CHECK (traffic_weight >= 0 AND traffic_weight <= 1),
    is_baseline         BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (experiment_id, label)
);

ALTER TABLE experiments
    ADD CONSTRAINT fk_winner_variant
    FOREIGN KEY (winner_variant_id) REFERENCES experiment_variants(id);

-- Sticky assignment: same (experiment, unit_id) always resolves to same variant.
-- unit_id is whatever the caller uses for consistent-hash stickiness (user_id, session_id, etc).
CREATE TABLE experiment_assignments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id       UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    unit_id             TEXT NOT NULL,
    variant_id          UUID NOT NULL REFERENCES experiment_variants(id),
    assigned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (experiment_id, unit_id)
);

CREATE INDEX idx_assignments_lookup ON experiment_assignments(experiment_id, unit_id);

-- ─────────────────────────────────────────────────────────────────────────
-- PHASE 3: METRICS & EVENTS
-- ─────────────────────────────────────────────────────────────────────────

-- One row per served request. Built-in metrics captured directly; custom
-- metrics (LLM-judge quality, task accuracy) go in the JSONB `custom_metrics`.
CREATE TABLE experiment_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id       UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    variant_id          UUID NOT NULL REFERENCES experiment_variants(id),
    unit_id             TEXT NOT NULL,
    latency_ms          NUMERIC,
    input_tokens        INT,
    output_tokens       INT,
    cost_usd            NUMERIC,
    is_error            BOOLEAN NOT NULL DEFAULT false,
    primary_metric_value NUMERIC,             -- the value used for significance testing
    custom_metrics      JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_events_experiment_variant ON experiment_events(experiment_id, variant_id);
CREATE INDEX idx_events_created_at ON experiment_events(created_at);

-- Rolling significance-test snapshots, so the dashboard can show trend lines
-- without recomputing stats over the full event history on every page load.
CREATE TABLE experiment_analysis_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id       UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    variant_id          UUID NOT NULL REFERENCES experiment_variants(id),
    sample_size         INT NOT NULL,
    mean_value          NUMERIC,
    std_dev             NUMERIC,
    p_value             NUMERIC,              -- vs baseline
    is_significant      BOOLEAN,
    test_used           TEXT,                 -- 't_test' | 'mann_whitney_u'
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_snapshots_experiment ON experiment_analysis_snapshots(experiment_id, computed_at);
