"""The semantics contract (docs/model-semantics.md) checked against the SimPy
reference engine (ADR-0007, step 4). Each test pins one statement of the page;
when a test here has to change, the page has to change with it."""

from __future__ import annotations

import pytest

from smolpy import Network
from smolpy.model import frames


def _pair(
    *, speed: float = 1_000, length: float = 0, mode: str = "store-and-forward"
) -> tuple[Network, object, object]:
    """a -- sw -- b, both links identical."""
    net = Network("pair")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sw = net.switch("sw", ports=2, mode=mode)  # type: ignore[arg-type]
    net.link(a, sw, speed=speed, length=length)
    net.link(b, sw, speed=speed, length=length)
    return net, a, b


def _values(result, key: str) -> list[float]:
    return [v for _, v in result.metrics[key]]


def _all_equal(values: list[float], expected: float) -> None:
    assert values, "no samples"
    assert values == pytest.approx([expected] * len(values))


# §1 Time and units


def test_run_covers_half_open_interval() -> None:
    net, a, b = _pair()
    a.sends(b, rate=1_000, size=125)
    net.observe("bytes_received", on=b, every=100)
    samples = net.simulate(duration=1_000).metrics["bytes_received:b"]
    assert samples[-1][0] == 900.0  # no sample at t = duration
    # frames generated at 1, 2, ... 999 ms; the one at 1 000 ms is not
    assert samples[-1][1] * 1e6 / 125 == 899


def test_samples_fall_on_multiples_of_every() -> None:
    net, a, b = _pair()
    a.sends(b, rate=100)
    net.observe("throughput", on=b, every=0.25)
    times = [t for t, _ in net.simulate(duration=5).metrics["throughput:b"]]
    assert times == pytest.approx([0.25 * k for k in range(1, 20)])


# §2 Frames


def test_frame_sizes() -> None:
    assert frames.mqtt_publish_frame_size("plant/t", 20, qos=0) == 54 + 2 + 2 + 7 + 20
    assert frames.mqtt_publish_frame_size("plant/t", 20, qos=1) == 54 + 2 + 2 + 7 + 2 + 20
    assert frames.MQTT_PUBACK_BYTES == 58
    assert frames.MODBUS_REQUEST_BYTES == 65
    assert frames.modbus_response_frame_size(10) == 82
    assert frames.pn_frame_size(10) == 60 and frames.pn_frame_size(100) == 124
    assert frames.PN_RT_PRIORITY == 6


# §3 Links and egress queues


def test_single_hop_latency_is_tx_plus_prop_per_link() -> None:
    net, a, b = _pair(speed=1_000, length=100)  # tx 125 B = 1 µs, prop 0.5 µs
    a.sends(b, rate=1_000, size=125)
    net.observe("latency", on=b, every=10)
    _all_equal(_values(net.simulate(duration=100), "latency:b"), 3.0)


def test_propagation_does_not_hold_the_link() -> None:
    # A frame every 1.2 µs, 1 µs on the wire, 5 µs in flight: if propagation
    # held the link the queue would grow without bound and latency with it.
    net, a, b = _pair(speed=1_000, length=1_000)
    a.sends(b, rate=1e6 / 1.2, size=125)
    net.observe("latency", on=b, every=0.1)
    net.observe("queue_depth", on=net._nodes["sw"], every=0.1)
    result = net.simulate(duration=2)
    _all_equal(_values(result, "latency:b"), 12.0)
    assert set(_values(result, "queue_depth:sw")) == {0.0}


# §4 Switches


def test_cut_through_behaves_as_store_and_forward() -> None:
    """Open issue 2: mode is not modelled."""
    results = []
    for mode in ("store-and-forward", "cut-through"):
        net, a, b = _pair(speed=100, length=10)
        a.sends(b, rate=5_000, size=1_000)
        net.observe("latency", on=b, every=10)
        results.append(net.simulate(duration=200).metrics)
    assert results[0] == results[1]


# §6 Traffic sources


def test_constant_flow_first_frame_after_one_interval() -> None:
    net, a, b = _pair()
    a.sends(b, rate=1_000, size=125, delay_ms=2)  # first frame at 2 + 1 ms
    net.observe("bytes_received", on=b, every=0.5)
    values = _values(net.simulate(duration=5), "bytes_received:b")
    assert values[:6] == [0, 0, 0, 0, 0, 0]  # up to t = 3.0 ms
    assert values[6] == pytest.approx(125e-6)  # t = 3.5 ms


def test_profinet_first_frame_at_delay() -> None:
    net = Network("pn")
    plc = net.adapter("plc", ip="10.0.0.1")
    io = net.profinet_device("io", ip="10.0.0.2")
    sw = net.switch("sw", ports=2)
    net.link(plc, sw, speed=100, length=0)
    net.link(io, sw, speed=100, length=0)
    plc.controls(io, cycle=1, inputs=36, outputs=36, delay_ms=1)
    net.observe("bytes_received", on=plc, every=0.5)
    values = _values(net.simulate(duration=3), "bytes_received:plc")
    assert values[:2] == [0, 0]  # t = 0.5, 1.0 ms
    assert values[2] == pytest.approx(60e-6)  # input frame sent at t = 1 ms
    assert values[3] == pytest.approx(60e-6)  # next one at t = 2 ms


def test_bursty_long_run_rate() -> None:
    """Open issue 1: bursty averages rate / 1.5."""
    net, a, b = _pair()
    a.sends(b, rate=1_000, size=125, pattern="bursty")
    net.observe("bytes_received", on=b, every=50_000)
    t_ms, mb = net.simulate(duration=100_001).metrics["bytes_received:b"][-1]
    rate = mb * 1e6 / 125 / (t_ms / 1_000)
    assert rate == pytest.approx(1_000 / 1.5, rel=0.03)


# §8 Metrics


def test_metric_definitions() -> None:
    net, a, b = _pair(speed=1_000)
    a.sends(b, rate=1_000, size=125)  # 1 Mb/s
    for metric, node in [("throughput", b), ("bytes_received", b), ("bytes_sent", a)]:
        net.observe(metric, on=node, every=100)
    net.observe("utilization", on=b, every=100)
    result = net.simulate(duration=1_000)
    assert _values(result, "throughput:b")[1:] == pytest.approx([1.0] * 8)
    received = _values(result, "bytes_received:b")
    assert received == sorted(received)  # cumulative
    assert received[-1] == pytest.approx(899 * 125 / 1e6)
    # the frame generated at 900 ms finishes transmission after the sample
    assert _values(result, "bytes_sent:a")[-1] == pytest.approx(899 * 125 / 1e6)
    # links into b: one 1 000 Mb/s link carrying 1 Mb/s
    assert _values(result, "utilization:b")[1:] == pytest.approx([0.1] * 8)


def test_window_metrics_skip_empty_windows() -> None:
    net, a, b = _pair()
    a.sends(b, rate=1, size=125)  # frames at 1 000 and 2 000 ms
    net.observe("latency", on=b, every=100)
    net.observe("throughput", on=b, every=100)
    result = net.simulate(duration=3_000)
    assert [t for t, _ in result.metrics["latency:b"]] == [1_100.0, 2_100.0]
    assert len(result.metrics["throughput:b"]) == 29


def test_modbus_latency_is_round_trip() -> None:
    net = Network("mb")
    master = net.adapter("m", ip="10.0.0.1")
    slave = net.modbus_slave("s", ip="10.0.0.2", unit_id=1)
    sw = net.switch("sw", ports=2)
    net.link(master, sw, speed=100, length=0)
    net.link(slave, sw, speed=100, length=0)
    master.polls(slave, register=0, count=1, rate=10)
    net.observe("modbus_latency", on=master, every=100)
    # request 65 B and response 64 B, each over two 100 Mb/s links
    expected = 2 * 65 * 8 / 100 + 2 * 64 * 8 / 100
    values = _values(net.simulate(duration=1_000), "modbus_latency:m")
    _all_equal(values, expected)


def test_mqtt_latency_is_end_to_end() -> None:
    net = Network("mqtt")
    sensor = net.adapter("sensor", ip="10.0.0.1")
    server = net.adapter("server", ip="10.0.0.2")
    broker = net.mqtt_broker("broker", ip="10.0.0.3")
    sw = net.switch("sw", ports=3)
    for node in (sensor, server, broker):
        net.link(node, sw, speed=100, length=0)
    broker.routes("t", to=[server])
    sensor.publishes(broker, topic="t", rate=10, payload=20, qos=0)
    net.observe("latency", on=server, every=100)
    # sensor -> sw -> broker -> sw -> server: four transmissions of the PUBLISH
    expected = 4 * frames.mqtt_publish_frame_size("t", 20, 0) * 8 / 100
    values = _values(net.simulate(duration=1_000), "latency:server")
    _all_equal(values, expected)


def test_watchdog_expiries_are_cumulative() -> None:
    net = Network("wd")
    plc = net.adapter("plc", ip="10.0.0.1")
    io = net.profinet_device("io", ip="10.0.0.2")
    bulk = net.adapter("bulk", ip="10.0.0.3")
    sw = net.switch("sw", ports=3, qos="fifo")  # no priority: bulk delays RT frames
    for node in (plc, io, bulk):
        net.link(node, sw, speed=100, length=0)
    plc.controls(io, cycle=1, inputs=32, outputs=32, hold=1)
    bulk.sends(plc, rate=20_000, size=1_500, pattern="batch", batch=50)
    net.observe("pn_watchdog_expiries", on=plc, every=10)
    values = _values(net.simulate(duration=500), "pn_watchdog_expiries:plc")
    assert values == sorted(values) and values[-1] > values[0] > 0


def test_every_test_the_contract_names_exists() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    page = (root / "docs" / "model-semantics.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`(test_\w+)`", page))
    defined = {
        m for f in (root / "tests").glob("test_*.py")
        for m in re.findall(r"^\s*def (test_\w+)", f.read_text(encoding="utf-8"), re.M)
    }  # fmt: skip
    assert named and not named - defined, f"named in the contract but missing: {named - defined}"
