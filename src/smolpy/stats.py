"""Statistics across runs (ADR-0009), in plain Python: no numpy or scipy."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MeanCI:
    """Mean of *n* values with the sample standard deviation and a two-sided
    confidence interval at *level* (Student's *t*). With fewer than two
    values, *std* and the interval are NaN."""

    n: int
    mean: float
    std: float
    low: float
    high: float
    level: float = 0.95

    @property
    def half_width(self) -> float:
        return (self.high - self.low) / 2


def mean_ci(values: Sequence[float], level: float = 0.95) -> MeanCI:
    """Mean, standard deviation and confidence interval of *values*.

    NaN values are not dropped: they make the result NaN, so a missing value
    is never hidden.
    """
    if not 0 < level < 1:
        raise ValueError(f"level must be between 0 and 1, got {level!r}")
    n = len(values)
    if n == 0:
        raise ValueError("mean_ci needs at least one value")
    mean = math.fsum(values) / n
    if n == 1:
        nan = math.nan
        return MeanCI(1, mean, nan, nan, nan, level)
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (n - 1))
    half = t_quantile((1 + level) / 2, n - 1) * std / math.sqrt(n)
    return MeanCI(n, mean, std, mean - half, mean + half, level)


def t_quantile(p: float, df: float) -> float:
    """The *p* quantile of Student's *t* distribution with *df* degrees of
    freedom, e.g. ``t_quantile(0.975, 29) == 2.0452...``.

    Relative error below 1e-11 for df up to 1e5, checked against scipy.
    """
    if not 0 < p < 1:
        raise ValueError(f"p must be between 0 and 1, got {p!r}")
    if not df > 0:
        raise ValueError(f"df must be positive, got {df!r}")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_quantile(1 - p, df)
    # Bracket, then bisect: t_cdf is increasing, and bisection on floats
    # converges to the closest representable value.
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < p:
        lo, hi = hi, hi * 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if mid in (lo, hi):
            break
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def t_cdf(t: float, df: float) -> float:
    """Cumulative distribution function of Student's *t* with *df* degrees
    of freedom."""
    # x and 1 - x are both computed directly: for large df, x is close to 1
    # and 1 - x would lose most of its digits.
    tail = 0.5 * _betainc(df / 2, 0.5, df / (df + t * t), t * t / (df + t * t))  # P(T > |t|)
    return 1 - tail if t >= 0 else tail


def _betainc(a: float, b: float, x: float, y: float) -> float:
    """Regularised incomplete beta function I_x(a, b), with y = 1 - x given
    separately, by its continued fraction (Lentz's method)."""
    if x <= 0:
        return 0.0
    if y <= 0:
        return 1.0
    # The continued fraction converges fast for x < (a + 1) / (a + b + 2).
    if x > (a + 1) / (a + b + 2):
        return 1 - _betainc(b, a, y, x)
    # log of a value close to 1 through log1p of its complement
    log_x = math.log1p(-y) if y < 0.5 else math.log(x)
    log_y = math.log1p(-x) if x < 0.5 else math.log(y)
    log_front = a * log_x + b * log_y - _log_beta(a, b)
    tiny = 1e-300
    f = c = 1.0
    d = 0.0
    for i in range(500):
        k = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = k * (b - k) * x / ((a + 2 * k - 1) * (a + 2 * k))
        else:
            num = -(a + k) * (a + b + k) * x / ((a + 2 * k) * (a + 2 * k + 1))
        d = 1 + num * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + num / c
        c = c if abs(c) > tiny else tiny
        f *= c * d
        if abs(c * d - 1) < 1e-15:
            break
    return math.exp(log_front) * (f - 1) / a


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


@dataclass(frozen=True)
class DiffCI:
    """Difference of two means, ``mean(a) - mean(b)``, with Welch's
    confidence interval at *level* and its degrees of freedom *df*."""

    diff: float
    low: float
    high: float
    df: float
    level: float = 0.95

    def contains(self, value: float = 0.0) -> bool:
        return self.low <= value <= self.high


def diff_ci(a: Sequence[float], b: Sequence[float], level: float = 0.95) -> DiffCI:
    """Welch's confidence interval for ``mean(a) - mean(b)``: no assumption
    that the two samples have the same variance.

    Both samples need at least two values. When both have zero variance the
    interval is the single point *diff* and *df* is NaN. NaN values make the
    result NaN, as in :func:`mean_ci`.
    """
    if len(a) < 2 or len(b) < 2:
        raise ValueError(
            f"diff_ci needs at least two values in each sample, got {len(a)} and {len(b)}"
        )
    ca, cb = mean_ci(a, level), mean_ci(b, level)
    diff = ca.mean - cb.mean
    va, vb = ca.std**2 / ca.n, cb.std**2 / cb.n  # squared standard errors
    se2 = va + vb
    if se2 == 0:
        return DiffCI(diff, diff, diff, math.nan, level)
    df = se2**2 / (va**2 / (ca.n - 1) + vb**2 / (cb.n - 1))
    if math.isnan(df):
        nan = math.nan
        return DiffCI(diff, nan, nan, nan, level)
    half = t_quantile((1 + level) / 2, df) * math.sqrt(se2)
    return DiffCI(diff, diff - half, diff + half, df, level)
