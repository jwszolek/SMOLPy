"""The SimPy engine runs the Scenario IR (ADR-0007, migration step 2)."""

from __future__ import annotations

import dataclasses

import pytest

from benchmarks.scenarios import SCENARIOS
from smolpy import Network
from smolpy.model import Observation, Scenario
from smolpy.sim.engine import run_simulation


def _cell() -> Network:
    """Every node and traffic kind, small enough for a quick run."""
    net = Network("cell")
    plc = net.adapter("plc", ip="10.0.0.1")
    host = net.adapter("host", ip="10.0.0.2")
    sw = net.switch("sw", ports=8, mac_table="topology")
    broker = net.mqtt_broker("broker", ip="10.0.1.1")
    slave = net.modbus_slave("slave", ip="10.0.2.1", unit_id=3)
    io = net.profinet_device("io", ip="10.0.3.1")
    for node in (plc, host, broker, slave, io):
        net.link(node, sw, speed=100, length=10)
    plc.controls(io, cycle=2, inputs=32, outputs=16)
    host.sends(plc, rate=2_000, size="imix", pattern="poisson")
    host.publishes(broker, topic="t", rate=50, qos=1)
    plc.polls(slave, register=0, count=8, rate=20)
    broker.routes("t", to=[plc])
    for metric, target in [
        ("throughput", plc),
        ("latency", plc),
        ("modbus_latency", plc),
        ("pn_latency", plc),
        ("pn_jitter", io),
        ("queue_depth", sw),
        ("broker_queue", broker),
        ("utilization", sw),
    ]:
        net.observe(metric, on=target, every=50)  # type: ignore[arg-type]
    return net


def test_result_carries_scenario_and_stats() -> None:
    net = _cell()
    result = net.simulate(duration=500, seed=3)
    assert result.scenario == net.to_scenario(duration=500, seed=3)
    assert result.stats is not None
    assert result.stats.backend == "simpy"
    assert result.stats.events > 0
    assert result.stats.wall_s > 0


def test_engine_needs_only_the_ir() -> None:
    """A scenario read back from JSON runs to exactly the same metrics."""
    net = _cell()
    direct = net.simulate(duration=500, seed=3)
    from_json = run_simulation(Scenario.from_json(net.to_scenario(500, 3).to_json()))
    assert from_json.metrics == direct.metrics
    assert from_json.stats is not None and direct.stats is not None
    assert from_json.stats.events == direct.stats.events
    assert all(direct.metrics.values())


@pytest.mark.parametrize("scenario", ["B1", "B2", "B3"])
def test_benchmark_scenarios_run_from_json(scenario: str) -> None:
    built = SCENARIOS[scenario](20_000, True)
    direct = built.network.simulate(duration=built.duration_ms)
    ir = Scenario.from_json(built.network.to_scenario(duration=built.duration_ms).to_json())
    assert run_simulation(ir).metrics == direct.metrics


def test_scenario_without_run_is_rejected() -> None:
    with pytest.raises(ValueError, match="no run configuration"):
        run_simulation(_cell().to_scenario())


def test_reference_to_an_undeclared_node_is_rejected() -> None:
    sc = _cell().to_scenario(duration=100)
    broken = dataclasses.replace(
        sc, observations=sc.observations + (Observation("latency", "nobody", 10),)
    )
    with pytest.raises(ValueError, match="undeclared node 'nobody'"):
        run_simulation(broken)


def test_export_names_the_scenario(tmp_path) -> None:
    import json

    path = tmp_path / "r.json"
    _cell().simulate(duration=200).export(str(path))
    assert json.loads(path.read_text())["network"] == "cell"
