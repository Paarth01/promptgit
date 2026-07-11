from datetime import datetime, timezone
from uuid import UUID

import numpy as np
from app import schemas
from app.auth import require_admin, require_editor, require_viewer
from app.database import get_db
from app.db_models import (
    ApiKey,
    Experiment,
    ExperimentAnalysisSnapshot,
    ExperimentEvent,
    ExperimentVariant,
    Prompt,
    PromptVersion,
)
from app.redis_client import get_redis
from app.services import versioning
from app.services.auto_promote import maybe_auto_promote
from app.services.collectors import get_collector
from app.services.serving import invalidate_cache, resolve_and_render
from app.services.stats import (
    VariantSample,
    analyze_experiment,
    check_guardrails,
    minimum_detectable_effect,
    minimum_detectable_effect_continuous,
)
from app.services.winner import compute_hold_until, determine_winner
from app.worker import QUEUE_KEY
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

router = APIRouter(tags=["experiments"])


@router.post("/experiments", response_model=schemas.ExperimentOut, status_code=201)
def create_experiment(
    body: schemas.ExperimentCreate,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_editor),
):
    try:
        prompt = versioning.get_prompt_by_slug(db, body.prompt_slug)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))

    for v in body.variants:
        version = db.query(PromptVersion).filter(PromptVersion.id == v.prompt_version_id).first()
        if not version or version.prompt_id != prompt.id:
            raise HTTPException(
                422, f"version {v.prompt_version_id} doesn't belong to prompt '{body.prompt_slug}'"
            )

    experiment = Experiment(
        prompt_id=prompt.id,
        name=body.name,
        primary_metric=body.primary_metric,
        metric_type=body.metric_type,
        target_sample_size=body.target_sample_size,
        min_detectable_effect=body.min_detectable_effect,
        status="draft",
        created_by=body.created_by,
    )
    db.add(experiment)
    db.flush()

    for v in body.variants:
        db.add(
            ExperimentVariant(
                experiment_id=experiment.id,
                prompt_version_id=v.prompt_version_id,
                label=v.label,
                traffic_weight=v.traffic_weight,
                is_baseline=v.is_baseline,
            )
        )
    db.commit()
    db.refresh(experiment)
    return experiment


@router.get("/experiments", response_model=list[schemas.ExperimentOut])
def list_experiments(db: Session = Depends(get_db), caller: ApiKey = Depends(require_viewer)):
    return db.query(Experiment).order_by(Experiment.created_at.desc()).all()


@router.get("/experiments/{experiment_id}", response_model=schemas.ExperimentOut)
def get_experiment(
    experiment_id: UUID, db: Session = Depends(get_db), caller: ApiKey = Depends(require_viewer)
):
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        raise HTTPException(404, "experiment not found")
    return exp


@router.post("/experiments/{experiment_id}/start", response_model=schemas.ExperimentOut)
def start_experiment(
    experiment_id: UUID, db: Session = Depends(get_db), caller: ApiKey = Depends(require_editor)
):
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        raise HTTPException(404, "experiment not found")
    if exp.status not in ("draft", "paused"):
        raise HTTPException(400, f"cannot start experiment in status '{exp.status}'")

    now = datetime.now(timezone.utc)
    exp.status = "running"
    exp.started_at = exp.started_at or now
    exp.hold_until = compute_hold_until(exp.started_at)
    db.commit()
    db.refresh(exp)

    prompt = db.query(Prompt).filter(Prompt.id == exp.prompt_id).first()
    invalidate_cache(prompt.slug)
    return exp


@router.post("/experiments/{experiment_id}/pause", response_model=schemas.ExperimentOut)
def pause_experiment(
    experiment_id: UUID, db: Session = Depends(get_db), caller: ApiKey = Depends(require_editor)
):
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        raise HTTPException(404, "experiment not found")
    exp.status = "paused"
    db.commit()
    db.refresh(exp)
    prompt = db.query(Prompt).filter(Prompt.id == exp.prompt_id).first()
    invalidate_cache(prompt.slug)
    return exp


@router.post("/experiments/{experiment_id}/promote", response_model=schemas.PromptOut)
def promote_winner(
    experiment_id: UUID,
    actor: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_admin),
):
    """Manual one-click promotion (Phase 4 dashboard button). Activates the
    winner's prompt_version as the prompt's active_version and marks the
    experiment completed. Admin-only: promoting a variant to production
    traffic is the highest-stakes action in the system."""
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        raise HTTPException(404, "experiment not found")
    if exp.status == "completed":
        raise HTTPException(400, "experiment already completed (likely auto-promoted)")
    if not exp.winner_variant_id:
        raise HTTPException(400, "no winner has been determined for this experiment yet")

    winner_variant = db.query(ExperimentVariant).filter(ExperimentVariant.id == exp.winner_variant_id).one()
    prompt = db.query(Prompt).filter(Prompt.id == exp.prompt_id).one()

    updated = versioning.activate_version(
        db,
        prompt.slug,
        winner_variant.prompt_version_id,
        actor,
        reason=f"auto-promoted winner of experiment '{exp.name}' ({exp.id})",
    )
    exp.status = "completed"
    exp.stopped_at = datetime.now(timezone.utc)
    db.commit()
    invalidate_cache(prompt.slug)
    return updated


@router.post("/serve/{slug}", response_model=schemas.ServeResponse)
def serve(
    slug: str,
    body: schemas.ServeRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_viewer),
):
    """The single endpoint application code calls. Transparently resolves to
    either the prompt's active version or a running experiment's variant.
    Viewer-level access is enough — serving a prompt isn't a mutating action."""
    try:
        result = resolve_and_render(db, slug, body.unit_id, body.context)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except (versioning.SchemaDriftError, versioning.MissingVariablesError) as e:
        raise HTTPException(422, str(e))
    return result


def _enqueue_snapshot(experiment_id: UUID) -> None:
    """Push a notification onto the Redis queue the metrics worker blocks
    on, so significance snapshots get recomputed within moments of new data
    landing instead of waiting for the worker's next poll."""
    try:
        get_redis().rpush(QUEUE_KEY, str(experiment_id))
    except Exception as e:
        # Never let a Redis hiccup fail event ingestion — the worker's
        # periodic safety-net sweep will pick this experiment up regardless.
        print(f"[api] failed to enqueue snapshot for {experiment_id}: {e}")


@router.post("/events", status_code=201)
def record_event(
    body: schemas.EventCreate,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_editor),
):
    """Metric collector ingestion point. Pushes to the metrics worker's
    queue after committing so significance snapshots stay near-real-time."""
    variant = db.query(ExperimentVariant).filter(ExperimentVariant.id == body.variant_id).first()
    if not variant:
        raise HTTPException(404, "variant not found")

    db.add(
        ExperimentEvent(
            experiment_id=variant.experiment_id,
            variant_id=body.variant_id,
            unit_id=body.unit_id,
            latency_ms=body.latency_ms,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cost_usd=body.cost_usd,
            is_error=body.is_error,
            primary_metric_value=body.primary_metric_value,
            custom_metrics=body.custom_metrics,
        )
    )
    db.commit()
    _enqueue_snapshot(variant.experiment_id)
    return {"status": "recorded"}


@router.post("/events/judge", response_model=schemas.JudgeEventResponse, status_code=201)
def record_judge_event(
    body: schemas.JudgeEventRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_editor),
):
    """Runs a pluggable custom metric collector (LLM-judge or task-accuracy)
    against a model output, then records the resulting score as an
    ExperimentEvent's primary_metric_value — same downstream path as a
    caller-supplied metric, just scored by the collector instead of by the
    caller. This is the real, wired-up version of the 'pluggable metric
    collectors' phase: `llm_judge` makes an actual Anthropic API call."""
    variant = db.query(ExperimentVariant).filter(ExperimentVariant.id == body.variant_id).first()
    if not variant:
        raise HTTPException(404, "variant not found")

    collector = get_collector(body.collector)
    try:
        result = collector.collect(
            model_output=body.model_output,
            rendered_prompt=body.rendered_prompt,
            reference_answer=body.reference_answer,
            rubric=body.rubric,
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(422, str(e))

    db.add(
        ExperimentEvent(
            experiment_id=variant.experiment_id,
            variant_id=body.variant_id,
            unit_id=body.unit_id,
            latency_ms=body.latency_ms,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cost_usd=body.cost_usd,
            is_error=False,
            primary_metric_value=result.score,
            custom_metrics={"collector": body.collector, "reasoning": result.reasoning},
        )
    )
    db.commit()
    _enqueue_snapshot(variant.experiment_id)

    return schemas.JudgeEventResponse(
        primary_metric_value=result.score,
        reasoning=result.reasoning,
        collector_used=body.collector,
    )


@router.get("/experiments/{experiment_id}/results", response_model=schemas.ExperimentResults)
def get_results(experiment_id: UUID, db: Session = Depends(get_db), caller: ApiKey = Depends(require_viewer)):
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        raise HTTPException(404, "experiment not found")

    variants = db.query(ExperimentVariant).filter(ExperimentVariant.experiment_id == exp.id).all()
    samples = []
    total_events = 0
    for v in variants:
        events = db.query(ExperimentEvent).filter(ExperimentEvent.variant_id == v.id).all()
        total_events += len(events)
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

    results = analyze_experiment(samples, exp.metric_type)
    halt, halt_reason = check_guardrails(results)

    if halt and exp.status == "running":
        exp.status = "stopped_guardrail"
        exp.stopped_at = datetime.now(timezone.utc)
        db.commit()

    winner_id, ready, reason = determine_winner(results, exp.target_sample_size, exp.hold_until, halt)
    if winner_id and not exp.winner_variant_id:
        exp.winner_variant_id = UUID(winner_id)
        db.commit()

    if not halt:
        maybe_auto_promote(db, exp, exp.winner_variant_id, ready, reason)
        db.refresh(exp)

    # Same MDE calculation the worker stores in snapshots, surfaced live here
    # too so /results is never stale relative to the trend-line data.
    baseline_sample = next((s for s in samples if s.is_baseline), None)
    mde = None
    if baseline_sample is not None:
        if exp.metric_type == "binary" and baseline_sample.n:
            mde = minimum_detectable_effect(baseline_sample.successes / baseline_sample.n, baseline_sample.n)
        elif exp.metric_type == "continuous" and len(baseline_sample.values) > 1:
            baseline_std = float(np.std(baseline_sample.values, ddof=1))
            mde = minimum_detectable_effect_continuous(baseline_std, len(baseline_sample.values))
        if mde is not None and mde != mde:  # NaN
            mde = None

    progress = min(
        100.0,
        (
            (min((r.sample_size for r in results), default=0) / exp.target_sample_size) * 100
            if exp.target_sample_size
            else 0.0
        ),
    )

    return schemas.ExperimentResults(
        experiment_id=exp.id,
        status=exp.status,
        target_sample_size=exp.target_sample_size,
        total_samples=total_events,
        progress_pct=round(progress, 1),
        variants=[
            schemas.VariantStats(
                variant_id=UUID(r.variant_id),
                label=r.label,
                is_baseline=r.is_baseline,
                sample_size=r.sample_size,
                mean_value=r.mean_value,
                std_dev=r.std_dev,
                error_rate=r.error_rate,
                p_value_vs_baseline=r.p_value_vs_baseline,
                is_significant=r.is_significant,
                test_used=r.test_used,
                relative_lift_vs_baseline=r.relative_lift_vs_baseline,
            )
            for r in results
        ],
        winner_variant_id=exp.winner_variant_id,
        winner_ready=ready,
        winner_reason=(halt_reason if halt else reason),
        mde_at_current_sample_size=mde,
    )


@router.get("/experiments/{experiment_id}/snapshots", response_model=list[schemas.SnapshotOut])
def get_snapshots(
    experiment_id: UUID, db: Session = Depends(get_db), caller: ApiKey = Depends(require_viewer)
):
    """Trend-line data: the full history of significance snapshots the
    worker has written for this experiment, one row per variant per
    processing round. This is what the dashboard's trend chart reads from —
    the snapshot table existed before but nothing was reading it back."""
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        raise HTTPException(404, "experiment not found")

    variants = {
        v.id: v.label
        for v in db.query(ExperimentVariant).filter(ExperimentVariant.experiment_id == exp.id).all()
    }
    snapshots = (
        db.query(ExperimentAnalysisSnapshot)
        .filter(ExperimentAnalysisSnapshot.experiment_id == exp.id)
        .order_by(ExperimentAnalysisSnapshot.computed_at.asc())
        .all()
    )
    return [
        schemas.SnapshotOut(
            variant_id=s.variant_id,
            variant_label=variants.get(s.variant_id, "unknown"),
            sample_size=s.sample_size,
            mean_value=s.mean_value,
            p_value=s.p_value,
            is_significant=s.is_significant,
            min_detectable_effect=s.min_detectable_effect,
            computed_at=s.computed_at,
        )
        for s in snapshots
    ]
