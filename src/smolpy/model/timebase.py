"""Simulated time in integer nanoseconds (ADR-0007, revision of 2026-11-14).

The rules every engine follows to turn scenario values into simulated time,
and simulated quantities into metric values. They are normative; the text
is in Model Semantics §1 and §8.

- **Exact value, then one rounding.** A scenario value means its shortest
  decimal representation, as written in the scenario JSON: ``0.1`` ms is
  exactly 1/10 ms. A time quantity is computed from such values exactly,
  then rounded once to the nearest nanosecond, ties to even.
- **No drift.** A periodic source's *k*-th event is at
  ``start + round(k * period)`` with the period kept exact.
- **Metrics** are exact quotients of integers (or of exact values) rounded
  to a float once, so they do not depend on the order of additions.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from fractions import Fraction

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
NS_PER_M = 5  # propagation at 2 * 10^8 m/s
MAX_NS = 2**63 - 1  # a signed 64-bit count of nanoseconds: 292 years

Exact = int | Fraction


def exact(value: float | int) -> Exact:
    """The exact value a scenario number stands for: an int as is, a float
    as its shortest decimal representation."""
    if isinstance(value, bool):
        raise TypeError(f"expected a number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _normal(Fraction(repr(value)))  # raises for NaN and infinities
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _normal(value: Exact) -> Exact:
    """An integral Fraction as an int, so integer fast paths apply."""
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    return value


def round_ns(value: Exact) -> int:
    """Nearest integer, ties to even, of an exact number of nanoseconds."""
    return round(value)


def ms_to_ns(ms: float | int) -> int:
    """A duration, delay, cycle or ``every`` in ms, in nanoseconds."""
    return round_ns(exact(ms) * NS_PER_MS)


def period_ns(ms: float | int) -> Exact:
    """A period given in ms, as an exact number of nanoseconds (not rounded)."""
    return _normal(exact(ms) * NS_PER_MS)


def interval_ns(rate_per_s: float | int) -> Exact:
    """The exact interval of a source with *rate_per_s* events per second."""
    return _normal(Fraction(NS_PER_S) / exact(rate_per_s))


def propagation_ns(length_m: float | int) -> int:
    """Propagation delay of a link of *length_m* metres."""
    return round_ns(exact(length_m) * NS_PER_M)


def transmission_ns(size_bytes: int, speed_mbps: float | int) -> int:
    """Transmission time of a frame of *size_bytes* at *speed_mbps*.

    Exact at 10, 100 and 1 000 Mb/s; at 10 Gb/s exact for sizes divisible by
    5 bytes and otherwise rounded by at most 0.5 ns.
    """
    return round_ns(Fraction(size_bytes * 8 * NS_PER_US) / exact(speed_mbps))


def periodic_ns(start_ns: int, period: Exact, k: int) -> int:
    """The instant of the *k*-th event of a periodic source that starts at
    *start_ns* with an exact *period* (ns): within 0.5 ns of exact, for any k."""
    if isinstance(period, int):
        return start_ns + k * period
    return start_ns + _round_ratio(k * period.numerator, period.denominator)


def instants(start_ns: int, period: Exact) -> Iterator[int]:
    """The instants ``periodic_ns(start_ns, period, k)`` for k = 1, 2, ...
    in integer arithmetic only, for engines that need one per event."""
    period = _normal(period)
    if isinstance(period, int):
        return itertools.count(start_ns + period, period)
    return _fractional_instants(start_ns, period.numerator, period.denominator)


def _fractional_instants(start_ns: int, num: int, den: int) -> Iterator[int]:
    total = 0
    while True:
        total += num
        yield start_ns + _round_ratio(total, den)


def _round_ratio(num: int, den: int) -> int:
    """``round(num / den)``, ties to even, for integers with den > 0."""
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q & 1):
        q += 1
    return q


def draw_ns(value_ns: float) -> int:
    """A random draw (a float number of nanoseconds) as a delay."""
    return round(value_ns)


def quotient(numerator: Exact, denominator: Exact) -> float:
    """``numerator / denominator`` computed exactly and rounded to the
    nearest float once."""
    if denominator == 0:
        raise ZeroDivisionError("quotient with a zero denominator")
    if isinstance(numerator, int) and isinstance(denominator, int):
        return numerator / denominator  # int / int is correctly rounded
    return float(Fraction(numerator) / Fraction(denominator))


def ns_to_ms(t_ns: int) -> float:
    """A sample time, as reported: milliseconds."""
    return quotient(t_ns, NS_PER_MS)
