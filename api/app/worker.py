"""Async metrics worker (Phase 3).

Runs as a separate container. Every SNAPSHOT_INTERVAL_SECONDS, for every
running experiment, computes current significance stats and writes a
snapshot row — this is what powers the dashboard's trend lines without
forcing every dashboard page load to recompute stats over the full event
history. Also evaluates guardrails and flips experiment status if triggered.

This is intentionally a simple polling loop rather than a task queue
(Celery/RQ) — for the demo's scale (a handful of experiments, synthetic
traffic) polling is more than sufficient, and it keeps the docker-compose
footprint small. The natural next step at real scale would be to trigger
snapshot computation from the /events ingestion endpoint via a queue instead
of polling on a timer.
"""
import time
from datetime import datetime, timezone
from uuid import UUID

from app.database import SessionLocal
from app.db_models import (
    Experiment, ExperimentAnalysisSnapshot, ExperimentEvent, ExperimentVariant,
)
from app.services.stats import VariantSample, analyze_experiment, check_guardrails
from app.services.winner import determine_winner

SNAPSHOT_INTERVAL_SECONDS = 15


def run_once():
    db = SessionLocal()
    try:
        running = db.query(Experiment).filter(Experiment.status == "running").all()
        for exp in running:
            variants = db.query(ExperimentVariant).filter(
                ExperimentVariant.experiment_id == exp.id
            ).all()

            samples = []
            for v in variants:
                events = db.query(ExperimentEvent).filter(
                    ExperimentEvent.variant_id == v.id
                ).all()
                values = [
                    float(e.primary_metric_value) for e in events
                    if e.primary_metric_value is not None
                ]
                successes = sum(1 for e in events if e.primary_metric_value == 1)
                samples.append(VariantSample(
                    variant_id=str(v.id),
                    label=v.label,
                    is_baseline=v.is_baseline,
                    values=values,
                    successes=successes,
                    n=len(events),
                    error_count=sum(1 for e in events if e.is_error),
                    total_events=len(events),
                ))

            if not any(s.total_events for s in samples):
                continue  # no data yet, nothing to snapshot

            results = analyze_experiment(samples, exp.metric_type)

            for r in results:
                db.add(ExperimentAnalysisSnapshot(
                    experiment_id=exp.id,
                    variant_id=UUID(r.variant_id),
                    sample_size=r.sample_size,
                    mean_value=r.mean_value,
                    std_dev=r.std_dev,
                    p_value=r.p_value_vs_baseline,
                    is_significant=r.is_significant,
                    test_used=r.test_used,
                ))

            halt, reason = check_guardrails(results)
            if halt:
                exp.status = "stopped_guardrail"
                exp.stopped_at = datetime.now(timezone.utc)
                print(f"[worker] halted experiment {exp.id} ({exp.name}): {reason}")
            else:
                winner_id, ready, reason = determine_winner(
                    results, exp.target_sample_size, exp.hold_until, False
                )
                if ready and winner_id and not exp.winner_variant_id:
                    exp.winner_variant_id = UUID(winner_id)
                    print(f"[worker] winner determined for {exp.id} ({exp.name}): {reason}")

            db.commit()
    finally:
        db.close()


def main():
    print(f"[worker] starting, polling every {SNAPSHOT_INTERVAL_SECONDS}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[worker] error during run: {e}")
        time.sleep(SNAPSHOT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
