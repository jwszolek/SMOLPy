"""smolpy.model.validate: every semantic rule in one place (ADR-0007, step 3)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from smolpy import Network
from smolpy.core.batch import BatchSpec
from smolpy.lang import SMOLSemanticError, interpret_and_run
from smolpy.model import (
    AdapterNode,
    Flow,
    HubNode,
    Link,
    ModbusSlaveNode,
    MqttBrokerNode,
    Observation,
    Poll,
    ProfinetAR,
    ProfinetDeviceNode,
    Publish,
    Route,
    RunConfig,
    Scenario,
    SwitchNode,
)
from smolpy.model.validate import ScenarioError, validate
from smolpy.sim.engine import run_simulation


def _base() -> Scenario:
    """A valid scenario: every node kind on one switch, every traffic kind."""
    return Scenario(
        name="cell",
        nodes=(
            AdapterNode("plc", "10.0.0.1", "02:00:0a:00:00:01"),
            AdapterNode("host", "10.0.0.2", "02:00:0a:00:00:02"),
            SwitchNode("sw", 8),
            HubNode("hub", 2),
            MqttBrokerNode("broker", "10.0.1.1", "02:00:0a:00:01:01"),
            ModbusSlaveNode("slave", "10.0.2.1", "02:00:0a:00:02:01", 1),
            ProfinetDeviceNode("io", "10.0.3.1", "02:00:0a:00:03:01"),
        ),
        links=(
            Link("plc", "sw", 100, 10),
            Link("host", "sw", 100, 10),
            Link("broker", "sw", 100, 10),
            Link("slave", "sw", 100, 10),
            Link("io", "sw", 100, 10),
            Link("hub", "sw", 10, 10),
        ),
        traffic=(
            ProfinetAR("plc", "io", 4, 32, 16),
            Flow("host", "plc", 100),
            Publish("host", "broker", "t"),
            Poll("plc", "slave", 0, 4),
            Route("broker", "t", ("plc",)),
        ),
        observations=(Observation("latency", "plc", 10), Observation("queue_depth", "sw", 10)),
        run=RunConfig(100),
    )


def _with(section: str, index: int | None = None, **changes) -> Scenario:
    """_base() with one element changed (index given) or a section replaced."""
    sc = _base()
    if index is None:
        return dataclasses.replace(sc, **{section: changes["value"]})
    items = list(getattr(sc, section))
    items[index] = dataclasses.replace(items[index], **changes)
    return dataclasses.replace(sc, **{section: tuple(items)})


def _plus(section: str, *extra) -> Scenario:
    sc = _base()
    return dataclasses.replace(sc, **{section: getattr(sc, section) + extra})


def test_base_scenario_is_valid() -> None:
    validate(_base())
    run_simulation(_base())


INVALID = [
    # nodes
    (_with("nodes", 0, ip="10.0.0"), "'10.0.0' is not an IPv4 address", ("nodes", 0)),
    (_with("nodes", 0, mac="02:00:00:00:01"), "must be six hex bytes", ("nodes", 0)),
    (_with("nodes", 1, mac="02:00:0A:00:00:01"), "already used by 'plc'", ("nodes", 1)),
    (_with("nodes", 1, name="plc"), "'plc' is declared twice", ("nodes", 1)),
    (_with("nodes", 2, ports=0), "ports must be a positive integer", ("nodes", 2)),
    (_with("nodes", 2, qos="wfq"), "invalid qos 'wfq'", ("nodes", 2)),
    (_with("nodes", 2, mac_table="static"), "invalid mac_table 'static'", ("nodes", 2)),
    (_with("nodes", 2, mode="hybrid"), "invalid mode 'hybrid'", ("nodes", 2)),
    (_with("nodes", 5, unit_id=256), "unit_id 256 out of range 0-255", ("nodes", 5)),
    # links
    (_plus("links", Link("plc", "plc", 100, 1)), "linked to itself", ("links", 6)),
    (_with("links", 0, speed_mbps=0), "speed must be a positive number", ("links", 0)),
    (_with("links", 0, length_m=-1), "length must be zero or positive", ("links", 0)),
    (_with("links", 0, b="nowhere"), "undeclared node 'nowhere'", ("links", 0)),
    (
        _plus("links", Link("plc", "hub", 10, 1)),
        "'plc' is an end station and already has a link",
        ("links", 6),
    ),
    (_with("nodes", 2, ports=5), "'sw' has only 5 ports, all in use", ("links", 5)),
    (_plus("links", Link("host", "hub", 10, 1)), "already has a link", ("links", 6)),
    # traffic
    (
        _with("traffic", 1, priority=9),
        "flow host -> plc: priority 9 out of range 0-7",
        ("traffic", 1),
    ),
    (_with("traffic", 1, rate=0), "rate must be a positive number", ("traffic", 1)),
    (_with("traffic", 1, size=0), "size must be a positive number of bytes", ("traffic", 1)),
    (_with("traffic", 1, pattern="burst"), "invalid pattern 'burst'", ("traffic", 1)),
    (_with("traffic", 1, pattern="batch"), "needs a batch size", ("traffic", 1)),
    (
        _with("traffic", 1, batch=BatchSpec("geom", 2)),
        "only meaningful with pattern 'batch'",
        ("traffic", 1),
    ),
    (
        _with("traffic", 1, pattern="batch", batch=BatchSpec("pareto", 1.0)),
        "shape must be > 1",
        ("traffic", 1),
    ),
    (_with("traffic", 1, delay_ms=-5), "delay must be zero or positive", ("traffic", 1)),
    (
        _with("traffic", 1, destination="host"),
        "source and destination are the same",
        ("traffic", 1),
    ),
    (_with("traffic", 1, destination="sw"), "'sw' is a Switch, expected Adapter", ("traffic", 1)),
    (_with("traffic", 2, qos=2), "invalid qos 2", ("traffic", 2)),
    (_with("traffic", 2, topic=""), "topic must be a non-empty string", ("traffic", 2)),
    (_with("traffic", 2, broker="slave"), "expected MQTTBroker", ("traffic", 2)),
    (_with("traffic", 3, count=126), "count 126 out of range 1-125", ("traffic", 3)),
    (_with("traffic", 3, register=70_000), "register 70000 out of range", ("traffic", 3)),
    (
        _with("traffic", 3, slave="io"),
        "'io' is a ProfinetDevice, expected ModbusSlave",
        ("traffic", 3),
    ),
    (_with("traffic", 0, cycle_ms=3), "cycle 3 ms must be one of", ("traffic", 0)),
    (_with("traffic", 0, inputs=0), "inputs 0 out of range 1-1440", ("traffic", 0)),
    (_with("traffic", 0, outputs=1441), "outputs 1441 out of range 1-1440", ("traffic", 0)),
    (_with("traffic", 0, hold=0), "hold 0 out of range 1-255", ("traffic", 0)),
    (
        _plus("traffic", ProfinetAR("plc", "io", 8, 8, 8)),
        "'plc' already controls 'io'",
        ("traffic", 5),
    ),
    (_with("traffic", 4, subscribers=("plc", "plc")), "'plc' listed twice", ("traffic", 4)),
    (_plus("traffic", Route("broker", "t", ("host",))), "topic is routed twice", ("traffic", 5)),
    (_with("traffic", 4, subscribers=("hub",)), "'hub' is a Hub, expected Adapter", ("traffic", 4)),
    # observations
    (
        _with("observations", 0, metric="frame_loss"),
        "unknown or unsupported metric 'frame_loss'",
        ("observations", 0),
    ),
    (
        _with("observations", 0, metric="pn_jitter", target="sw"),
        "'sw' is a Switch, expected Adapter or ProfinetDevice",
        ("observations", 0),
    ),
    (_with("observations", 0, every_ms=0), "every must be a positive number", ("observations", 0)),
    (_with("observations", 0, target="ghost"), "undeclared node 'ghost'", ("observations", 0)),
    (
        _plus("observations", Observation("latency", "plc", 5)),
        "observed twice",
        ("observations", 2),
    ),
    # run
    (_with("run", value=RunConfig(0)), "duration must be a positive number", ("run", 0)),
    (_with("run", value=RunConfig(10, seed=1.5)), "seed must be an integer", ("run", 0)),  # type: ignore[arg-type]
]


@pytest.mark.parametrize("scenario, message, where", INVALID, ids=[m for _, m, _ in INVALID])
def test_rule(scenario: Scenario, message: str, where: tuple[str, int]) -> None:
    with pytest.raises(ScenarioError) as caught:
        validate(scenario)
    assert message in str(caught.value)
    assert caught.value.where == where


def test_isolated_node_without_traffic_is_allowed() -> None:
    validate(_plus("nodes", AdapterNode("spare", "10.0.9.9", "02:00:0a:00:09:09")))


def test_node_with_traffic_needs_a_link() -> None:
    sc = _plus("nodes", AdapterNode("spare", "10.0.9.9", "02:00:0a:00:09:09"))
    sc = dataclasses.replace(sc, traffic=sc.traffic + (Flow("spare", "plc", 10),))
    with pytest.raises(ScenarioError, match="'spare' has no link"):
        validate(sc)


def test_observed_node_needs_a_link() -> None:
    sc = _plus("nodes", AdapterNode("spare", "10.0.9.9", "02:00:0a:00:09:09"))
    sc = dataclasses.replace(sc, observations=(Observation("utilization", "spare", 10),))
    with pytest.raises(ScenarioError, match="'spare' has no link, so there is nothing to observe"):
        validate(sc)


def test_traffic_between_disconnected_parts_is_rejected() -> None:
    sc = _plus(
        "nodes",
        AdapterNode("x", "10.0.9.1", "02:00:0a:00:09:01"),
        AdapterNode("y", "10.0.9.2", "02:00:0a:00:09:02"),
    )
    sc = dataclasses.replace(
        sc, links=sc.links + (Link("x", "y", 100, 1),), traffic=(Flow("x", "plc", 10),)
    )
    with pytest.raises(ScenarioError, match="'x' and 'plc' are not connected"):
        validate(sc)


def test_loops_are_rejected() -> None:
    sc = _plus("nodes", SwitchNode("sw2", 4))
    sc = dataclasses.replace(
        sc, links=sc.links + (Link("sw", "sw2", 100, 1), Link("sw2", "hub", 100, 1))
    )
    with pytest.raises(ScenarioError, match="link sw2 -- hub: closes a loop"):
        validate(sc)


def test_half_duplex_is_not_supported_by_simpy() -> None:
    sc = _with("links", 5, duplex=False)
    validate(sc)  # a valid model ...
    with pytest.raises(ScenarioError, match="half-duplex links are not supported by the simpy"):
        run_simulation(sc)  # ... that this backend cannot run


def test_invalid_json_scenario_never_runs() -> None:
    text = _with("traffic", 1, priority=8).to_json()
    with pytest.raises(ScenarioError, match="priority 8"):
        run_simulation(Scenario.from_json(text))


# --- Python API: errors at the call that made them ---


def _net() -> tuple[Network, object, object, object]:
    net = Network("t")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sw = net.switch("sw", ports=2)
    return net, a, b, sw


def test_python_api_rejects_metric_on_wrong_node_type() -> None:
    net, a, b, sw = _net()
    with pytest.raises(ValueError, match="observe pn_jitter on sw: 'sw' is a Switch"):
        net.observe("pn_jitter", on=sw, every=10)  # previously ignored silently


def test_python_api_rejects_unsupported_metric() -> None:
    net, a, *_ = _net()
    with pytest.raises(ValueError, match="unknown or unsupported metric 'frame_loss'"):
        net.observe("frame_loss", on=a, every=10)


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda net, a, b, sw: a.sends(sw, rate=10), "expected Adapter"),
        (lambda net, a, b, sw: a.sends(b, rate=-1), "rate must be a positive number"),
        (lambda net, a, b, sw: a.publishes(b, topic="t"), "expected MQTTBroker"),
        (lambda net, a, b, sw: a.polls(b, register=0, count=1), "expected ModbusSlave"),
        (lambda net, a, b, sw: net.link(a, a, speed=100, length=1), "linked to itself"),
        (lambda net, a, b, sw: net.switch("s2", ports=0), "ports must be a positive"),
        (lambda net, a, b, sw: net.modbus_slave("m", ip="10.0.0.9", unit_id=300), "unit_id 300"),
        (lambda net, a, b, sw: net.adapter("c", ip="10.0.0.3", mac="zz"), "six hex bytes"),
    ],
)  # fmt: skip
def test_python_api_rejects_at_the_call(call, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        call(*_net())


def test_python_api_checks_the_whole_scenario_before_running() -> None:
    net, a, b, sw = _net()
    c = net.adapter("c", ip="10.0.0.3")
    for node in (a, b, c):
        net.link(node, sw, speed=100, length=1)  # 3 links on a 2-port switch
    with pytest.raises(ValueError, match="'sw' has only 2 ports, all in use"):
        net.simulate(duration=10)


# --- .smol: the same rules, with the source line ---


def _smol(tmp_path: Path, body: str) -> None:
    f = tmp_path / "t.smol"
    f.write_text(f'network "t" {{\n{body}\n    simulate duration=100 mode=headless\n}}\n')
    interpret_and_run(f)


def test_smol_reports_whole_scenario_errors_with_the_line(tmp_path: Path) -> None:
    body = """    adapter a ip=10.0.0.1
    adapter b ip=10.0.0.2
    adapter c ip=10.0.0.3
    switch sw ports=2
    link a -- sw speed=100 length=1
    link b -- sw speed=100 length=1
    link c -- sw speed=100 length=1"""
    with pytest.raises(SMOLSemanticError, match="'sw' has only 2 ports") as caught:
        _smol(tmp_path, body)
    assert caught.value.line == 8


def test_smol_maps_traffic_errors_to_their_statement(tmp_path: Path) -> None:
    body = """    adapter a ip=10.0.0.1
    adapter b ip=10.0.0.2
    adapter c ip=10.0.0.3
    switch sw ports=4
    link a -- sw speed=100 length=1
    link b -- sw speed=100 length=1
    flow a -> b rate=10
    flow a -> c rate=10"""
    with pytest.raises(SMOLSemanticError, match="flow a -> c: 'c' has no link") as caught:
        _smol(tmp_path, body)
    assert caught.value.line == 9


def test_smol_rejects_metric_on_wrong_node_type(tmp_path: Path) -> None:
    body = """    adapter a ip=10.0.0.1
    switch sw ports=4
    link a -- sw speed=100 length=1
    observe pn_jitter on sw every=10"""
    with pytest.raises(SMOLSemanticError, match="expected Adapter or ProfinetDevice") as caught:
        _smol(tmp_path, body)
    assert caught.value.line == 5
