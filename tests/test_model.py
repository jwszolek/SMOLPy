"""Scenario IR (ADR-0007, migration step 1): Network -> Scenario -> JSON -> Scenario."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from benchmarks.scenarios import SCENARIOS
from smolpy import Network
from smolpy.core.batch import BatchSpec
from smolpy.lang import interpret_to_scenario
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

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class _Captured(Exception):
    def __init__(self, network: Network, duration: float, seed: int) -> None:
        self.network, self.duration, self.seed = network, duration, seed


def _example_scenario(path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    """Run an example script up to its simulate() call and return its scenario."""

    def capture(self: Network, duration: float, *, seed: int = 42, **_: object) -> None:
        raise _Captured(self, duration, seed)

    monkeypatch.setattr(Network, "simulate", capture)
    with pytest.raises(_Captured) as caught:
        runpy.run_path(str(path), run_name="__main__")
    c = caught.value
    return c.network.to_scenario(duration=c.duration, seed=c.seed)


def _round_trip(scenario: Scenario) -> None:
    text = scenario.to_json()
    again = Scenario.from_json(text)
    assert again == scenario
    assert again.to_json() == text


def _full_network() -> Network:
    """One of every node kind and traffic kind, with non-default options."""
    net = Network("everything")
    plc = net.adapter("plc", ip="10.0.0.1")
    host = net.adapter("host", ip="10.0.0.2", mac="02:aa:bb:cc:dd:ee")
    sw = net.switch("sw", ports=8, mode="cut-through", qos="fifo", mac_table="topology")
    hub = net.hub("hub", ports=4)
    broker = net.mqtt_broker("broker", ip="10.0.1.1")
    slave = net.modbus_slave("slave", ip="10.0.2.1", unit_id=7)
    io = net.profinet_device("io", ip="10.0.3.1")
    for node in (plc, host, broker, slave, io):
        net.link(node, sw, speed=100, length=10)
    net.link(hub, sw, speed=10, length=100, duplex=False)

    plc.controls(io, cycle=4, inputs=32, outputs=16, hold=5, delay_ms=1.5)
    plc.sends(host, rate=500, size="imix", pattern="poisson", priority=3)
    plc.sends(host, rate=200, size=1500, pattern="batch", batch="pareto:1.8", delay_ms=10)
    host.publishes(broker, topic="plant/t", rate=2, payload=40, qos=1)
    host.polls(slave, register=100, count=4, rate=5)
    broker.routes("plant/t", to=[plc, host])

    net.observe("pn_jitter", on=plc, every=50)
    net.observe("queue_depth", on=sw, every=20)
    return net


def test_every_kind_maps_to_the_ir() -> None:
    sc = _full_network().to_scenario(duration=1_000, seed=7)
    assert sc.name == "everything"
    assert sc.nodes == (
        AdapterNode("plc", "10.0.0.1", "02:00:0a:00:00:01"),
        AdapterNode("host", "10.0.0.2", "02:aa:bb:cc:dd:ee"),
        SwitchNode("sw", 8, "cut-through", "fifo", "topology"),
        HubNode("hub", 4),
        MqttBrokerNode("broker", "10.0.1.1", "02:00:0a:00:01:01"),
        ModbusSlaveNode("slave", "10.0.2.1", "02:00:0a:00:02:01", 7),
        ProfinetDeviceNode("io", "10.0.3.1", "02:00:0a:00:03:01"),
    )
    assert sc.links[-1] == Link("hub", "sw", 10, 100, duplex=False)
    assert [lk.a for lk in sc.links] == ["plc", "host", "broker", "slave", "io", "hub"]
    assert sc.traffic == (
        ProfinetAR("plc", "io", 4, 32, 16, 5, 1.5),
        Flow("plc", "host", 500, "imix", "poisson", 0.0, None, 3),
        Flow("plc", "host", 200, 1500, "batch", 10, BatchSpec("pareto", 1.8), 0),
        Publish("host", "broker", "plant/t", 2, 40, 1, 0.0),
        Poll("host", "slave", 100, 4, 5, 0.0),
        Route("broker", "plant/t", ("plc", "host")),
    )
    assert sc.observations == (
        Observation("pn_jitter", "plc", 50),
        Observation("queue_depth", "sw", 20),
    )
    assert sc.run == RunConfig(1_000, 7)
    assert sc.node("slave") == ModbusSlaveNode("slave", "10.0.2.1", "02:00:0a:00:02:01", 7)


def test_full_network_round_trips() -> None:
    _round_trip(_full_network().to_scenario(duration=1_000, seed=7))


def test_scenario_without_run_round_trips() -> None:
    sc = _full_network().to_scenario()
    assert sc.run is None
    _round_trip(sc)


@pytest.mark.parametrize(
    "ip, mac",
    [("10.0.4.1", "02:00:0a:00:04:01"), ("192.168.255.0", "02:00:c0:a8:ff:00")],
)
def test_derived_mac_has_six_bytes(ip: str, mac: str) -> None:
    assert Network("n").adapter("a", ip=ip).mac == mac


@pytest.mark.parametrize("ip", ["10.0.1", "10.0.0.256", "host.local"])
def test_mac_cannot_be_derived_from_a_non_ipv4_address(ip: str) -> None:
    with pytest.raises(ValueError, match="cannot derive a MAC"):
        Network("n").adapter("a", ip=ip)
    with pytest.raises(ValueError, match="is not an IPv4 address"):  # an explicit MAC does not help
        Network("n").adapter("a", ip=ip, mac="02:00:00:00:00:01")


def test_json_is_versioned() -> None:
    data = json.loads(Network("empty").to_scenario().to_json())
    assert data["format"] == "smol-ir"
    assert data["version"] == 1


@pytest.mark.parametrize(
    "path", sorted(EXAMPLES.glob("*.py")), ids=lambda p: p.name
)  # fmt: skip
def test_python_example_round_trips(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sc = _example_scenario(path, monkeypatch)
    assert sc.nodes and sc.links and sc.run is not None
    _round_trip(sc)


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.smol")), ids=lambda p: p.name)
def test_smol_example_round_trips(path: Path) -> None:
    sc = interpret_to_scenario(path)
    assert sc.nodes and sc.links and sc.run is not None
    _round_trip(sc)


def test_smol_profinet_example_matches_its_python_twin(monkeypatch: pytest.MonkeyPatch) -> None:
    from_smol = interpret_to_scenario(EXAMPLES / "example_profinet.smol")
    from_python = _example_scenario(EXAMPLES / "example_profinet.py", monkeypatch)
    assert from_smol == from_python


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_benchmark_scenario_round_trips(scenario: str) -> None:
    built = SCENARIOS[scenario](100_000, True)
    sc = built.network.to_scenario(duration=built.duration_ms)
    assert len(sc.nodes) == len(built.network._nodes)
    assert len(sc.links) == len(built.network._links)
    assert len(sc.observations) == len(built.network._observations)
    _round_trip(sc)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="other"), "not a smol-ir document"),
        (lambda d: d.update(version=2), "unsupported smol-ir version 2"),
        (lambda d: d.update(extra=1), "unknown field(s) extra"),
        (lambda d: d["nodes"][0].update(kind="router"), "unknown kind 'router'"),
        (lambda d: d["nodes"][0].pop("ip"), "missing field(s) ip"),
        (lambda d: d["links"][0].update(speed=1), "unknown field(s) speed"),
        (lambda d: d["traffic"][2].update(batch={"kind": "zipf", "param": 2}), "batch kind"),
    ],
)  # fmt: skip
def test_from_json_rejects_malformed_documents(mutate, message: str) -> None:
    data = _full_network().to_scenario(duration=1_000).to_dict()
    mutate(data)
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        Scenario.from_dict(data)


def test_optional_fields_take_their_defaults() -> None:
    data = _full_network().to_scenario(duration=1_000).to_dict()
    for key in ("mode", "qos", "mac_table"):
        del data["nodes"][2][key]
    del data["links"][0]["duplex"]
    del data["run"]["seed"]
    sc = Scenario.from_dict(data)
    assert sc.nodes[2] == SwitchNode("sw", 8)
    assert sc.links[0].duplex is True
    assert sc.run == RunConfig(1_000, 42)
