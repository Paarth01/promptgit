"""Phase 2: the serving endpoint. Callers hit /serve/{slug} and never know
whether they got the plain active version or got routed into a running
experiment — that's the whole point of transparent resolution.

Resolution order:
  1. Is there a running experiment for this prompt? -> assign variant via
     consistent-hash traffic splitter, return that variant's version.
  2. Otherwise -> return the prompt's active_version_id.

Caching: routing metadata (which experiment/variants are active for a slug)
is cached in Redis with a short TTL, keyed by slug. This is deliberately a
cache of plain IDs/labels — never ORM objects — so it's safe to share across
processes and doesn't hit SQLAlchemy's "DetachedInstanceError" trap you get
from caching objects bound to a request-scoped session. On a cache hit we
still do one cheap query to fetch the actual PromptVersion row by ID; what
we save is the "does this prompt have a running experiment" lookup, which is
the part that would otherwise run on every single inference call.
"""

import json
from uuid import UUID

from app.db_models import Experiment, ExperimentVariant, PromptVersion
from app.redis_client import get_redis
from app.services.traffic_splitter import assign_variant
from app.services.versioning import get_prompt_by_slug, render
from sqlalchemy.orm import Session

_CACHE_TTL_SECONDS = 5


def _cache_key(slug: str) -> str:
    return f"serve_routing:{slug}"


def invalidate_cache(slug: str | None = None):
    r = get_redis()
    if slug is None:
        for key in r.scan_iter("serve_routing:*"):
            r.delete(key)
    else:
        r.delete(_cache_key(slug))


def _resolve_routing(db: Session, slug: str) -> dict:
    """Figure out whether this prompt has a running experiment, and if so,
    which variants it has. Returns plain JSON-serializable data — cacheable
    in Redis, safe across processes and across DB sessions."""
    r = get_redis()
    cached = r.get(_cache_key(slug))
    if cached is not None:
        return json.loads(cached)

    prompt = get_prompt_by_slug(db, slug)
    experiment = (
        db.query(Experiment).filter(Experiment.prompt_id == prompt.id, Experiment.status == "running").first()
    )

    result = {
        "prompt_id": str(prompt.id),
        "active_version_id": str(prompt.active_version_id) if prompt.active_version_id else None,
        "experiment_id": None,
        "variants": [],
    }
    if experiment:
        variants = db.query(ExperimentVariant).filter(ExperimentVariant.experiment_id == experiment.id).all()
        result["experiment_id"] = str(experiment.id)
        result["variants"] = [
            {
                "id": str(v.id),
                "label": v.label,
                "traffic_weight": float(v.traffic_weight),
                "prompt_version_id": str(v.prompt_version_id),
            }
            for v in variants
        ]

    r.set(_cache_key(slug), json.dumps(result), ex=_CACHE_TTL_SECONDS)
    return result


class _CachedVariant:
    """Lightweight stand-in for ExperimentVariant, built from cached JSON —
    just enough attributes for the traffic splitter to work with."""

    def __init__(self, d: dict):
        self.id = UUID(d["id"])
        self.label = d["label"]
        self.traffic_weight = d["traffic_weight"]
        self.prompt_version_id = UUID(d["prompt_version_id"])


def resolve_and_render(db: Session, slug: str, unit_id: str, context: dict) -> dict:
    routing = _resolve_routing(db, slug)

    if routing["experiment_id"] is not None:
        variants = [_CachedVariant(v) for v in routing["variants"]]
        variant = assign_variant(db, UUID(routing["experiment_id"]), unit_id, variants)
        version = db.query(PromptVersion).filter(PromptVersion.id == variant.prompt_version_id).one()
        rendered = render(version, context)
        return {
            "resolved_prompt_text": rendered,
            "prompt_version_id": version.id,
            "version_number": version.version_number,
            "params": version.params,
            "experiment_id": UUID(routing["experiment_id"]),
            "variant_id": variant.id,
            "variant_label": variant.label,
        }

    if routing["active_version_id"] is None:
        raise ValueError(f"prompt '{slug}' has no active version")
    version = db.query(PromptVersion).filter(PromptVersion.id == UUID(routing["active_version_id"])).one()
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
