"""Batch-size distributions for the ``batch`` traffic pattern.

A ``batch`` flow emits frames in groups: group arrival instants form a
Poisson process, and every frame of a group is enqueued at the same instant
(back to back). The group size X is drawn independently for each group from
one of these distributions:

* ``k`` (an int, or a string such as ``"8"``) -- every group has exactly k
  frames;
* ``"geom:m"`` -- geometric on {1, 2, ...} with mean m;
* ``"pareto:a"`` -- ``floor(Y)`` with Y ~ Pareto(shape a, scale 1), i.e. the
  heavy-tailed burst length used by the ``bursty`` pattern, but with a
  configurable shape. Its mean is the Riemann zeta value zeta(a), finite for
  a > 1; its second moment, 2*zeta(a-1) - zeta(a), is finite only for a > 2.

The flow's ``rate`` stays the long-run average number of frames per second;
the group arrival rate is ``rate / E[X]``. Changing the batch size therefore
changes how bursty the traffic is without changing the average load, which is
what a burstiness experiment needs.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal

BatchKind = Literal["fixed", "geom", "pareto"]


@dataclass(frozen=True)
class BatchSpec:
    kind: BatchKind
    param: float

    def mean(self) -> float:
        """E[X]."""
        if self.kind == "fixed":
            return self.param
        if self.kind == "geom":
            return self.param
        return _zeta(self.param)

    def second_moment(self) -> float:
        """E[X^2]; ``math.inf`` when it diverges (pareto with shape <= 2)."""
        if self.kind == "fixed":
            return self.param**2
        if self.kind == "geom":
            p = 1.0 / self.param
            return (2.0 - p) / (p * p)
        if self.param <= 2.0:
            return math.inf
        return 2.0 * _zeta(self.param - 1.0) - _zeta(self.param)

    def draw(self, rng: random.Random) -> int:
        if self.kind == "fixed":
            return int(self.param)
        if self.kind == "geom":
            p = 1.0 / self.param
            if p >= 1.0:
                return 1
            # Inverse transform for the geometric distribution on {1, 2, ...}.
            u = 1.0 - rng.random()  # in (0, 1]
            return 1 + int(math.log(u) / math.log(1.0 - p))
        return max(1, int(rng.paretovariate(self.param)))


def parse_batch(spec: int | str) -> BatchSpec:
    """Parse a batch-size spec; raises ValueError on invalid input."""
    if isinstance(spec, bool):
        raise ValueError(f"invalid batch size {spec!r}")
    if isinstance(spec, int):
        if spec < 1:
            raise ValueError(f"batch size must be >= 1, got {spec}")
        return BatchSpec("fixed", float(spec))
    text = str(spec).strip()
    if text.isdigit():
        return parse_batch(int(text))
    kind, sep, value = text.partition(":")
    if not sep:
        raise ValueError(
            f"invalid batch spec {spec!r}: expected an integer, 'geom:<mean>' "
            "or 'pareto:<shape>'"
        )
    try:
        param = float(value)
    except ValueError:
        raise ValueError(f"invalid batch spec {spec!r}: {value!r} is not a number") from None
    if kind == "geom":
        if param < 1.0:
            raise ValueError(f"geometric batch mean must be >= 1, got {param}")
        return BatchSpec("geom", param)
    if kind == "pareto":
        if param <= 1.0:
            raise ValueError(f"pareto batch shape must be > 1 (finite mean), got {param}")
        return BatchSpec("pareto", param)
    raise ValueError(f"invalid batch kind {kind!r}: expected 'geom' or 'pareto'")


def _zeta(s: float, terms: int = 10_000) -> float:
    """Riemann zeta(s) for s > 1: direct sum plus an Euler-Maclaurin tail."""
    n = terms
    head = math.fsum(k ** (-s) for k in range(1, n))
    tail = n ** (1.0 - s) / (s - 1.0) + 0.5 * n ** (-s) + s * n ** (-s - 1.0) / 12.0
    return head + tail
