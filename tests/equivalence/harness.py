"""Compare the results of a candidate backend against SimPy, the reference
implementation (ADR-0007, ADR-0009).

- **Deterministic scenarios** (no randomness): every metric window must agree
  within ``abs_tol + rel_tol * |reference|``. The tolerance only covers the
  representation of time (SimPy float µs, Rust integer ns): ``rel_tol`` is
  1e-6 and ``abs_tol`` is 1 ns in the metric's unit.
- **Stochastic scenarios**: 30 replications per backend, with different
  seeds. For each metric's per-run mean, Welch's 95 % confidence interval of
  the difference of means must contain 0, and the means must differ by less
  than 2 %.

Every comparison returns a list of :class:`Mismatch`; empty means equivalent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from smolpy import batch as smolpy_batch
from smolpy.batch import BatchResult, run_many
from smolpy.core.network import SimulationResult
from smolpy.model import Scenario
from smolpy.stats import diff_ci

REFERENCE = "simpy"
KNOWN_BACKENDS = ("simpy", "rust", "ns3")

REL_TOL = 1e-6
TIME_TOL_MS = 1e-6  # 1 ns
# 1 ns in the unit of each metric measured in µs; the others are counts,
# bytes or rates, which time rounding does not change.
ABS_TOL = {"latency": 1e-3, "modbus_latency": 1e-3, "pn_latency": 1e-3, "pn_jitter": 1e-3}

REPLICATIONS = 30
LEVEL = 0.95
MAX_REL_DIFF = 0.02
CANDIDATE_SEED_OFFSET = 1000  # the candidate's runs are independent of the reference's


def installed(backend: str) -> bool:
    return backend in smolpy_batch.BACKENDS


@dataclass(frozen=True)
class Mismatch:
    metric: str
    detail: str

    def __str__(self) -> str:
        return f"{self.metric}: {self.detail}"


def abs_tol(key: str) -> float:
    """The absolute tolerance for a metric key such as ``"latency:plc"``."""
    return ABS_TOL.get(key.split(":", 1)[0], 0.0)


# --- deterministic ---


def run_once(scenario: Scenario, backend: str) -> SimulationResult:
    batch = run_many(scenario, runs=1, backend=backend, workers=1)
    batch.raise_on_failure()
    (result,) = batch.results()
    return result


def compare_runs(
    candidate: SimulationResult, reference: SimulationResult, rel_tol: float = REL_TOL
) -> list[Mismatch]:
    """Window-by-window comparison of two runs of a deterministic scenario."""
    out = _compare_keys(set(candidate.metrics), set(reference.metrics))
    for key in sorted(set(candidate.metrics) & set(reference.metrics)):
        got, want = candidate.metrics[key], reference.metrics[key]
        if len(got) != len(want):
            out.append(Mismatch(key, f"{len(got)} samples, the reference has {len(want)}"))
            continue
        tol = abs_tol(key)
        bad = [
            (i, g, w)
            for i, (g, w) in enumerate(zip(got, want, strict=True))
            if abs(g[0] - w[0]) > TIME_TOL_MS
            or not abs(g[1] - w[1]) <= tol + rel_tol * abs(w[1])  # NaN never agrees
        ]
        if bad:
            i, g, w = bad[0]
            out.append(
                Mismatch(
                    key,
                    f"{len(bad)} of {len(want)} samples differ, first at index {i}:"
                    f" {g} against {w}",
                )
            )
    return out


# --- stochastic ---


def run_replications(
    scenario: Scenario, backend: str, runs: int = REPLICATIONS, seed_offset: int = 0
) -> BatchResult:
    assert scenario.run is not None
    base = scenario.run.seed + seed_offset
    batch = run_many(scenario, runs=runs, seeds=[base + k for k in range(runs)], backend=backend)
    batch.raise_on_failure()
    return batch


def compare_batches(
    candidate: BatchResult,
    reference: BatchResult,
    level: float = LEVEL,
    max_rel_diff: float = MAX_REL_DIFF,
) -> list[Mismatch]:
    """Compare the per-run means of every metric of two batches of one
    stochastic scenario."""
    keys_c = {k for r in candidate.results() for k in r.metrics}
    keys_r = {k for r in reference.results() for k in r.metrics}
    out = _compare_keys(keys_c, keys_r)
    for key in sorted(keys_c & keys_r):
        a, b = candidate.scalar(key), reference.scalar(key)
        empty_a, empty_b = sum(map(math.isnan, a)), sum(map(math.isnan, b))
        if empty_a or empty_b:
            # A window metric of a run without any frames. The scenarios are
            # chosen so that this does not happen; if it does, say so.
            out.append(Mismatch(key, f"runs without samples: {empty_a} and {empty_b}"))
            continue
        ci = diff_ci(a, b, level)
        mean_b = math.fsum(b) / len(b)
        if not ci.contains(0):
            out.append(
                Mismatch(
                    key,
                    f"difference of means {ci.diff:.6g}, {level:.0%} interval"
                    f" [{ci.low:.6g}, {ci.high:.6g}] excludes 0",
                )
            )
        rel = abs(ci.diff) / abs(mean_b) if mean_b else (0.0 if ci.diff == 0 else math.inf)
        if not rel < max_rel_diff:
            out.append(Mismatch(key, f"means differ by {rel:.2%}, more than {max_rel_diff:.0%}"))
    return out


def _compare_keys(candidate: set[str], reference: set[str]) -> list[Mismatch]:
    return [Mismatch(k, "missing from the candidate") for k in sorted(reference - candidate)] + [
        Mismatch(k, "not in the reference") for k in sorted(candidate - reference)
    ]
