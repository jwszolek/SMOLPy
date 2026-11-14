"""run_many and BatchResult (ADR-0009, step 1)."""

from __future__ import annotations

import dataclasses

import pytest

from smolpy import BatchResult, Network, run_many
from smolpy.model import Flow, RunConfig
from smolpy.model.validate import ScenarioError


def _net() -> Network:
    net = Network("cell")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sw = net.switch("sw", ports=2)
    net.link(a, sw, speed=100, length=10)
    net.link(b, sw, speed=100, length=10)
    a.sends(b, rate=500, size="imix", pattern="poisson")
    net.observe("bytes_received", on=b, every=10)
    return net


def test_replications_use_consecutive_seeds_and_match_single_runs() -> None:
    net = _net()
    batch = run_many(net, runs=3, duration=100, workers=1)
    assert [r.seed for r in batch.runs] == [42, 43, 44]
    for run in batch.runs:
        assert run.ok and run.result is not None
        assert run.result.metrics == net.simulate(duration=100, seed=run.seed).metrics
        assert run.result.scenario.run == RunConfig(100, run.seed)
        assert run.result.stats is not None and run.result.stats.events > 0


def test_replications_differ() -> None:
    a, b = run_many(_net(), runs=2, duration=100, workers=1).results()
    assert a.metrics != b.metrics


def test_worker_processes_give_the_same_results() -> None:
    serial = run_many(_net(), runs=4, duration=100, workers=1)
    parallel = run_many(_net(), runs=4, duration=100, workers=2)
    assert [r.metrics for r in parallel.results()] == [r.metrics for r in serial.results()]


def test_explicit_seeds() -> None:
    batch = run_many(_net(), runs=2, seeds=[7, 1_000], duration=100, workers=1)
    assert [r.seed for r in batch.runs] == [7, 1_000]


def test_base_seed_comes_from_the_scenario() -> None:
    sc = _net().to_scenario(duration=100, seed=5)
    assert [r.seed for r in run_many(sc, runs=2, workers=1).runs] == [5, 6]


def test_sweep_runs_every_scenario_with_the_same_seeds() -> None:
    base = _net().to_scenario(duration=100)
    sweep = [
        dataclasses.replace(base, traffic=(dataclasses.replace(base.traffic[0], rate=r),))
        for r in (100, 1_000)
    ]
    batch = run_many(sweep, runs=2, workers=2)
    assert [(r.scenario, r.index, r.seed) for r in batch.runs] == [
        (0, 0, 42), (0, 1, 43), (1, 0, 42), (1, 1, 43),
    ]  # fmt: skip
    assert batch.scenarios == tuple(sweep)
    low, high = (batch.results(i)[0].metrics["bytes_received:b"][-1][1] for i in (0, 1))
    assert high > 5 * low
    flow = batch.results(1)[0].scenario.traffic[0]
    assert isinstance(flow, Flow) and flow.rate == 1_000


def test_duration_is_required_without_a_run_section() -> None:
    with pytest.raises(ValueError, match="has no run configuration; pass duration="):
        run_many(_net(), runs=1)
    with pytest.raises(ValueError, match="has no run configuration; pass duration="):
        run_many(_net().to_scenario(), runs=1)


def test_duration_overrides_the_scenario() -> None:
    sc = _net().to_scenario(duration=1_000, seed=9)
    (run,) = run_many(sc, duration=50, workers=1).runs
    assert run.result is not None and run.result.scenario.run == RunConfig(50, 9)
    assert run.result.metrics["bytes_received:b"][-1][0] == 40.0


def test_invalid_scenario_raises_before_any_run() -> None:
    sc = _net().to_scenario(duration=100)
    bad = dataclasses.replace(sc, links=sc.links[:1])  # b has traffic but no link
    with pytest.raises(ScenarioError, match="'b' has no link"):
        run_many([sc, bad], runs=2)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"runs": 0}, "runs must be a positive integer, got 0"),
        ({"runs": True}, "runs must be a positive integer, got True"),
        ({"workers": 0}, "workers must be a positive integer, got 0"),
        ({"runs": 2, "seeds": [1]}, "1 seeds given for 2 runs"),
        ({"runs": 1, "seeds": [1.5]}, "seeds must be integers"),
        ({"backend": "rust"}, "unknown backend 'rust', expected one of simpy"),
    ],
)
def test_bad_arguments(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run_many(_net(), duration=100, **kwargs)


def test_bad_source() -> None:
    with pytest.raises(TypeError, match="expected Network, Scenario or a list of Scenarios"):
        run_many("plant.smol")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one scenario"):
        run_many([])


def test_a_failing_run_is_recorded_and_the_others_go_on(monkeypatch) -> None:
    from smolpy.sim import engine

    real = engine.run_simulation

    def flaky(scenario, **kwargs):
        if scenario.run.seed == 43:
            raise RuntimeError("boom")
        return real(scenario, **kwargs)

    monkeypatch.setattr(engine, "run_simulation", flaky)
    batch = run_many(_net(), runs=3, duration=100, workers=1)
    assert [r.ok for r in batch.runs] == [True, False, True]
    (failed,) = batch.failures
    assert failed.seed == 43 and failed.result is None
    assert "RuntimeError: boom" in (failed.error or "")
    assert len(batch.results()) == 2
    with pytest.raises(RuntimeError, match=r"1 of 3 runs failed\nscenario 0 run 1 \(seed 43\)"):
        batch.raise_on_failure()


def test_no_failures() -> None:
    batch = run_many(_net(), duration=100, workers=1)
    assert isinstance(batch, BatchResult) and batch.failures == []
    batch.raise_on_failure()


def test_scripts_need_no_main_guard(tmp_path) -> None:
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "plant.py"
    script.write_text(
        textwrap.dedent(
            """
            from smolpy import Network, run_many
            net = Network("cell")
            a = net.adapter("a", ip="10.0.0.1")
            b = net.adapter("b", ip="10.0.0.2")
            sw = net.switch("sw", ports=2)
            net.link(a, sw, speed=100, length=10)
            net.link(b, sw, speed=100, length=10)
            a.sends(b, rate=500, pattern="poisson")
            net.observe("bytes_received", on=b, every=10)
            batch = run_many(net, runs=3, duration=50, workers=2)
            batch.raise_on_failure()
            print("runs", len(batch.results()))
            """
        )
    )
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("runs 3") == 1  # the script ran once, not once per worker


def test_a_dead_worker_is_recorded_and_replaced() -> None:
    from smolpy.batch import _Worker

    text = _net().to_scenario(duration=50).to_json()
    worker = _Worker()
    try:
        worker._proc.kill()
        worker._proc.wait()
        outcome = worker.run(0, text, 42)
        assert isinstance(outcome, str) and outcome.startswith("worker process died")
        metrics, stats = worker.run(0, text, 42)  # a fresh process takes over
        assert stats.events > 0 and metrics["bytes_received:b"]
    finally:
        worker.close()


# --- step 2: summary, scalar, export ---


def _sparse_net() -> Network:
    """Few frames, short windows: some latency windows are empty in some runs."""
    net = Network("sparse")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sw = net.switch("sw", ports=2)
    net.link(a, sw, speed=100, length=10)
    net.link(b, sw, speed=100, length=10)
    a.sends(b, rate=100, size="imix", pattern="poisson")
    net.observe("latency", on=b, every=10)
    net.observe("bytes_received", on=b, every=10)
    return net


def test_summary_is_the_mean_ci_of_every_window() -> None:
    from smolpy.stats import mean_ci

    batch = run_many(_net(), runs=5, duration=100, workers=1)
    summary = batch.summary()
    assert list(summary) == ["bytes_received:b"]
    runs = [r.metrics["bytes_received:b"] for r in batch.results()]
    assert [t for t, _ in summary["bytes_received:b"]] == [t for t, _ in runs[0]]
    for w, (t, ci) in enumerate(summary["bytes_received:b"]):
        assert ci == mean_ci([run[w][1] for run in runs])
        assert ci.n == 5


def test_summary_counts_the_runs_that_have_a_window() -> None:
    batch = run_many(_sparse_net(), runs=6, duration=300, workers=1)
    latency = batch.summary()["latency:b"]
    per_run = [{t for t, _ in r.metrics["latency:b"]} for r in batch.results()]
    for t, ci in latency:
        assert ci.n == sum(t in times for times in per_run)
    assert min(ci.n for _, ci in latency) < 6  # some windows are empty in some runs


def test_summary_of_another_scenario_and_level() -> None:
    base = _net().to_scenario(duration=100)
    other = dataclasses.replace(base, traffic=(dataclasses.replace(base.traffic[0], rate=50),))
    batch = run_many([base, other], runs=3, workers=1)
    assert batch.summary(1) != batch.summary(0)
    t, ci = batch.summary(1, level=0.9)["bytes_received:b"][-1]
    assert ci.level == 0.9
    with pytest.raises(IndexError, match="scenario 2 out of range: the batch has 2"):
        batch.summary(2)


def test_scalar() -> None:
    batch = run_many(_net(), runs=3, duration=100, workers=1)
    series = [[v for _, v in r.metrics["bytes_received:b"]] for r in batch.results()]
    assert batch.scalar("bytes_received:b", "last") == [s[-1] for s in series]
    assert batch.scalar("bytes_received:b", "max") == [max(s) for s in series]
    assert batch.scalar("bytes_received:b", "min") == [min(s) for s in series]
    assert batch.scalar("bytes_received:b") == pytest.approx([sum(s) / len(s) for s in series])
    assert batch.scalar("bytes_received:b", lambda vs: len(vs)) == [9.0, 9.0, 9.0]


def test_scalar_of_a_run_without_samples_is_nan() -> None:
    import math

    net = Network("late")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sw = net.switch("sw", ports=2)
    net.link(a, sw, speed=100, length=10)
    net.link(b, sw, speed=100, length=10)
    a.sends(b, rate=10)  # first frame at 100 ms, after the run
    net.observe("latency", on=b, every=10)
    batch = run_many(net, runs=2, duration=50, workers=1)

    def never(values: list[float]) -> float:
        raise AssertionError("called without samples")

    assert all(math.isnan(v) for v in batch.scalar("latency:b", never))
    assert len(batch.scalar("latency:b")) == 2


def test_scalar_rejects() -> None:
    batch = run_many(_net(), runs=1, duration=100, workers=1)
    with pytest.raises(KeyError, match="no metric 'latency:b' in the runs; observed: bytes_"):
        batch.scalar("latency:b")
    with pytest.raises(ValueError, match="unknown reduce 'median', expected one of mean"):
        batch.scalar("bytes_received:b", "median")


def test_export_csv_and_json(tmp_path, monkeypatch) -> None:
    import csv
    import json

    from smolpy.sim import engine

    real = engine.run_simulation

    def flaky(scenario, **kwargs):
        if scenario.run.seed == 43:
            raise RuntimeError("boom")
        return real(scenario, **kwargs)

    monkeypatch.setattr(engine, "run_simulation", flaky)
    batch = run_many(_net(), runs=2, duration=30, workers=1)

    batch.export(tmp_path / "runs.csv")
    rows = list(csv.DictReader(open(tmp_path / "runs.csv", encoding="utf-8")))
    ok = [r for r in rows if r["run"] == "0"]
    expected = batch.runs[0].result.metrics["bytes_received:b"]  # type: ignore[union-attr]
    assert [(float(r["time_ms"]), float(r["value"])) for r in ok] == expected
    assert {r["seed"] for r in ok} == {"42"} and {r["metric"] for r in ok} == {"bytes_received:b"}
    (failed,) = [r for r in rows if r["run"] == "1"]
    assert failed["seed"] == "43" and failed["error"] == "RuntimeError: boom"
    assert failed["time_ms"] == failed["metric"] == failed["value"] == ""

    batch.export(str(tmp_path / "runs.out"), format="json")
    data = json.loads((tmp_path / "runs.out").read_text(encoding="utf-8"))
    assert data["scenarios"] == ["cell"]
    first, second = data["runs"]
    assert (first["seed"], first["error"]) == (42, None)
    assert first["metrics"]["bytes_received:b"] == [list(s) for s in expected]
    assert second["metrics"] is None and "RuntimeError: boom" in second["error"]

    with pytest.raises(ValueError, match="unknown format 'xlsx', expected 'csv' or 'json'"):
        batch.export(tmp_path / "runs.xlsx")
