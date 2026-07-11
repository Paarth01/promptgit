"""Phase 3: actual auto-promotion — the part that was missing before.

determine_winner() (in winner.py) only decides whether a winner is ready;
it was never wired to *act* on that decision. This module is the missing
link: given a ready winner, it activates that version and marks the
experiment completed, using the same versioning.activate_version() path a
human clicks through in the dashboard — so auto-promotion and manual
promotion produce identical audit-log entries, just with a different actor.

Called from two places:
  - the metrics worker, right after it computes fresh results (near-real-time)
  - GET /experiments/{id}/results, so promotion isn't only as fresh as the
    worker's last queue pop — anyone checking results triggers the same check
"""

import os
from datetime import datetime, timezone
from uuid import UUID

from app.db_models import Experiment, ExperimentVariant, Prompt
from app.services import versioning
from app.services.serving import invalidate_cache
from sqlalchemy.orm import Session

AUTO_PROMOTE_ENABLED = os.environ.get("AUTO_PROMOTE_ENABLED", "true").lower() == "true"

AUTO_PROMOTE_ACTOR = "auto-promotion-system"


def maybe_auto_promote(
    db: Session, exp: Experiment, winner_variant_id: UUID | None, ready: bool, reason: str
) -> bool:
    """If a winner is ready and auto-promotion is enabled, actually promote
    it: activate the winning version and mark the experiment completed.
    Returns True if promotion happened this call, False otherwise.

    Idempotent/safe to call repeatedly: only acts when exp.status == 'running',
    so a second call after promotion (status now 'completed') is a no-op.
    """
    if not AUTO_PROMOTE_ENABLED or not ready or winner_variant_id is None:
        return False
    if exp.status != "running":
        return False

    winner_variant = db.query(ExperimentVariant).filter(ExperimentVariant.id == winner_variant_id).one()
    prompt = db.query(Prompt).filter(Prompt.id == exp.prompt_id).one()

    versioning.activate_version(
        db,
        prompt.slug,
        winner_variant.prompt_version_id,
        actor=AUTO_PROMOTE_ACTOR,
        reason=f"auto-promoted winner of experiment '{exp.name}' ({exp.id}): {reason}",
    )
    exp.status = "completed"
    exp.stopped_at = datetime.now(timezone.utc)
    db.commit()
    invalidate_cache(prompt.slug)
    return True
