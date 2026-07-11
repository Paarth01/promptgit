import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.services.stats import VariantResult  # noqa: E402
from app.services.winner import determine_winner  # noqa: E402


def _result(**kwargs):
    defaults = dict(
        variant_id="v1",
        label="variant",
        is_baseline=False,
        sample_size=100,
        mean_value=0.5,
        std_dev=None,
        error_rate=0.0,
        p_value_vs_baseline=None,
        is_significant=None,
        test_used=None,
        relative_lift_vs_baseline=None,
    )
    defaults.update(kwargs)
    return VariantResult(**defaults)


def test_determine_winner_handles_none_lift_without_crashing():
    """Regression test for the crash the integration suite caught: when a
    baseline's rate is exactly 0, relative_lift_vs_baseline is legitimately
    None (undefined), and determine_winner's message formatting must not
    blow up with a TypeError on None:+.1% formatting."""
    baseline = _result(variant_id="base", label="baseline", is_baseline=True, mean_value=0.0)
    winner = _result(
        variant_id="win",
        label="winner",
        mean_value=0.8,
        p_value_vs_baseline=0.001,
        is_significant=True,
        relative_lift_vs_baseline=None,  # the undefined case
    )
    past_hold = datetime.now(timezone.utc) - timedelta(hours=1)
    variant_id, ready, reason = determine_winner(
        [baseline, winner], target_sample_size=50, hold_until=past_hold, guardrail_triggered=False
    )
    assert variant_id == "win"
    assert ready is True
    assert "undefined" in reason


def test_determine_winner_waits_for_sample_size():
    winner = _result(sample_size=10, is_significant=True, relative_lift_vs_baseline=0.2)
    variant_id, ready, reason = determine_winner(
        [winner], target_sample_size=50, hold_until=None, guardrail_triggered=False
    )
    assert ready is False
    assert "sample size" in reason


def test_determine_winner_waits_for_hold_period():
    winner = _result(sample_size=100, is_significant=True, relative_lift_vs_baseline=0.2)
    future_hold = datetime.now(timezone.utc) + timedelta(hours=1)
    variant_id, ready, reason = determine_winner(
        [winner], target_sample_size=50, hold_until=future_hold, guardrail_triggered=False
    )
    assert ready is False
    assert "hold" in reason


def test_determine_winner_no_promotion_when_baseline_wins():
    baseline = _result(variant_id="base", label="baseline", is_baseline=True, mean_value=0.9)
    loser = _result(
        variant_id="loser",
        mean_value=0.5,
        is_significant=True,
        relative_lift_vs_baseline=-0.4,  # worse than baseline
        p_value_vs_baseline=0.001,
    )
    variant_id, ready, reason = determine_winner(
        [baseline, loser],
        target_sample_size=50,
        hold_until=datetime.now(timezone.utc) - timedelta(hours=1),
        guardrail_triggered=False,
    )
    assert variant_id is None
    assert ready is False


def test_determine_winner_guardrail_blocks_promotion_even_if_significant():
    winner = _result(sample_size=100, is_significant=True, relative_lift_vs_baseline=0.2)
    variant_id, ready, reason = determine_winner(
        [winner], target_sample_size=50, hold_until=None, guardrail_triggered=True
    )
    assert variant_id is None
    assert ready is False
    assert "guardrail" in reason
