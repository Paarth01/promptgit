"""Async metrics worker (Phase 3) — queue-driven, not polling.

The /events endpoint pushes an experiment_id onto the Redis list
`dirty_experiments` every time a new event is recorded. This worker blocks
on BLPOP against that list, so snapshot computation happens within moments
of new data arriving instead of waiting up to a fixed poll interval — the
"trigger from /events via a queue instead of polling on a timer" upgrade.

A periodic safety-net sweep (every SWEEP_INTERVAL_SECONDS, on BLPOP timeout)
still checks all running experiments directly. This covers two edge cases
the queue alone doesn't: events that arrive while the worker is down (queue
items are only pushed, never replayed from DB state), and guardrail/winner
checks needing to happen even during quiet periods if an experiment is
sitting right at its hold_until boundary.
"""

from datetime import datetime, timezone
from uuid import UUID

import numpy as np
from app.database import SessionLocal
from app.db_models import (
    Experiment,
    ExperimentAnalysisSnapshot,
    ExperimentEvent,
    ExperimentVariant,
)
from app.redis_client import get_redis
from app.services.auto_promote import maybe_auto_promote
from app.services.stats import (
    VariantSample,
    analyze_experiment,
    check_guardrails,
    minimum_detectable_effect,
    minimum_detectable_effect_continuous,
)
from app.services.winner import determine_winner

QUEUE_KEY = "dirty_experiments"
BLPOP_TIMEOUT_SECONDS = 10  # how long to block waiting for a queue item
SWEEP_INTERVAL_SECONDS = 60  # safety-net full sweep cadence


def process_experiment(db, experiment_id: UUID) -> None:
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp or exp.status != "running":
        return

    variants = db.query(ExperimentVariant).filter(ExperimentVariant.experiment_id == exp.id).all()

    samples = []
    for v in variants:
        events = db.query(ExperimentEvent).filter(ExperimentEvent.variant_id == v.id).all()
        values = [float(e.primary_metric_value) for e in events if e.primary_metric_value is not None]
        successes = sum(1 for e in events if e.primary_metric_value == 1)
        samples.append(
            VariantSample(
                variant_id=str(v.id),
                label=v.label,
                is_baseline=v.is_baseline,
                values=values,
                successes=successes,
                n=len(events),
                error_count=sum(1 for e in events if e.is_error),
                total_events=len(events),
            )
        )

    if not any(s.total_events for s in samples):
        return

    results = analyze_experiment(samples, exp.metric_type)

    baseline_sample = next((s for s in samples if s.is_baseline), None)
    mde = None
    if baseline_sample is not None:
        if exp.metric_type == "binary" and baseline_sample.n:
            mde = minimum_detectable_effect(baseline_sample.successes / baseline_sample.n, baseline_sample.n)
        elif exp.metric_type == "continuous" and len(baseline_sample.values) > 1:
            baseline_std = float(np.std(baseline_sample.values, ddof=1))
            mde = minimum_detectable_effect_continuous(baseline_std, len(baseline_sample.values))
        if mde is not None and (mde != mde):  # NaN check without importing math
            mde = None

    for r in results:
        db.add(
            ExperimentAnalysisSnapshot(
                experiment_id=exp.id,
                variant_id=UUID(r.variant_id),
                sample_size=r.sample_size,
                mean_value=r.mean_value,
                std_dev=r.std_dev,
                p_value=r.p_value_vs_baseline,
                is_significant=r.is_significant,
                test_used=r.test_used,
                min_detectable_effect=mde,
            )
        )

    halt, reason = check_guardrails(results)
    if halt:
        exp.status = "stopped_guardrail"
        exp.stopped_at = datetime.now(timezone.utc)
        print(f"[worker] halted experiment {exp.id} ({exp.name}): {reason}")
        db.commit()
    else:
        winner_id, ready, reason = determine_winner(results, exp.target_sample_size, exp.hold_until, False)
        if winner_id and not exp.winner_variant_id:
            exp.winner_variant_id = UUID(winner_id)
            print(f"[worker] winner determined for {exp.id} ({exp.name}): {reason}")
        db.commit()

        promoted = maybe_auto_promote(db, exp, exp.winner_variant_id, ready, reason)
        if promoted:
            print(f"[worker] auto-promoted winner for {exp.id} ({exp.name})")


def sweep_all_running() -> None:
    db = SessionLocal()
    try:
        running = db.query(Experiment).filter(Experiment.status == "running").all()
        for exp in running:
            process_experiment(db, exp.id)
    finally:
        db.close()


def main():
    r = get_redis()
    print(f"[worker] starting, listening on '{QUEUE_KEY}' (BLPOP timeout={BLPOP_TIMEOUT_SECONDS}s)")
    last_sweep = 0.0
    import time

    while True:
        item = r.blpop(QUEUE_KEY, timeout=BLPOP_TIMEOUT_SECONDS)
        try:
            if item is not None:
                _, experiment_id_str = item
                db = SessionLocal()
                try:
                    process_experiment(db, UUID(experiment_id_str))
                finally:
                    db.close()
            else:
                # BLPOP timed out — no new events. Use the idle moment for
                # the periodic safety-net sweep if it's due.
                now = time.time()
                if now - last_sweep >= SWEEP_INTERVAL_SECONDS:
                    sweep_all_running()
                    last_sweep = now
        except Exception as e:
            print(f"[worker] error processing item {item}: {e}")


if __name__ == "__main__":
    main()
