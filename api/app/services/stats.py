"""Phase 3: significance testing.

Test selection:
  - binary metrics (conversion/success rate)   -> two-proportion z-test
  - continuous metrics, roughly normal          -> Welch's t-test (unequal var)
  - continuous metrics, better safe than sorry  -> Mann-Whitney U (non-parametric,
    used as the default for 'continuous' since we can't assume normality of
    LLM-judge scores, latency, etc. without checking)

We report both a p-value and an observed relative lift, and separately track
whether the experiment has reached its pre-registered target_sample_size —
significance and "enough data" are different questions and the dashboard
should show both.
"""
from dataclasses import dataclass

import numpy as np
from scipy import stats as scipy_stats

ALPHA = 0.05  # 95% confidence, matches spec's "auto-promote at 95% confidence"


@dataclass
class VariantSample:
    variant_id: str
    label: str
    is_baseline: bool
    values: list[float]      # primary_metric_value for continuous metrics
    successes: int = 0        # for binary metrics
    n: int = 0                 # for binary metrics (== len(values) for continuous)
    error_count: int = 0
    total_events: int = 0


@dataclass
class VariantResult:
    variant_id: str
    label: str
    is_baseline: bool
    sample_size: int
    mean_value: float | None
    std_dev: float | None
    error_rate: float
    p_value_vs_baseline: float | None
    is_significant: bool | None
    test_used: str | None
    relative_lift_vs_baseline: float | None


def _welch_t_test(baseline: list[float], variant: list[float]) -> float:
    if len(baseline) < 2 or len(variant) < 2:
        return 1.0
    _, p = scipy_stats.ttest_ind(baseline, variant, equal_var=False)
    return float(p)


def _mann_whitney(baseline: list[float], variant: list[float]) -> float:
    if len(baseline) < 1 or len(variant) < 1:
        return 1.0
    try:
        _, p = scipy_stats.mannwhitneyu(baseline, variant, alternative="two-sided")
        return float(p)
    except ValueError:
        # identical distributions or all-tied values -> mannwhitneyu raises
        return 1.0


def _two_proportion_z_test(
    baseline_successes: int, baseline_n: int, variant_successes: int, variant_n: int
) -> float:
    if baseline_n == 0 or variant_n == 0:
        return 1.0
    p1, p2 = baseline_successes / baseline_n, variant_successes / variant_n
    p_pool = (baseline_successes + variant_successes) / (baseline_n + variant_n)
    se = np.sqrt(p_pool * (1 - p_pool) * (1 / baseline_n + 1 / variant_n))
    if se == 0:
        return 1.0
    z = (p2 - p1) / se
    p_value = 2 * (1 - scipy_stats.norm.cdf(abs(z)))
    return float(p_value)


def analyze_experiment(
    samples: list[VariantSample], metric_type: str, use_nonparametric: bool = True
) -> list[VariantResult]:
    baseline = next((s for s in samples if s.is_baseline), None)
    results: list[VariantResult] = []

    for s in samples:
        error_rate = (s.error_count / s.total_events) if s.total_events else 0.0

        if metric_type == "binary":
            mean_value = (s.successes / s.n) if s.n else None
            std_dev = None
            test_used = "two_proportion_z_test"
        else:
            mean_value = float(np.mean(s.values)) if s.values else None
            std_dev = float(np.std(s.values, ddof=1)) if len(s.values) > 1 else None
            test_used = "mann_whitney_u" if use_nonparametric else "welch_t_test"

        p_value, is_significant, lift = None, None, None

        if baseline is not None and s.variant_id != baseline.variant_id:
            if metric_type == "binary":
                p_value = _two_proportion_z_test(
                    baseline.successes, baseline.n, s.successes, s.n
                )
            else:
                if use_nonparametric:
                    p_value = _mann_whitney(baseline.values, s.values)
                else:
                    p_value = _welch_t_test(baseline.values, s.values)

            is_significant = p_value < ALPHA
            baseline_mean = (
                (baseline.successes / baseline.n) if metric_type == "binary" and baseline.n
                else (float(np.mean(baseline.values)) if baseline.values else None)
            )
            if baseline_mean and mean_value is not None and baseline_mean != 0:
                lift = (mean_value - baseline_mean) / abs(baseline_mean)

        results.append(VariantResult(
            variant_id=s.variant_id,
            label=s.label,
            is_baseline=s.is_baseline,
            sample_size=s.n if metric_type == "binary" else len(s.values),
            mean_value=mean_value,
            std_dev=std_dev,
            error_rate=error_rate,
            p_value_vs_baseline=p_value,
            is_significant=is_significant,
            test_used=test_used if s.variant_id != (baseline.variant_id if baseline else None) else None,
            relative_lift_vs_baseline=lift,
        ))

    return results


def minimum_detectable_effect(
    baseline_rate: float, n_per_variant: int, alpha: float = ALPHA, power: float = 0.8
) -> float:
    """Approximate MDE for a binary metric given current sample size, so the
    dashboard can show 'at this sample size, you can detect an effect of at
    least X%' even before the experiment concludes."""
    z_alpha = scipy_stats.norm.ppf(1 - alpha / 2)
    z_power = scipy_stats.norm.ppf(power)
    p = baseline_rate
    if n_per_variant <= 0 or p <= 0 or p >= 1:
        return float("nan")
    se = np.sqrt(2 * p * (1 - p) / n_per_variant)
    mde = (z_alpha + z_power) * se
    return float(mde)


ERROR_RATE_GUARDRAIL = 0.15          # halt if any variant's error rate exceeds this
UNDERPERFORM_GUARDRAIL_P = 0.01      # halt if variant is *significantly worse* at this p threshold


def check_guardrails(results: list[VariantResult]) -> tuple[bool, str | None]:
    """Auto-stop guardrails: error-rate spikes or a variant significantly
    underperforming baseline. Returns (should_halt, reason)."""
    for r in results:
        if r.error_rate > ERROR_RATE_GUARDRAIL:
            return True, f"variant '{r.label}' error rate {r.error_rate:.1%} exceeds guardrail"

    for r in results:
        if r.is_baseline or r.p_value_vs_baseline is None:
            continue
        if (
            r.p_value_vs_baseline < UNDERPERFORM_GUARDRAIL_P
            and r.relative_lift_vs_baseline is not None
            and r.relative_lift_vs_baseline < 0
        ):
            return True, (
                f"variant '{r.label}' significantly underperforming baseline "
                f"(p={r.p_value_vs_baseline:.4f}, lift={r.relative_lift_vs_baseline:.1%})"
            )
    return False, None
