import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.services.stats import (  # noqa: E402
    VariantSample, analyze_experiment, check_guardrails, _two_proportion_z_test,
)


def test_binary_metric_detects_real_difference():
    random.seed(42)
    baseline = VariantSample(
        variant_id="a", label="baseline", is_baseline=True,
        values=[], successes=140, n=200, error_count=0, total_events=200,
    )
    variant = VariantSample(
        variant_id="b", label="variant", is_baseline=False,
        values=[], successes=175, n=200, error_count=0, total_events=200,
    )
    results = analyze_experiment([baseline, variant], "binary")
    variant_result = next(r for r in results if r.label == "variant")
    assert variant_result.p_value_vs_baseline is not None
    assert variant_result.p_value_vs_baseline < 0.05
    assert variant_result.is_significant is True
    assert variant_result.relative_lift_vs_baseline > 0


def test_binary_metric_no_difference_not_significant():
    baseline = VariantSample(
        variant_id="a", label="baseline", is_baseline=True,
        values=[], successes=100, n=200, error_count=0, total_events=200,
    )
    variant = VariantSample(
        variant_id="b", label="variant", is_baseline=False,
        values=[], successes=102, n=200, error_count=0, total_events=200,
    )
    results = analyze_experiment([baseline, variant], "binary")
    variant_result = next(r for r in results if r.label == "variant")
    assert variant_result.p_value_vs_baseline > 0.05
    assert variant_result.is_significant is False


def test_continuous_metric_mann_whitney():
    random.seed(1)
    baseline_vals = [random.gauss(100, 10) for _ in range(100)]
    variant_vals = [random.gauss(130, 10) for _ in range(100)]  # clearly higher
    baseline = VariantSample(
        variant_id="a", label="baseline", is_baseline=True,
        values=baseline_vals, n=len(baseline_vals), total_events=len(baseline_vals),
    )
    variant = VariantSample(
        variant_id="b", label="variant", is_baseline=False,
        values=variant_vals, n=len(variant_vals), total_events=len(variant_vals),
    )
    results = analyze_experiment([baseline, variant], "continuous")
    variant_result = next(r for r in results if r.label == "variant")
    assert variant_result.is_significant is True
    assert variant_result.relative_lift_vs_baseline > 0.1


def test_guardrail_error_rate_spike():
    baseline = VariantSample(
        variant_id="a", label="baseline", is_baseline=True,
        values=[], successes=50, n=100, error_count=2, total_events=100,
    )
    bad_variant = VariantSample(
        variant_id="b", label="bad", is_baseline=False,
        values=[], successes=50, n=100, error_count=25, total_events=100,  # 25% error rate
    )
    results = analyze_experiment([baseline, bad_variant], "binary")
    halt, reason = check_guardrails(results)
    assert halt is True
    assert "error rate" in reason


def test_guardrail_significant_underperformance():
    baseline = VariantSample(
        variant_id="a", label="baseline", is_baseline=True,
        values=[], successes=150, n=200, error_count=0, total_events=200,
    )
    bad_variant = VariantSample(
        variant_id="b", label="bad", is_baseline=False,
        values=[], successes=60, n=200, error_count=0, total_events=200,  # much worse
    )
    results = analyze_experiment([baseline, bad_variant], "binary")
    halt, reason = check_guardrails(results)
    assert halt is True
    assert "underperforming" in reason


def test_no_guardrail_when_healthy():
    baseline = VariantSample(
        variant_id="a", label="baseline", is_baseline=True,
        values=[], successes=140, n=200, error_count=1, total_events=200,
    )
    variant = VariantSample(
        variant_id="b", label="variant", is_baseline=False,
        values=[], successes=150, n=200, error_count=2, total_events=200,
    )
    results = analyze_experiment([baseline, variant], "binary")
    halt, reason = check_guardrails(results)
    assert halt is False
    assert reason is None


def test_two_proportion_z_test_symmetric():
    p1 = _two_proportion_z_test(50, 100, 60, 100)
    p2 = _two_proportion_z_test(60, 100, 50, 100)
    assert abs(p1 - p2) < 1e-9  # p-value shouldn't care which side is "baseline"
