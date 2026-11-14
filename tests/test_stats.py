"""Student's t and confidence intervals in plain Python (ADR-0009, step 2)."""

from __future__ import annotations

import math

import pytest

from smolpy.stats import mean_ci, t_cdf, t_quantile


# Reference values: scipy.stats.t.ppf.
@pytest.mark.parametrize(
    "p, df, expected",
    [
        (0.975, 1, 12.706204736174694),
        (0.975, 2, 4.302652729749462),
        (0.975, 5, 2.5705818356363146),
        (0.975, 10, 2.228138851986274),
        (0.975, 29, 2.045229642132703),
        (0.975, 100, 1.9839715185235518),
        (0.995, 3, 5.840909309733355),
        (0.9995, 1, 636.6192487687897),
        (0.95, 9, 1.833112932656237),
        (0.975, 2.5, 3.5746548420036817),
        (0.6, 4, 0.2707222947075976),
    ],
)
def test_t_quantile_matches_tables(p: float, df: float, expected: float) -> None:
    assert t_quantile(p, df) == pytest.approx(expected, rel=1e-12)


def test_t_quantile_for_many_degrees_of_freedom() -> None:
    # far more runs than a batch has; precision is lost only as df -> 1e9
    assert t_quantile(0.975, 1e4) == pytest.approx(1.960201239890626, rel=1e-10)
    assert t_quantile(0.975, 1e5) == pytest.approx(1.9599877075346095, rel=1e-10)


def test_t_quantile_is_symmetric_and_inverts_the_cdf() -> None:
    assert t_quantile(0.5, 7) == 0.0
    assert t_quantile(0.025, 29) == -t_quantile(0.975, 29)
    for p in (0.6, 0.9, 0.99):
        assert t_cdf(t_quantile(p, 4), 4) == pytest.approx(p, rel=1e-12)


@pytest.mark.parametrize(
    "p, df, message",
    [(0, 5, "p must be between 0 and 1"), (1.0, 5, "p must be"), (0.9, 0, "df must be positive")],
)
def test_t_quantile_rejects(p: float, df: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        t_quantile(p, df)


def test_mean_ci() -> None:
    ci = mean_ci([1, 2, 3, 4, 5])
    assert (ci.n, ci.mean, ci.level) == (5, 3.0, 0.95)
    assert ci.std == pytest.approx(math.sqrt(2.5))
    half = 2.7764451051977934 * math.sqrt(2.5) / math.sqrt(5)
    assert (ci.low, ci.high) == pytest.approx((3 - half, 3 + half))
    assert ci.half_width == pytest.approx(half)
    assert mean_ci([1, 2, 3, 4, 5], level=0.99).half_width > half


def test_mean_ci_of_one_value_has_no_interval() -> None:
    ci = mean_ci([4.0])
    assert ci.n == 1 and ci.mean == 4.0
    assert math.isnan(ci.std) and math.isnan(ci.low) and math.isnan(ci.high)


def test_mean_ci_does_not_hide_nan() -> None:
    assert math.isnan(mean_ci([1.0, math.nan, 3.0]).mean)


def test_mean_ci_rejects() -> None:
    with pytest.raises(ValueError, match="at least one value"):
        mean_ci([])
    with pytest.raises(ValueError, match="level must be between 0 and 1"):
        mean_ci([1, 2], level=95)
