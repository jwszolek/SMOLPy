"""The Rust engine's time rules against smolpy.model.timebase (ADR-0008).

Bit-exact equivalence needs both engines to turn every scenario number into
the same nanoseconds and every metric into the same float. These tests feed
both the same random inputs.
"""

from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest

from smolpy.model import timebase as tb

rust = pytest.importorskip("smolpy_engine_rs", reason="the rust backend is not installed")

_RNG = random.Random(20261115)


def _numbers(n: int) -> list[float]:
    """Scenario-like values: integers, short decimals and awkward floats."""
    out: list[float] = [0.1, 0.25, 1e-07, 2.5, 1.5, 0.0000005, 0.0000015, 1e3, 333.3]
    for _ in range(n):
        out.append(round(_RNG.uniform(0, 10_000), _RNG.randint(0, 9)))
        out.append(_RNG.uniform(0, 100))
        out.append(float(_RNG.randint(1, 10**6)))
    return out


def test_ms_to_ns_and_propagation() -> None:
    for x in _numbers(500):
        assert rust._ms_to_ns(repr(x)) == tb.ms_to_ns(x), x
        assert rust._propagation_ns(repr(x)) == tb.propagation_ns(x), x


def test_transmission() -> None:
    speeds = [10, 100, 1_000, 10_000, 2.5, 25_000.0, 40_000, 0.3]
    for speed, size in itertools.product(speeds, [64, 65, 66, 72, 512, 594, 1_518, 1_522, 9_018]):
        assert rust._transmission_ns(size, repr(speed)) == tb.transmission_ns(size, speed)


def test_instants() -> None:
    for rate in [3, 7_000, 50_000.0, 1e6 / 1.2, 2.5, 333.3, 1_234.5678]:
        start = _RNG.randint(0, 10**9)
        period = tb.interval_ns(rate)
        got = rust._instants(start, repr(rate), 5_000)
        assert got == list(itertools.islice(tb.instants(start, period), 5_000)), rate


def test_ratio_is_rounded_as_python_does() -> None:
    pairs = [(1, 3), (10**30 + 1, 10**30), (2**64 + 1, 3), (7, 2**60 + 1), (10**20, 7)]
    for _ in range(5_000):
        pairs.append(
            (_RNG.randint(1, 10 ** _RNG.randint(1, 30)), _RNG.randint(1, 10 ** _RNG.randint(1, 30)))
        )
    for n, d in pairs:
        assert rust._ratio(str(n), str(d)) == n / d == float(Fraction(n, d)), (n, d)
