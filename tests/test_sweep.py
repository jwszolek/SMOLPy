"""Sweep helpers on Scenario (ADR-0009, step 4)."""

from __future__ import annotations

import pytest

from smolpy import Network, run_many
from smolpy.model import Flow, Poll, ProfinetAR, RunConfig, Scenario
from smolpy.model.streams import source_ids
from smolpy.model.validate import ScenarioError


def _scenario() -> Scenario:
    net = Network("cell")
    sw = net.switch("sw", ports=5)
    plc = net.adapter("plc", ip="10.0.0.1")
    io = net.profinet_device("io", ip="10.0.0.2")
    bulk = net.adapter("bulk", ip="10.0.0.3")
    srv = net.adapter("srv", ip="10.0.0.4")
    meter = net.modbus_slave("meter", ip="10.0.0.5", unit_id=1)
    for node in (plc, io, bulk, srv, meter):
        net.link(node, sw, speed=100, length=10)
    plc.controls(io, cycle=4, inputs=20, outputs=20)
    plc.polls(meter, register=0, count=4, rate=10)
    bulk.sends(srv, rate=1000, size=1500, pattern="poisson")
    bulk.sends(plc, rate=50, size=200)
    net.observe("bytes_received", on=srv, every=20)
    return net.to_scenario(duration=100, seed=3)


def _flows(sc: Scenario) -> dict[str, float]:
    return {t.destination: t.rate for t in sc.traffic if isinstance(t, Flow)}


def test_with_flow_rate_changes_the_matching_flows_only() -> None:
    base = _scenario()
    sc = base.with_flow_rate(250, destination="srv")
    assert _flows(sc) == {"srv": 250, "plc": 50}
    assert _flows(base) == {"srv": 1000, "plc": 50}  # a copy, the base is unchanged
    assert _flows(base.with_flow_rate(7, source="bulk")) == {"srv": 7, "plc": 7}
    assert [type(t) for t in sc.traffic] == [type(t) for t in base.traffic]  # order kept


def test_a_selection_that_matches_nothing_is_an_error() -> None:
    with pytest.raises(
        ValueError,
        match=r"no flow matches \{'destination': 'svr'\} in scenario 'cell'; "
        r"there are: bulk -> srv, bulk -> plc",
    ):
        _scenario().with_flow_rate(1, destination="svr")
    with pytest.raises(ValueError, match="no publish matches .*; it has no publish"):
        _scenario().with_traffic("publish", {"topic": "t"}, rate=1)


def test_with_traffic_rejects() -> None:
    sc = _scenario()
    with pytest.raises(ValueError, match="unknown traffic kind 'flows', expected one of flow,"):
        sc.with_traffic("flows", rate=1)
    with pytest.raises(ValueError, match=r"flow has no field 'speed' \(in changes\); fields:"):
        sc.with_traffic("flow", speed=1)
    with pytest.raises(ValueError, match=r"poll has no field 'device' \(in where\)"):
        sc.with_traffic("poll", {"device": "io"}, rate=1)
    with pytest.raises(ValueError, match="at least one field to change"):
        sc.with_traffic("flow", {"source": "bulk"})


def test_with_traffic_and_with_cycle() -> None:
    sc = _scenario().with_traffic("poll", {"slave": "meter"}, rate=20, count=8)
    (poll,) = [t for t in sc.traffic if isinstance(t, Poll)]
    assert (poll.rate, poll.count) == (20, 8)
    sc = _scenario().with_cycle(16, device="io")
    (ar,) = [t for t in sc.traffic if isinstance(t, ProfinetAR)]
    assert ar.cycle_ms == 16
    with pytest.raises(ValueError, match="no profinet_ar matches .*there are: plc -> io"):
        _scenario().with_cycle(16, controller="io")


def test_with_link_speed() -> None:
    base = _scenario()
    sc = base.with_link_speed(1000, between=("sw", "srv"))  # either order
    assert {(lk.a, lk.speed_mbps) for lk in sc.links} == {
        ("plc", 100),
        ("io", 100),
        ("bulk", 100),
        ("srv", 1000),
        ("meter", 100),
    }
    assert {lk.speed_mbps for lk in base.with_link_speed(10).links} == {10}
    with pytest.raises(ValueError, match="no link between 'srv' and 'plc' in scenario 'cell'"):
        base.with_link_speed(10, between=("srv", "plc"))


def test_with_run() -> None:
    sc = _scenario()
    assert sc.with_run(seed=9).run == RunConfig(100, 9)
    assert sc.with_run(500).run == RunConfig(500, 3)
    bare = Scenario("bare")
    assert bare.with_run(200).run == RunConfig(200, 42)
    with pytest.raises(ValueError, match="has no run configuration; pass duration_ms"):
        bare.with_run(seed=1)
    with pytest.raises(ValueError, match="needs duration_ms or seed"):
        sc.with_run()


def test_a_sweep_keeps_every_random_stream() -> None:
    base = _scenario()
    sweep = [base.with_flow_rate(r, destination="srv") for r in (200, 400, 800)]
    assert all(source_ids(sc) == source_ids(base) for sc in sweep)


def test_sweep_with_run_many(capsys) -> None:
    base = _scenario()
    sweep = [base.with_flow_rate(r, destination="srv") for r in (200, 400, 800)]
    batch = run_many(sweep, runs=2, workers=1)
    totals = [sum(batch.scalar("bytes_received:srv", "sum", scenario=i)) for i in range(3)]
    assert totals[0] < totals[1] < totals[2]
    batch.report()
    out = capsys.readouterr().out
    assert "scenario 2 (cell): 2 of 2 runs succeeded" in out


def test_an_invalid_value_is_rejected_before_anything_runs() -> None:
    with pytest.raises(ScenarioError, match="rate"):
        run_many(_scenario().with_flow_rate(-5, destination="srv"), runs=2, workers=1)
