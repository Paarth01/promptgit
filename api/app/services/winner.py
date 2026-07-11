"""Phase 3: winner declaration and auto-promotion.

Auto-promote requires ALL of:
  1. target_sample_size reached for every variant
  2. the leading variant is statistically significant vs baseline (p < 0.05)
  3. experiment has been running past its 24h hold_until timestamp
     (protects against day-of-week / time-of-day confounds in early data)
  4. no guardrail has fired
"""
import os
from datetime import datetime, timedelta, timezone

from app.services.stats import VariantResult

# Configurable so the Phase 5 demo can run the full auto-promote flow in
# seconds instead of waiting 24 real hours. Production deployments should
# leave HOLD_PERIOD_HOURS unset (defaults to the spec's 24h).
HOLD_PERIOD = timedelta(hours=float(os.environ.get("HOLD_PERIOD_HOURS", 24)))


def compute_hold_until(started_at: datetime) -> datetime:
    return started_at + HOLD_PERIOD


def determine_winner(
    results: list[VariantResult],
    target_sample_size: int,
    hold_until: datetime | None,
    guardrail_triggered: bool,
) -> tuple[str | None, bool, str]:
    """Returns (winner_variant_id_or_None, ready_to_promote, reason)."""
    if guardrail_triggered:
        return None, False, "guardrail triggered; experiment halted, no auto-promotion"

    under_sample = [r for r in results if r.sample_size < target_sample_size]
    if under_sample:
        labels = ", ".join(r.label for r in under_sample)
        return None, False, f"waiting on sample size for: {labels}"

    if hold_until is not None and datetime.now(timezone.utc) < hold_until:
        remaining = hold_until - datetime.now(timezone.utc)
        return None, False, f"in 24h data-quality hold, {remaining} remaining"

    significant = [r for r in results if not r.is_baseline and r.is_significant]
    if not significant:
        return None, False, "no variant reached statistical significance vs baseline"

    # Winner = statistically significant variant with the best (highest) mean,
    # among those beating baseline. If baseline itself beats all challengers,
    # there's no winner to promote (status quo stands).
    best = max(significant, key=lambda r: (r.mean_value if r.mean_value is not None else float("-inf")))
    if best.relative_lift_vs_baseline is not None and best.relative_lift_vs_baseline <= 0:
        return None, False, "baseline outperforms all challengers; no promotion"

    return best.variant_id, True, (
        f"'{best.label}' significant at p={best.p_value_vs_baseline:.4f}, "
        f"lift={best.relative_lift_vs_baseline:+.1%}, sample size reached, hold period cleared"
    )
