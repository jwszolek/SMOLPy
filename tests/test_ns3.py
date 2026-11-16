"""The ns-3 backend's Python side (smolpy.ns3): locating ns-3, lowering a
scenario for smol-ns3, and computing metrics from its counters. None of these
tests need ns-3; the equivalence suite runs the backend itself where
NS3_HOME names a build."""

from __future__ import annotations

from fractions import Fraction

import pytest

from smolpy import Network, backends, ns3
from smolpy.model.validate import ScenarioError


@pytest.fixture
def no_ns3(monkeypatch):
    monkeypatch.delenv("NS3_HOME", raising=False)


def test_without_ns3_home_the_backend_is_unavailable(no_ns3) -> None:
    with pytest.raises(ns3.Ns3Unavailable, match="NS3_HOME is not set"):
        ns3.locate()
    assert "ns3" not in backends.installed()
    with pytest.raises(backends.BackendUnavailable, match="NS3_HOME"):
        backends.get("ns3")


def test_ns3_home_must_hold_a_build(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NS3_HOME", str(tmp_path))
    with pytest.raises(ns3.Ns3Unavailable, match="has no ns-3 build"):
        ns3.locate()


def test_a_build_is_found(monkeypatch, tmp_path) -> None:
    lib, include = tmp_path / "build" / "lib", tmp_path / "build" / "include" / "ns3"
    lib.mkdir(parents=True)
    include.mkdir(parents=True)
    (include / "core-module.h").touch()
    for module in ("core", "network", "internet"):
        (lib / f"libns3.49-{module}.so").touch()
        (lib / f"libns3.49-{module}-optimized.so").touch()  # another profile: ignored
    monkeypatch.setenv("NS3_HOME", str(tmp_path))
    monkeypatch.setenv("CXX", "sh")  # any program on PATH
    found = ns3.locate()
    assert found.libs == ("ns3.49-internet", "ns3.49-network", "ns3.49-core")
    assert found.compiler == "sh"


def _network() -> Network:
    net = Network("small")
    sw = net.switch("sw", ports=3, mac_table="topology")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100, length=10)
    net.link(b, sw, speed=2.5, length=3)
    a.sends(b, rate=3_000, size=512, delay_ms=0.25)
    net.observe("latency", on=b, every=1.5)
    net.observe("utilization", on=sw, every=2)
    c = net.adapter("c", ip="10.0.0.3")
    net.observe("throughput", on=c, every=1)  # no link: no samples, as on SimPy
    return net


def test_lowering() -> None:
    text, samplers = ns3.lower(_network().to_scenario(duration=10))
    assert text.splitlines() == [
        "smol-ns3 1",
        "duration 10000000",
        "switch",
        "adapter 02:00:0a:00:00:01 10.0.0.1",
        "adapter 02:00:0a:00:00:02 10.0.0.2",
        "adapter 02:00:0a:00:00:03 10.0.0.3",
        "link 1 0 100 1 50 960",  # 10 m: 50 ns; 12 B gap at 100 Mb/s
        "link 2 0 5 2 15 38400",  # 2.5 Mb/s = 5/2
        "static 0 02:00:0a:00:00:01 0",  # stations wired to the switch
        "static 0 02:00:0a:00:00:02 1",
        "flow 1 10.0.0.2 466 250000 1000000 3",  # 512 - 46 B of UDP payload; T = 10^6/3 ns
        "sink 2",
        "observe latency 2 1500000 1",
        "observe utilization 0 2000000 1 0 2",  # the devices that send to the switch
    ]
    assert [s.key for s in samplers] == ["latency:b", "utilization:sw", "throughput:c"]
    assert samplers[1].speed == Fraction(205, 2)
    assert samplers[2].metric == ""  # reported, with no samples


def test_metrics_use_the_reference_formulas() -> None:
    _, samplers = ns3.lower(_network().to_scenario(duration=10))
    output = "\n".join(
        [
            "0 1500000 3000 2",  # two latencies summing to 3 µs
            "0 3000000 0 0",  # an empty window: no sample
            "1 2000000 4100 0",  # bits into the switch
            "1 4000000 8200 0",
            "events 123",
        ]
    )
    metrics, events = ns3._metrics(output, samplers)
    assert events == 123
    assert metrics["latency:b"] == [(1.5, 3000 / (2 * 1000))]
    assert metrics["utilization:sw"] == [
        (2.0, float(Fraction(4100 * 100_000) / (Fraction(205, 2) * 2_000_000))),
        (4.0, float(Fraction(4100 * 100_000) / (Fraction(205, 2) * 2_000_000))),
    ]
    assert metrics["throughput:c"] == []


@pytest.fixture
def fake_ns3(monkeypatch):
    """The ns3 backend as if ns-3 were installed, for its capability checks."""
    monkeypatch.setattr(ns3, "locate", lambda: None)


def test_priorities_are_outside_its_capabilities(fake_ns3) -> None:
    net = Network("prio")
    sw = net.switch("sw", ports=2)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100, length=1)
    net.link(b, sw, speed=100, length=1)
    a.sends(b, rate=100, size=100, priority=3)
    with pytest.raises(ScenarioError, match="flow a -> b: the ns3 backend does not support 802.1Q"):
        backends.check(net.to_scenario(duration=10), "ns3")
    with pytest.raises(ScenarioError, match="Modbus slave \\(planned for Phase 6\\)"):
        net.modbus_slave("m", ip="10.0.0.3", unit_id=1)
        backends.check(net.to_scenario(duration=10), "ns3")
