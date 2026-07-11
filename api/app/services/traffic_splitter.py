"""Phase 2: consistent-hash traffic splitter.

Design: hash(experiment_id + unit_id) -> a float in [0, 1), then walk the
cumulative traffic-weight ranges to pick a variant. This is deterministic
(same unit_id always -> same variant for a given experiment) without needing
to store anything — the DB assignment table is a cache/audit record, not the
source of truth for the hash itself. That matters because it means the
splitter still behaves consistently even if you rebuild the assignments
table, and it composes cleanly with the persisted-assignment check below for
mid-experiment reproducibility even if traffic weights change.
"""

import hashlib
from uuid import UUID

from app.db_models import ExperimentAssignment, ExperimentVariant
from sqlalchemy.orm import Session


def _hash_to_unit_interval(experiment_id: UUID, unit_id: str) -> float:
    key = f"{experiment_id}:{unit_id}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    # Use first 13 hex chars (~52 bits) as an integer, normalize to [0, 1).
    as_int = int(digest[:13], 16)
    max_val = 16**13
    return as_int / max_val


def assign_variant(
    db: Session, experiment_id: UUID, unit_id: str, variants: list[ExperimentVariant]
) -> ExperimentVariant:
    """Return the variant this unit_id is (or should be) assigned to.
    Persists the assignment on first sight so:
      1. it survives changes to traffic_weight mid-experiment (no reshuffling
         active users when you tweak a split), and
      2. the dashboard/audit can answer "who saw what" directly from a table
         instead of recomputing hashes.
    """
    existing = (
        db.query(ExperimentAssignment)
        .filter(
            ExperimentAssignment.experiment_id == experiment_id,
            ExperimentAssignment.unit_id == unit_id,
        )
        .first()
    )
    if existing:
        return next(v for v in variants if v.id == existing.variant_id)

    r = _hash_to_unit_interval(experiment_id, unit_id)

    # Sort variants for a stable cumulative ordering (by label, deterministic).
    ordered = sorted(variants, key=lambda v: v.label)
    cumulative = 0.0
    chosen = ordered[-1]  # fallback for floating point edge case at r ~= 1.0
    for v in ordered:
        cumulative += float(v.traffic_weight)
        if r < cumulative:
            chosen = v
            break

    assignment = ExperimentAssignment(experiment_id=experiment_id, unit_id=unit_id, variant_id=chosen.id)
    db.add(assignment)
    db.commit()
    return chosen
