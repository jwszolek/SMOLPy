"""Compare the results of a candidate backend against SimPy, the reference
implementation (ADR-0007, ADR-0009).

- **Deterministic scenarios** (no randomness): every metric sample must be
  identical, time and value, bit for bit. Every engine uses the same integer
  nanosecond clock and computes each value as one rounded quotient (Model
  Semantics §1, §8), so there is nothing a tolerance would have to cover.
- **Stochastic scenarios**: 30 replications per backend, with different
  seeds. For each metric's per-run mean, Welch's confidence interval of the
  difference of means must contain 0, and the means must differ by less than
  2 %. The interval of each of a scenario's *k* metrics is at
  ``1 - 0.05 / k`` (Bonferroni), so that two equivalent backends fail a
  scenario by chance at most 5 % of the time, however many metrics it has.

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

REPLICATIONS = 30
LEVEL = 0.95  # for the scenario as a whole; each metric gets 1 - (1 - LEVEL) / k
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


# --- deterministic ---


def run_once(scenario: Scenario, backend: str) -> SimulationResult:
    batch = run_many(scenario, runs=1, backend=backend, workers=1)
    batch.raise_on_failure()
    (result,) = batch.results()
    return result


def compare_runs(candidate: SimulationResult, reference: SimulationResult) -> list[Mismatch]:
    """Sample-by-sample comparison of two runs of a deterministic scenario:
    times and values must be identical."""
    out = _compare_keys(set(candidate.metrics), set(reference.metrics))
    for key in sorted(set(candidate.metrics) & set(reference.metrics)):
        got, want = candidate.metrics[key], reference.metrics[key]
        if len(got) != len(want):
            out.append(Mismatch(key, f"{len(got)} samples, the reference has {len(want)}"))
            continue
        bad = [
            (i, g, w)
            for i, (g, w) in enumerate(zip(got, want, strict=True))
            if not (g[0] == w[0] and g[1] == w[1])  # NaN never agrees
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


def metric_level(level: float, metrics: int) -> float:
    """The Bonferroni level of each of *metrics* intervals, so that all of
    them hold together with probability at least *level*."""
    return 1 - (1 - level) / metrics


def compare_batches(
    candidate: BatchResult,
    reference: BatchResult,
    level: float = LEVEL,
    max_rel_diff: float = MAX_REL_DIFF,
) -> list[Mismatch]:
    """Compare the per-run means of every metric of two batches of one
    stochastic scenario. *level* holds for the scenario as a whole."""
    keys_c = {k for r in candidate.results() for k in r.metrics}
    keys_r = {k for r in reference.results() for k in r.metrics}
    out = _compare_keys(keys_c, keys_r)
    common = sorted(keys_c & keys_r)
    per_metric = metric_level(level, len(common)) if common else level
    for key in common:
        a, b = candidate.scalar(key), reference.scalar(key)
        empty_a, empty_b = sum(map(math.isnan, a)), sum(map(math.isnan, b))
        if empty_a or empty_b:
            # A window metric of a run without any frames. The scenarios are
            # chosen so that this does not happen; if it does, say so.
            out.append(Mismatch(key, f"runs without samples: {empty_a} and {empty_b}"))
            continue
        ci = diff_ci(a, b, per_metric)
        mean_b = math.fsum(b) / len(b)
        if not ci.contains(0):
            out.append(
                Mismatch(
                    key,
                    f"difference of means {ci.diff:.6g}, {per_metric:.2%} interval"
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
