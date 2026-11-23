"""The ns-3 backend (smolpy.ns3): locating ns-3, lowering a scenario for
smol-ns3, and computing metrics from its counters, which need no ns-3; and
Modbus and MQTT over ns-3's TCP, against SimPy, where NS3_HOME names a build.
The equivalence suite runs the backend on its scenarios."""

from __future__ import annotations

import dataclasses
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


def test_a_cache_it_cannot_write_is_explained(monkeypatch, tmp_path) -> None:
    """In the -ns3 image the cache is read-only, with smol-ns3 built in: a
    run that would need another build says so, and what to do. Here the
    cache is a file, which no user can create directories in, root
    included (CI runs as root)."""
    cache = tmp_path / "cache"
    cache.write_text("")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    found = ns3.Installation(tmp_path, tmp_path, tmp_path, ("ns3.49-core",), "c++")
    with pytest.raises(ns3.Ns3Unavailable, match="not built, and cannot be built in .*XDG"):
        ns3.program(found)


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
        "smol-ns3 2",
        "duration 10000000",
        "switch 0",  # unbounded
        "adapter 02:00:0a:00:00:01 10.0.0.1",
        "adapter 02:00:0a:00:00:02 10.0.0.2",
        "adapter 02:00:0a:00:00:03 10.0.0.3",
        "link 1 0 100 1 50 960 1 1",  # 10 m: 50 ns; 12 B gap at 100 Mb/s; strict queues
        "link 2 0 5 2 15 38400 1 1",  # 2.5 Mb/s = 5/2
        "static 0 02:00:0a:00:00:01 0",  # stations wired to the switch
        "static 0 02:00:0a:00:00:02 1",
        "flow 1 10.0.0.2 466 0 250000 1000000 3",  # 512 - 46 B of UDP payload; T = 10^6/3 ns
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


def test_random_flows_list_their_frames() -> None:
    """Flows other than constant ones of fixed size list every frame, drawn
    as the reference engine draws them; a FIFO switch has no strict queues."""
    net = Network("random")
    sw = net.switch("sw", ports=2, qos="fifo")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100, length=1)
    net.link(b, sw, speed=100, length=1)
    a.sends(b, rate=1_000, size=200, pattern="batch", batch=3, priority=4)
    a.sends(b, rate=500, size="imix", delay_ms=1)
    text, _ = ns3.lower(net.to_scenario(duration=100))
    lines = text.splitlines()
    assert lines[5] == "link 1 0 100 1 5 960 1 0"  # the switch's end is FIFO
    batch, imix = (line.split() for line in lines if line.startswith("sends"))
    assert batch[:4] == ["sends", "1", "10.0.0.2", "4"]
    sends = list(zip(batch[5::2], batch[6::2], strict=True))
    assert len(sends) == int(batch[4]) and len(sends) % 3 == 0  # groups of 3 frames
    assert all(payload == str(200 - 46) for _, payload in sends)
    assert sends[0][0] == sends[1][0] == sends[2][0]  # a group shares its instant
    imix_sends = list(zip(imix[5::2], imix[6::2], strict=True))
    assert [int(t) for t, _ in imix_sends[:3]] == [3_000_000, 5_000_000, 7_000_000]
    assert len(imix_sends) == 49  # 3, 5, ... 99 ms
    assert {int(p) + 46 for _, p in imix_sends} <= {64, 594, 1518}


def test_lowering_modbus_and_mqtt() -> None:
    """One TCP connection per client and server, opened by the master or MQTT
    client; messages in bytes of TCP payload; topics numbered."""
    net = Network("tcp")
    sw = net.switch("sw", ports=5)
    scada = net.adapter("scada", ip="10.0.0.1")
    meter = net.modbus_slave("meter", ip="10.0.0.2", unit_id=3)
    broker = net.mqtt_broker("broker", ip="10.0.0.3")
    hmi = net.adapter("hmi", ip="10.0.0.4")
    lonely = net.adapter("lonely", ip="10.0.0.5")  # no link: no connection
    for node in (scada, meter, broker, hmi):
        net.link(node, sw, speed=100, length=0)
    scada.polls(meter, register=0, count=60, rate=400)
    scada.polls(meter, register=100, count=1, rate=200, delay_ms=0.5)  # the same connection
    broker.routes("plant/temp°C", to=[hmi, scada, lonely])
    scada.publishes(broker, topic="plant/temp°C", rate=300, payload=64, qos=1)
    hmi.publishes(broker, topic="other", rate=100, payload=16)
    net.observe("modbus_latency", on=scada, every=5)
    net.observe("broker_queue", on=broker, every=1)
    text, samplers = ns3.lower(net.to_scenario(duration=10))
    lines = text.splitlines()
    assert lines[3:6] == [  # every node with an IP address is a host
        "adapter 02:00:0a:00:00:01 10.0.0.1",
        "adapter 02:00:0a:00:00:02 10.0.0.2",
        "adapter 02:00:0a:00:00:03 10.0.0.3",
    ]
    assert "static 0 02:00:0a:00:00:03 2" in lines  # the broker, as on SimPy
    assert lines[lines.index("connect 1 3 1883") :] == [
        "connect 1 3 1883",  # scada, the MQTT client
        "publish 0 82 1 0 0 10000000 3",  # 2 + 2 + 12 characters + 2 + 64 B
        "connect 1 2 502",
        "poll 1 11 128 0 2500000 1",  # MBAP, FC and count, and 60 registers
        "poll 1 11 10 500000 5000000 1",  # the same connection
        "connect 4 3 1883",  # hmi, a subscriber
        "route 3 0 2 0",  # hmi's and scada's connections; lonely has none
        "publish 2 25 0 1 0 10000000 1",
        "observe modbus_latency 1 5000000 1",
        "observe broker_queue 3 1000000 1",
    ]
    assert [s.metric for s in samplers] == ["modbus_latency", "broker_queue"]


@pytest.fixture
def fake_ns3(monkeypatch):
    """The ns3 backend as if ns-3 were installed, for its capability checks."""
    monkeypatch.setattr(ns3, "locate", lambda: None)


def test_it_runs_the_whole_model(fake_ns3) -> None:
    """Every node, traffic kind, pattern and metric of the reference engine;
    headless only."""
    ns3_caps = backends.REGISTRY["ns3"].capabilities()
    simpy_caps = backends.REGISTRY["simpy"].capabilities()
    for field in ("nodes", "traffic", "patterns", "metrics", "features"):
        assert getattr(ns3_caps, field) == getattr(simpy_caps, field), field
    assert ns3_caps.priorities and not ns3_caps.dashboards and not ns3_caps.notes


def test_lowering_profinet() -> None:
    """ARs by controller in node order, as the reference engine starts them;
    frames sized by the model, 802.1Q tag and padding included. An AR with
    an unwired side exchanges nothing."""
    net = Network("pn")
    sw = net.switch("sw", ports=4)
    drive = net.profinet_device("drive", ip="10.0.1.1")
    robot = net.adapter("robot", ip="10.0.0.2")
    plc = net.adapter("plc", ip="10.0.0.1")
    spare = net.profinet_device("spare", ip="10.0.1.9")  # no link
    for node in (drive, robot, plc):
        net.link(node, sw, speed=100, length=0)
    plc.controls(drive, cycle=4, inputs=100, outputs=8, hold=2, delay_ms=0.25)
    robot.controls(drive, cycle=1, inputs=200, outputs=40)
    plc.controls(spare, cycle=2, inputs=8, outputs=8)
    net.observe("pn_jitter", on=drive, every=4)
    net.observe("pn_watchdog_expiries", on=spare, every=4)  # samples 0, as on SimPy
    text, samplers = ns3.lower(net.to_scenario(duration=10))
    assert [line for line in text.splitlines() if line.startswith(("ar", "observe"))] == [
        "ar 2 1 0 1000000 3 0 68 228",  # robot's first: robot comes first in node order
        "ar 3 1 1 4000000 2 250000 64 128",  # 8 B of outputs: padded to 64 B
        "observe pn_jitter 1 4000000 1",
        "observe pn_watchdog_expiries 4 4000000 1",
    ]
    assert [s.metric for s in samplers] == ["pn_jitter", "pn_watchdog_expiries"]


def test_priorities_can_be_outside_a_backends_capabilities(monkeypatch) -> None:
    """The check itself, with a backend that declares no priorities."""
    monkeypatch.setattr(ns3, "locate", lambda: None)
    caps = backends.REGISTRY["ns3"].capabilities()
    monkeypatch.setattr(
        backends.Ns3Backend,
        "capabilities",
        lambda self: dataclasses.replace(caps, priorities=False),
    )
    net = Network("prio")
    sw = net.switch("sw", ports=2)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100, length=1)
    net.link(b, sw, speed=100, length=1)
    a.sends(b, rate=100, size=100, priority=3)
    with pytest.raises(ScenarioError, match="flow a -> b: the ns3 backend does not support 802.1Q"):
        backends.check(net.to_scenario(duration=10), "ns3")


# --- on ns-3 ---

needs_ns3 = pytest.mark.skipif(
    not backends.REGISTRY["ns3"].available(), reason="NS3_HOME names no ns-3 build"
)

# A frame of 64 B ahead on a 100 Mb/s link, with its gap: (8 + 64) B + 12 B.
ACK_US = (8 + 64 + 12) * 8 / 100
SYN_ACK = 66  # a SYN-ACK with ns-3's window scale and SACK options: 20 + 28 + 18 B


def _run(net: Network, duration: float):
    scenario = net.to_scenario(duration=duration)
    return backends.get("simpy").run(scenario).metrics, backends.get("ns3").run(scenario).metrics


def _values(samples) -> list[float]:
    return [v for _, v in samples]


def _bytes(samples) -> list[int]:
    return [round(v * 1_000_000) for _, v in samples]


@needs_ns3
def test_modbus_over_tcp() -> None:
    """Every round trip is SimPy's plus 1 ns per send, TCP's time step. The
    first request is acknowledged at once, and its response waits behind the
    acknowledgement (in which time its own step passes); later
    acknowledgements travel with the next request."""
    net = Network("modbus")
    sw = net.switch("sw", ports=2)
    master = net.adapter("master", ip="10.0.0.1")
    slave = net.modbus_slave("slave", ip="10.0.0.2", unit_id=1)
    net.link(master, sw, speed=100, length=0)
    net.link(slave, sw, speed=100, length=0)
    master.polls(slave, register=0, count=1, rate=100)
    net.observe("modbus_latency", on=master, every=10)
    net.observe("bytes_received", on=master, every=10)
    simpy, ns3 = _run(net, 60)
    rtt = _values(simpy["modbus_latency:master"])[0]
    assert _values(ns3["modbus_latency:master"]) == pytest.approx(
        [rtt + ACK_US + 0.001] + [rtt + 0.002] * 3, abs=1e-9
    )
    response = _bytes(simpy["bytes_received:master"])[1]
    assert _bytes(ns3["bytes_received:master"]) == [
        SYN_ACK + 64 * (k > 0) + response * k for k in range(5)
    ]


@needs_ns3
@pytest.mark.parametrize("qos", [0, 1])
def test_mqtt_over_tcp(qos: int) -> None:
    """A PUBLISH reaches its subscriber 2 ns later than on SimPy: 1 ns per
    send. With QoS 1, each PUBACK carries the broker's acknowledgement; with
    QoS 0, the broker acknowledges every second PUBLISH with a frame of its
    own, which the copy to the subscriber waits behind."""
    net = Network("mqtt")
    sw = net.switch("sw", ports=3)
    sensor = net.adapter("sensor", ip="10.0.0.1")
    broker = net.mqtt_broker("broker", ip="10.0.0.2")
    hmi = net.adapter("hmi", ip="10.0.0.3")
    for node in (sensor, broker, hmi):
        net.link(node, sw, speed=100, length=0)
    sensor.publishes(broker, topic="t", rate=100, payload=20, qos=qos)
    broker.routes("t", to=[hmi])
    net.observe("bytes_received", on=sensor, every=10)
    net.observe("latency", on=sensor, every=10)
    net.observe("bytes_received", on=hmi, every=10)
    net.observe("latency", on=hmi, every=10)
    net.observe("broker_queue", on=broker, every=10)
    simpy, ns3 = _run(net, 60)
    copy = _values(simpy["latency:hmi"])[0]
    acked = [k % 2 == 0 for k in range(4)] if qos == 0 else [k == 0 for k in range(4)]
    assert _values(ns3["latency:hmi"]) == pytest.approx(
        [copy + (ACK_US + 0.001 if a else 0.002) for a in acked], abs=1e-9
    )
    if qos:
        puback = _values(simpy["latency:sensor"])[0]
        assert _values(ns3["latency:sensor"]) == pytest.approx(
            [puback + ACK_US] + [puback + 0.001] * 3, abs=1e-9
        )
        assert _bytes(ns3["bytes_received:sensor"]) == [
            SYN_ACK + 64 * (k > 0) + 64 * k for k in range(5)
        ]
    else:
        assert _values(ns3["latency:sensor"]) == []  # acknowledgements carry no data
        assert _bytes(ns3["bytes_received:sensor"]) == [
            SYN_ACK + 64 * ((k + 1) // 2) for k in range(5)
        ]
    message = _bytes(simpy["bytes_received:hmi"])[1]
    assert _bytes(ns3["bytes_received:hmi"]) == [SYN_ACK + message * k for k in range(5)]
    assert _values(ns3["broker_queue:broker"]) == [0.0] * 5


@needs_ns3
@pytest.mark.parametrize("length", [0, 100])
def test_a_frame_at_the_watchdogs_deadline_is_too_late(length: float) -> None:
    """With a hold of one cycle and no jitter, every frame arrives exactly at
    the deadline, which comes first, as on SimPy: one expiry per cycle."""
    net = Network("deadline")
    sw = net.switch("sw", ports=2)
    plc = net.adapter("plc", ip="10.0.0.1")
    io = net.profinet_device("io", ip="10.0.0.2")
    net.link(plc, sw, speed=100, length=length)
    net.link(io, sw, speed=100, length=length)
    plc.controls(io, cycle=1, inputs=10, outputs=10, hold=1)
    net.observe("pn_watchdog_expiries", on=io, every=5)
    net.observe("pn_jitter", on=io, every=5)
    simpy, ns3 = _run(net, 20)
    assert ns3 == simpy
    assert _values(ns3["pn_watchdog_expiries:io"]) == [4.0, 9.0, 14.0]  # from the 2nd frame
    assert set(_values(ns3["pn_jitter:io"])) == {0.0}


@needs_ns3
def test_rates_and_speeds_beyond_64_bits() -> None:
    """A rate or speed computed in Python is an exact rational of its decimal
    form, whose terms can exceed 64 bits: still SimPy's run, bit for bit."""
    net = Network("ratios")
    sw = net.switch("sw", ports=3)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    m = net.modbus_slave("m", ip="10.0.0.3", unit_id=1)
    net.link(a, sw, speed=100 / 3, length=7)
    net.link(b, sw, speed=100, length=7)
    net.link(m, sw, speed=100, length=7)
    rate = 0.8 * 10e6 / (1520 * 8)  # 657.8947368421053 frames/s
    a.sends(b, rate=rate, size=1500)
    a.sends(b, rate=rate / 7, size=200, priority=5)
    net.observe("latency", on=b, every=10 / 3)
    net.observe("utilization", on=sw, every=4.1)
    scenario = net.to_scenario(duration=200)
    text, _ = ns3.lower(scenario)
    assert any(len(f) > 20 for line in text.splitlines() for f in line.split())
    simpy, ns3_run = _run(net, 200)
    assert ns3_run == simpy
