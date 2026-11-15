"""Every installed backend against SimPy, the reference (ADR-0009, step 5).

With SimPy alone, SimPy is compared against itself: deterministic scenarios
must agree exactly, and stochastic ones, run with independent seeds, must
pass the statistical comparison. The tests at the end check that the harness
does detect differences.
"""

from __future__ import annotations

import dataclasses
from functools import cache

import pytest

from smolpy.batch import BatchResult
from smolpy.core.network import SimulationResult
from smolpy.model import Flow
from smolpy.stats import diff_ci, mean_ci

from . import harness
from .scenarios import DETERMINISTIC, STOCHASTIC

BACKENDS = [
    pytest.param(
        b, marks=pytest.mark.skipif(not harness.installed(b), reason=f"backend {b!r} not installed")
    )
    for b in harness.KNOWN_BACKENDS
]


@cache
def _reference_run(name: str) -> SimulationResult:
    return harness.run_once(DETERMINISTIC[name](), harness.REFERENCE)


@cache
def _reference_batch(name: str) -> BatchResult:
    return harness.run_replications(STOCHASTIC[name](), harness.REFERENCE)


# --- the suite ---


def _supported(scenario, backend: str):
    """Skip, naming the reason, a scenario the backend cannot run yet."""
    from smolpy import backends
    from smolpy.model.validate import ScenarioError

    try:
        backends.check(scenario, backend)
    except ScenarioError as exc:
        pytest.skip(f"outside the {backend} backend's capabilities: {exc}")
    return scenario


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", DETERMINISTIC)
def test_deterministic(name: str, backend: str) -> None:
    candidate = harness.run_once(_supported(DETERMINISTIC[name](), backend), backend)
    mismatches = harness.compare_runs(candidate, _reference_run(name))
    assert not mismatches, "\n".join(map(str, mismatches))


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", STOCHASTIC)
def test_stochastic(name: str, backend: str) -> None:
    candidate = harness.run_replications(
        _supported(STOCHASTIC[name](), backend), backend, seed_offset=harness.CANDIDATE_SEED_OFFSET
    )
    mismatches = harness.compare_batches(candidate, _reference_batch(name))
    assert not mismatches, "\n".join(map(str, mismatches))


# --- the scenarios are what they claim to be ---


@pytest.mark.parametrize("name", DETERMINISTIC)
def test_deterministic_scenarios_do_not_depend_on_the_seed(name: str) -> None:
    other = harness.run_once(DETERMINISTIC[name]().with_run(seed=999), harness.REFERENCE)
    assert other.metrics == _reference_run(name).metrics
    assert all(other.metrics.values()), "every metric has samples"


@pytest.mark.parametrize("name", STOCHASTIC)
def test_stochastic_metrics_are_precise_enough(name: str) -> None:
    batch = _reference_batch(name)
    assert batch.results()[0].metrics != batch.results()[1].metrics, "runs differ"
    for key in batch.results()[0].metrics:
        ci = mean_ci(batch.scalar(key))
        rel_se = ci.std / ci.mean / ci.n**0.5
        assert rel_se < harness.MAX_REL_DIFF / 3, f"{key}: standard error {rel_se:.2%} of the mean"


def test_every_metric_is_covered() -> None:
    from smolpy.model.validate import METRIC_TARGETS

    observed = {
        o.metric
        for make in (*DETERMINISTIC.values(), *STOCHASTIC.values())
        for o in make().observations
    }
    assert observed == set(METRIC_TARGETS)


# --- the harness detects differences ---


def _changed(result: SimulationResult, key: str, change) -> SimulationResult:
    metrics = dict(result.metrics)
    metrics[key] = [(t, change(v)) if i == 3 else (t, v) for i, (t, v) in enumerate(metrics[key])]
    return SimulationResult(metrics, result.scenario, result.stats)


def test_compare_runs_has_no_tolerance() -> None:
    import math

    ref = _reference_run("line")
    assert harness.compare_runs(_changed(ref, "latency:b", lambda v: v), ref) == []
    # the smallest possible change of one value, one ulp, is a mismatch
    (m,) = harness.compare_runs(
        _changed(ref, "latency:b", lambda v: math.nextafter(v, math.inf)), ref
    )
    assert m.metric == "latency:b" and m.detail.startswith(
        "1 of 11 samples differ, first at index 3"
    )
    (m,) = harness.compare_runs(_changed(ref, "throughput:b", lambda v: v * (1 + 1e-12)), ref)
    assert m.metric == "throughput:b"
    (m,) = harness.compare_runs(_changed(ref, "latency:b", lambda v: math.nan), ref)
    assert m.metric == "latency:b"


def test_compare_runs_reports_missing_and_shifted_samples() -> None:
    ref = _reference_run("line")
    metrics = dict(ref.metrics)
    del metrics["utilization:sw"]
    metrics["extra:b"] = []
    metrics["latency:b"] = metrics["latency:b"][1:]
    metrics["bytes_sent:a"] = [(t + 1e-6, v) for t, v in metrics["bytes_sent:a"]]  # 1 ns later
    got = harness.compare_runs(SimulationResult(metrics, ref.scenario), ref)
    assert [str(m) for m in got] == [
        "utilization:sw: missing from the candidate",
        "extra:b: not in the reference",
        "bytes_sent:a: 11 of 11 samples differ, first at index 0:"
        f" {metrics['bytes_sent:a'][0]} against {ref.metrics['bytes_sent:a'][0]}",
        "latency:b: 10 samples, the reference has 11",
    ]


def test_compare_runs_detects_a_changed_model() -> None:
    sc = DETERMINISTIC["line"]()
    slower = sc.with_link_speed(90, between=("a", "sw"))
    got = harness.compare_runs(harness.run_once(slower, "simpy"), _reference_run("line"))
    assert "latency:b" in {m.metric for m in got}


def test_compare_batches_detects_a_two_percent_change() -> None:
    sc = STOCHASTIC["random-load"]()
    traffic = tuple(
        dataclasses.replace(t, rate=t.rate * 1.03) if isinstance(t, Flow) else t for t in sc.traffic
    )
    loaded = harness.run_replications(
        dataclasses.replace(sc, traffic=traffic),
        "simpy",
        seed_offset=harness.CANDIDATE_SEED_OFFSET,
    )
    got = {m.metric for m in harness.compare_batches(loaded, _reference_batch("random-load"))}
    assert {"throughput:srv", "bytes_received:srv"} <= got


def test_each_metric_gets_a_bonferroni_level() -> None:
    assert harness.metric_level(0.95, 1) == 0.95
    assert harness.metric_level(0.95, 4) == pytest.approx(0.9875)
    batch = _reference_batch("random-load")
    a, b = batch.scalar("latency:srv")[:15], batch.scalar("latency:srv")[15:]
    assert diff_ci(a, b, 0.9875).high - diff_ci(a, b, 0.9875).low > (
        diff_ci(a, b, 0.95).high - diff_ci(a, b, 0.95).low
    )
