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
