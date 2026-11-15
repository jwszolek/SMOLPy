"""The semantics contract (docs/model-semantics.md) checked against the SimPy
reference engine (ADR-0007, step 4). Each test pins one statement of the page;
when a test here has to change, the page has to change with it."""

from __future__ import annotations

import pytest

from smolpy import Network
from smolpy.model import frames, timebase
from smolpy.model.streams import source_ids, stream_seed


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
    assert times == [0.25 * k for k in range(1, 20)]  # exact: integer ns, no drift


def test_time_is_integer_nanoseconds() -> None:
    # 10 Gb/s: preamble, SFD and a 64 B frame (72 B) take 57.6 ns, rounded to 58
    net, a, b = _pair(speed=10_000, length=3)  # prop 15 ns
    a.sends(b, rate=1_000, size=64)
    net.observe("latency", on=b, every=10)
    latency_ns = 2 * timebase.transmission_ns(8 + 64, 10_000) + 2 * timebase.propagation_ns(3)
    assert latency_ns == 2 * 58 + 2 * 15
    assert set(_values(net.simulate(duration=100), "latency:b")) == {latency_ns / 1_000}


def test_periodic_sources_do_not_drift() -> None:
    # 3 frames/s: the interval is 333 333 333.3... ns. The start is chosen so
    # that every third frame arrives exactly at a sample instant, where it
    # counts in the next window (§9). A source that drifted by even 1 ns, e.g.
    # by adding a rounded interval, would move those frames across the sample.
    net, a, b = _pair(speed=100, length=0)
    latency = 2 * timebase.transmission_ns(1_000, 100)  # 160 µs
    delay_ns = 10**9 - latency
    a.sends(b, rate=3, size=1_000, delay_ms=delay_ns / 10**6)
    net.observe("bytes_received", on=b, every=1_000)
    period = timebase.interval_ns(3)
    received = _values(net.simulate(duration=1_000_000), "bytes_received:b")
    assert len(received) == 999  # no sample at t = duration
    for j, value in enumerate(received, start=1):
        arrived = sum(
            1
            for k in range(1, 3 * j + 1)
            if timebase.periodic_ns(delay_ns, period, k) + latency < j * 10**9
        )
        # frame k arrives at 10⁹ + round(k · T): frame 3(j-1) exactly at sample j
        assert arrived == max(0, 3 * j - 4)
        assert value == arrived * 1_000 / 10**6  # MB, as the engine computes it


# §2 Frames


def test_frame_sizes() -> None:
    # 58 B: Ethernet header (14), IPv4 (20), TCP (20), FCS (4)
    assert frames.ETHERNET_IP_TCP_BYTES == 58
    assert frames.mqtt_publish_frame_size("plant/t", 20, qos=0) == 58 + 2 + 2 + 7 + 20
    assert frames.mqtt_publish_frame_size("plant/t", 20, qos=1) == 58 + 2 + 2 + 7 + 2 + 20
    assert frames.MQTT_PUBACK_BYTES == 64  # 62 B, padded to the minimum
    assert frames.MODBUS_REQUEST_BYTES == 69
    assert frames.modbus_response_frame_size(10) == 86
    assert frames.modbus_response_frame_size(1) == 68
    # 14 + 4 (802.1Q) + 2 (FrameID) + IO + 4 (APDU status) + 4 (FCS), at least 64
    assert frames.pn_frame_size(10) == 64 and frames.pn_frame_size(100) == 128
    assert frames.PN_RT_PRIORITY == 6
    # a flow frame with a priority above 0 carries an 802.1Q tag
    assert frames.flow_frame_size(100, 0) == 100 and frames.flow_frame_size(100, 3) == 104
    assert frames.WIRE_OVERHEAD_BYTES == 8 + 12  # preamble and SFD, inter-frame gap


def test_priority_frames_carry_a_tag() -> None:
    net, a, b = _pair(speed=100)
    a.sends(b, rate=1_000, size=100, priority=3)
    net.observe("bytes_received", on=b, every=10)
    net.observe("latency", on=b, every=10)
    result = net.simulate(duration=15)
    assert _values(result, "bytes_received:b")[0] == 9 * 104 / 10**6
    _all_equal(_values(result, "latency:b"), 2 * (8 + 104) * 8 / 100)


# §3 Links and egress queues


def test_single_hop_latency_is_tx_plus_prop_per_link() -> None:
    # 8 B preamble and SFD + a 117 B frame = 125 B = 1 µs at 1 Gb/s; prop 0.5 µs
    net, a, b = _pair(speed=1_000, length=100)
    a.sends(b, rate=1_000, size=117)
    net.observe("latency", on=b, every=10)
    _all_equal(_values(net.simulate(duration=100), "latency:b"), 3.0)


def test_propagation_does_not_hold_the_link() -> None:
    # A frame every 1.2 µs, 1 µs on the wire (+ 0.096 µs gap), 5 µs in flight:
    # if propagation held the link the queue would grow without bound.
    net, a, b = _pair(speed=1_000, length=1_000)
    a.sends(b, rate=1e6 / 1.2, size=117)
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
    assert values[2] == 64 / 10**6  # input frame (36 B of IO: 64 B) sent at t = 1 ms
    assert values[3] == 64 / 10**6  # next one at t = 2 ms


def test_bursty_long_run_rate() -> None:
    net, a, b = _pair()
    a.sends(b, rate=1_000, size=125, pattern="bursty")
    net.observe("bytes_received", on=b, every=50_000)
    t_ms, mb = net.simulate(duration=100_001).metrics["bytes_received:b"][-1]
    rate = mb * 1e6 / 125 / (t_ms / 1_000)
    assert rate == pytest.approx(1_000, rel=0.03)


def test_source_ids() -> None:
    net = Network("ids")
    a, b, c = (net.adapter(n, ip=f"10.0.0.{i}") for i, n in enumerate("abc", 1))
    broker = net.mqtt_broker("broker", ip="10.0.0.9")
    sw = net.switch("sw", ports=4)
    for node in (a, b, c, broker):
        net.link(node, sw, speed=100, length=0)
    a.sends(b, rate=10)
    a.publishes(broker, topic="t", rate=1)
    a.sends(c, rate=10)
    a.sends(b, rate=10, pattern="poisson")
    broker.routes("t", to=[b])
    # k counts statements of one kind between the same two ends, so an id does
    # not depend on where other statements sit in the scenario
    assert sorted(source_ids(net.to_scenario(duration=10))) == [
        "flow:a->b#0",
        "flow:a->b#1",
        "flow:a->c#0",
        "publish:a->broker#0",
        "route:broker->t#0",
    ]


def test_stream_seed_is_a_sha256_prefix() -> None:
    import hashlib

    digest = hashlib.sha256(b"42:flow:a->b#0").digest()
    assert stream_seed(42, "flow:a->b#0") == int.from_bytes(digest[:8], "big")
    assert stream_seed(42, "flow:a->b#0") == 5297917812165586412  # pinned for every engine


def _random_cell(*, with_other: bool, seed: int = 42) -> dict:
    net = Network("cell")
    a, b, c, d = (net.adapter(n, ip=f"10.0.0.{i}") for i, n in enumerate("abcd", 1))
    sw = net.switch("sw", ports=4)
    for node in (a, b, c, d):
        net.link(node, sw, speed=100, length=0)
    if with_other:  # declared first, so it comes before a -> b in the scenario
        c.sends(d, rate=500, size="imix", pattern="poisson")
    a.sends(b, rate=500, size="imix", pattern="poisson")
    net.observe("bytes_received", on=b, every=10)
    return net.simulate(duration=200, seed=seed).metrics["bytes_received:b"]


def test_adding_a_source_does_not_change_the_others() -> None:
    assert _random_cell(with_other=True) == _random_cell(with_other=False)


def test_run_seed_selects_the_draws() -> None:
    assert _random_cell(with_other=False, seed=1) == _random_cell(with_other=False, seed=1)
    assert _random_cell(with_other=False, seed=1) != _random_cell(with_other=False, seed=2)


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
    # links into b: one 1 000 Mb/s link occupied by (125 + 20) B per frame on the
    # wire: 1.16 Mb/s, while the byte metrics count the 125 B frames only
    assert _values(result, "utilization:b")[1:] == pytest.approx([0.116] * 8)


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
    # request 69 B and response 68 B (+ 8 B preamble and SFD), each over two 100 Mb/s links
    request, response = frames.MODBUS_REQUEST_BYTES, frames.modbus_response_frame_size(1)
    expected = 2 * (8 + request) * 8 / 100 + 2 * (8 + response) * 8 / 100
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
    expected = 4 * (8 + frames.mqtt_publish_frame_size("t", 20, 0)) * 8 / 100
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
