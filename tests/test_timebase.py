"""Integer-nanosecond time rules (ADR-0007, revision of 2026-11-14; step 6)."""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from smolpy.model import Observation, RunConfig
from smolpy.model import timebase as tb
from smolpy.model.validate import ScenarioError, check_observation, check_run


def test_scenario_values_are_their_decimal_value() -> None:
    assert tb.exact(0.1) == Fraction(1, 10)  # not the binary 0.1000000000000000055...
    assert tb.exact(1e-7) == Fraction(1, 10**7)
    assert tb.exact(3) == 3 and isinstance(tb.exact(3), int)
    with pytest.raises(TypeError):
        tb.exact(True)
    with pytest.raises(ValueError):
        tb.exact(math.inf)


def test_ms_to_ns() -> None:
    assert tb.ms_to_ns(0.1) == 100_000
    assert tb.ms_to_ns(1.5) == 1_500_000
    assert tb.ms_to_ns(10_000) == 10**10
    assert tb.ms_to_ns(0.123456789) == 123_457


def test_ties_go_to_even() -> None:
    assert [tb.ms_to_ns(x) for x in (0.0000005, 0.0000015, 0.0000025, 0.0000035)] == [0, 2, 2, 4]
    assert [tb.propagation_ns(x) for x in (0.1, 0.3, 0.5, 0.7)] == [0, 2, 2, 4]
    assert [tb.draw_ns(x) for x in (0.5, 1.5, 2.5, 2.5000001)] == [0, 2, 2, 3]


def test_propagation() -> None:
    assert tb.propagation_ns(10) == 50
    assert tb.propagation_ns(2.2) == 11


@pytest.mark.parametrize("speed", [10, 100, 1_000, 10.0, 1_000.0])
def test_transmission_is_exact_up_to_1_gbps(speed: float) -> None:
    for size in range(1, 2_001):
        exact = Fraction(size * 8_000) / Fraction(speed)
        assert exact.denominator == 1
        assert tb.transmission_ns(size, speed) == exact


def test_transmission_at_10_gbps_rounds_by_half_a_nanosecond_at_most() -> None:
    assert tb.transmission_ns(1_500, 10_000) == 1_200  # divisible by 5: exact
    assert tb.transmission_ns(64, 10_000) == 51  # 51.2 ns
    assert tb.transmission_ns(66, 10_000) == 53  # 52.8 ns
    for size in range(60, 1_600):
        assert abs(tb.transmission_ns(size, 10_000) - Fraction(size * 8, 10)) <= Fraction(1, 2)
    assert tb.transmission_ns(100, 2.5) == 320_000


def test_periodic_instants_do_not_drift() -> None:
    period = tb.interval_ns(3)  # 333 333 333.33... ns
    assert period == Fraction(10**9, 3)
    assert [tb.periodic_ns(0, period, k) for k in (1, 2, 3)] == [333_333_333, 666_666_667, 10**9]
    for k in (10**6, 10**9, 10**12 + 1):
        assert abs(tb.periodic_ns(7, period, k) - (7 + k * period)) <= Fraction(1, 2)
    # adding the rounded interval instead drifts by k / 3 ns
    assert 10**9 * round(period) == tb.periodic_ns(0, period, 10**9) - 333_333_333
    # a sampling period is exact too: every = 0.1 ms is 100 000 ns
    assert tb.periodic_ns(0, tb.period_ns(0.1), 10**7) == 10**12


def test_quotient_is_rounded_once() -> None:
    assert tb.quotient(1, 3) == 1 / 3
    assert tb.quotient(Fraction(1, 10), 1) == 0.1
    assert tb.quotient(10**30 + 1, 10**30) == 1.0
    # a mean latency in µs from an integer sum of ns: same for any order of additions
    latencies_ns = [32_100, 32_100, 32_101]
    assert tb.quotient(sum(latencies_ns), len(latencies_ns) * tb.NS_PER_US) == 32.100333333333335
    assert tb.ns_to_ms(1_500_000) == 1.5
    assert tb.ns_to_ms(1) == 1e-6
    with pytest.raises(ZeroDivisionError):
        tb.quotient(1, 0)


def test_the_range_of_a_run() -> None:
    check_run(RunConfig(tb.MAX_NS // tb.NS_PER_MS))
    with pytest.raises(ScenarioError, match=r"duration must be at most 2\^63 - 1 ns"):
        check_run(RunConfig(1e14))
    with pytest.raises(ScenarioError, match="duration must be at most"):
        check_run(RunConfig(math.inf))


def test_every_is_at_least_one_nanosecond() -> None:
    check_observation(Observation("throughput", "a", 0.000001))
    with pytest.raises(ScenarioError, match=r"every must be at least 1 ns \(0.000001 ms\)"):
        check_observation(Observation("throughput", "a", 0.0000005))
    with pytest.raises(ScenarioError, match="every must be at least 1 ns"):
        check_observation(Observation("throughput", "a", math.inf))
