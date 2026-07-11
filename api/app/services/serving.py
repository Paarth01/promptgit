"""Phase 2: the serving endpoint. Callers hit /serve/{slug} and never know
whether they got the plain active version or got routed into a running
experiment — that's the whole point of transparent resolution.

Resolution order:
  1. Is there a running experiment for this prompt? -> assign variant via
     consistent-hash traffic splitter, return that variant's version.
  2. Otherwise -> return the prompt's active_version_id.

A short-TTL in-process cache avoids a DB round trip on every single inference
call (this is the hot path). Cache is invalidated on activate/rollback and
on experiment start/stop. In a multi-process deployment this would move to
Redis; documented here as the natural next step.
"""
import time
from uuid import UUID

from sqlalchemy.orm import Session

from app.db_models import Experiment, ExperimentVariant, Prompt, PromptVersion
from app.services.traffic_splitter import assign_variant
from app.services.versioning import get_prompt_by_slug, render

_CACHE_TTL_SECONDS = 5
_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _cache_set(key: str, value: dict):
    _cache[key] = (time.time(), value)


def invalidate_cache(slug: str | None = None):
    if slug is None:
        _cache.clear()
    else:
        _cache.pop(f"resolve:{slug}", None)


def _resolve_routing(db: Session, slug: str) -> dict:
    """Figure out whether this prompt has a running experiment, and if so,
    fetch its variants. Cached because this is metadata, not per-request data."""
    cached = _cache_get(f"resolve:{slug}")
    if cached is not None:
        return {
            "prompt": db.merge(cached["prompt"]),
            "experiment": db.merge(cached["experiment"]) if cached["experiment"] else None,
            "variants": [db.merge(v) for v in cached["variants"]],
        }

    prompt = get_prompt_by_slug(db, slug)
    experiment = (
        db.query(Experiment)
        .filter(Experiment.prompt_id == prompt.id, Experiment.status == "running")
        .first()
    )

    result = {"prompt": prompt, "experiment": None, "variants": []}
    if experiment:
        variants = (
            db.query(ExperimentVariant)
            .filter(ExperimentVariant.experiment_id == experiment.id)
            .all()
        )
        result["experiment"] = experiment
        result["variants"] = variants

    _cache_set(f"resolve:{slug}", result)
    return result


def resolve_and_render(db: Session, slug: str, unit_id: str, context: dict) -> dict:
    routing = _resolve_routing(db, slug)
    prompt: Prompt = routing["prompt"]
    experiment: Experiment | None = routing["experiment"]

    if experiment is not None:
        variant = assign_variant(db, experiment.id, unit_id, routing["variants"])
        version = db.query(PromptVersion).filter(PromptVersion.id == variant.prompt_version_id).one()
        rendered = render(version, context)
        return {
            "resolved_prompt_text": rendered,
            "prompt_version_id": version.id,
            "version_number": version.version_number,
            "params": version.params,
            "experiment_id": experiment.id,
            "variant_id": variant.id,
            "variant_label": variant.label,
        }

    if prompt.active_version_id is None:
        raise ValueError(f"prompt '{slug}' has no active version")
    version = db.query(PromptVersion).filter(PromptVersion.id == prompt.active_version_id).one()
    rendered = render(version, context)
    return {
        "resolved_prompt_text": rendered,
        "prompt_version_id": version.id,
        "version_number": version.version_number,
        "params": version.params,
        "experiment_id": None,
        "variant_id": None,
        "variant_label": None,
    }
