"""Bounded switch buffers with tail drop (ADR-0011).

- Step 1: the scenario. The parameter in the Python API and in .smol, its
  validation, the IR (still version 2: an optional field, written only when
  set) and the capability that a backend needs to run it.
- Step 2: the reference engine. The admission rule, what a buffer that is
  never full changes (nothing), the bound on delay, the loss metrics, the
  protocols on loss, and an M/D/1/K queue against its analytical solution.
- Step 3: the Rust engine, bit for bit with SimPy down to the event count.
"""

from __future__ import annotations

import math
import statistics

import pytest

from smolpy import Network, backends
from smolpy.lang import SMOLSemanticError
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Scenario, SwitchNode
from smolpy.model import timebase as tb
from smolpy.model.validate import ScenarioError, validate
from smolpy.sim import engine


def _cell(buffer: int | None = 115_000, size: int = 1_518, tagged: int = 0) -> Network:
    net = Network("cell")
    sw = net.switch("sw", ports=3, buffer=buffer)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=8_000, size=size, pattern="poisson")
    if tagged:
        a.sends(b, rate=10, size=tagged, priority=3)
    net.observe("throughput", on=b, every=10)
    return net


def _backend(name: str) -> backends.Backend:
    try:
        return backends.get(name)
    except backends.BackendUnavailable as exc:
        pytest.skip(str(exc))


def _switch(sc: Scenario) -> SwitchNode:
    (sw,) = (n for n in sc.nodes if isinstance(n, SwitchNode))
    return sw


# --- the parameter ---


def test_a_switch_is_unbounded_by_default() -> None:
    assert _switch(_cell(None).to_scenario(duration=10)).buffer_bytes is None
    assert SwitchNode("sw", 8).buffer_bytes is None


def test_the_buffer_is_in_bytes() -> None:
    assert _switch(_cell(115_000).to_scenario(duration=10)).buffer_bytes == 115_000


@pytest.mark.parametrize("buffer", [0, -1, 1_521, 1_522.5, "115kB", True])
def test_a_buffer_below_one_maximum_frame_or_not_whole_bytes_is_rejected(buffer) -> None:
    with pytest.raises(ScenarioError, match="buffer must be a whole number of bytes"):
        Network("n").switch("sw", ports=2, buffer=buffer)


def test_one_maximum_frame_is_enough() -> None:
    validate(_cell(1_522).to_scenario(duration=10))


def test_a_buffer_smaller_than_the_largest_frame_is_rejected() -> None:
    sc = _cell(2_000, size=9_000).to_scenario(duration=10)
    with pytest.raises(
        ScenarioError, match="smaller than the largest frame of the scenario, 9000 B"
    ):
        validate(sc)
    validate(_cell(9_000, size=9_000).to_scenario(duration=10))


def test_a_priority_flow_counts_its_tag() -> None:
    net = _cell(1_522, tagged=1_522)  # 1 526 B with its tag
    with pytest.raises(ScenarioError, match="largest frame of the scenario, 1526 B"):
        validate(net.to_scenario(duration=10))


# --- .smol ---


_SMOL = """
network "cell" {
    switch  sw ports=2 %s
    adapter a ip=10.0.0.1
    adapter b ip=10.0.0.2
    link a -- sw speed=1000 length=5
    link b -- sw speed=100 length=5
    flow a -> b rate=1000 size=1518
    observe throughput on b every=10
    simulate duration=100 mode=headless
}
"""


@pytest.mark.parametrize(
    "arg, buffer",
    [
        ("", None),
        ("buffer=115000", 115_000),
        ("buffer=115kB", 115_000),
        ("buffer=1.5MB", 1_500_000),
        ("buffer=112KiB", 114_688),
        ("buffer=1MiB", 1_048_576),
        ("buffer=1.15e5", 115_000),
    ],
)
def test_smol_takes_bytes_with_a_suffix(tmp_path, arg: str, buffer: int | None) -> None:
    path = tmp_path / "cell.smol"
    path.write_text(_SMOL % arg)
    assert _switch(interpret_to_scenario(path)).buffer_bytes == buffer


@pytest.mark.parametrize("arg", ["buffer=115xB", "buffer=1.0001kB", "buffer=1000"])
def test_smol_rejects_a_bad_buffer(tmp_path, arg: str) -> None:
    path = tmp_path / "cell.smol"
    path.write_text(_SMOL % arg)
    with pytest.raises(SMOLSemanticError, match="buffer must be a whole number of bytes"):
        interpret_to_scenario(path)


# --- the IR ---


def test_the_buffer_round_trips_through_json() -> None:
    sc = _cell().to_scenario(duration=10)
    data = sc.to_dict()
    assert data["version"] == 2
    assert data["nodes"][0]["buffer_bytes"] == 115_000
    assert Scenario.from_json(sc.to_json()) == sc


def test_an_unbounded_switch_is_written_as_before() -> None:
    """A document without buffers is what it was before ADR-0011, so readers
    that do not know the field still read it."""
    data = _cell(None).to_scenario(duration=10).to_dict()
    assert data["nodes"][0] == {
        "kind": "switch",
        "name": "sw",
        "ports": 3,
        "mode": "store-and-forward",
        "qos": "strict",
        "mac_table": "learning",
    }
    assert Scenario.from_dict(data) == _cell(None).to_scenario(duration=10)


# --- capabilities ---


@pytest.mark.parametrize("name", ["simpy", "rust", "ns3"])
def test_a_backend_without_the_feature_rejects_a_bounded_switch(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _backend(name)
    caps = b.capabilities()
    if "bounded_buffer" in caps.features:
        monkeypatch.setattr(
            type(b),
            "capabilities",
            lambda self: type(caps)(
                **{**caps.__dict__, "features": caps.features - {"bounded_buffer"}}
            ),
        )
    with pytest.raises(
        ScenarioError, match="Switch 'sw': the .* backend does not support bounded switch buffers"
    ):
        backends.check(_cell().to_scenario(duration=10), name)


@pytest.mark.parametrize("name", ["simpy", "rust", "ns3"])
def test_an_unbounded_switch_runs_as_before(name: str) -> None:
    _backend(name)
    backends.check(_cell(None).to_scenario(duration=10), name)


# --- step 2: the reference engine ---

SIZE = 1_518


def _bottleneck(
    buffer: int | None,
    rho: float = 0.996,
    duration: float = 2_000,
    every: float = 100,
) -> tuple[Network, dict]:
    """Poisson 1518 B frames from a 1 Gb/s port to a 100 Mb/s one, at load
    *rho* of the 100 Mb/s port: the testbed's bottleneck."""
    net = Network("bottleneck")
    sw = net.switch("sw", ports=2, buffer=buffer)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=rho * 100e6 / ((SIZE + 20) * 8), size=SIZE, pattern="poisson")
    for metric, node in (
        ("drops", sw),
        ("drops_total", sw),
        ("frames_sent", a),
        ("frames_received", b),
        ("queue_depth", sw),
        ("latency", b),
    ):
        net.observe(metric, on=node, every=every)
    return net, {"duration": duration}


def _values(result, key: str) -> list[float]:
    return [v for _, v in result.metrics[key]]


def test_a_buffer_that_is_never_full_changes_nothing() -> None:
    """Same samples and the same number of events as an unbounded switch."""
    net, kw = _bottleneck(None, rho=0.6)
    free = net.simulate(**kw)
    net, kw = _bottleneck(10_000_000, rho=0.6)
    bounded = net.simulate(**kw)
    assert bounded.metrics == free.metrics
    assert bounded.stats.events == free.stats.events
    assert set(_values(bounded, "drops_total:sw")) == {0.0}


class _Admissions:
    """Every enqueue on a bounded port: the bytes queued before it, the
    frame and whether it was dropped."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records: list[tuple[int, int, int, engine._Frame, bool]] = []
        self.ports: list[engine._LinkChannel] = []  # the port of each record
        original = engine._LinkChannel.enqueue

        def enqueue(ch: engine._LinkChannel, frame: engine._Frame) -> None:
            if ch.buffer_bytes is None:
                return original(ch, frame)
            queued = ch.queue.queued_bytes  # type: ignore[attr-defined]
            # the count is the frame bytes of the frames in the store
            assert queued == sum(item.item.size_bytes for item in ch.queue.items)
            before = ch.frames_dropped
            original(ch, frame)
            self.records.append(
                (queued, frame.size_bytes, ch.buffer_bytes, frame, ch.frames_dropped > before)
            )
            self.ports.append(ch)

        monkeypatch.setattr(engine._LinkChannel, "enqueue", enqueue)


def _lossy_plant() -> Network:
    """Two learning switches, so frames to b are flooded at sw1 (b never
    sends); best effort and priority frames; two sources whose frames
    reach sw1 at the same instants."""
    net = Network("lossy")
    sw1 = net.switch("sw1", ports=4, buffer=6 * SIZE)
    sw2 = net.switch("sw2", ports=2, buffer=4 * SIZE, qos="fifo")
    a = net.adapter("a", ip="10.0.0.1")
    d = net.adapter("d", ip="10.0.0.4")
    c = net.adapter("c", ip="10.0.0.3")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw1, speed=1_000, length=5)
    net.link(d, sw1, speed=1_000, length=5)
    net.link(c, sw1, speed=100, length=5)
    net.link(sw1, sw2, speed=100, length=50)
    net.link(b, sw2, speed=100, length=5)
    a.sends(b, rate=4_500, size=SIZE, pattern="poisson")
    a.sends(b, rate=2_000, size=200, priority=6)
    d.sends(b, rate=2_000, size=200, priority=6)  # with a's: the same instants
    d.sends(c, rate=3_000, size=SIZE, pattern="bursty")
    net.observe("drops_total", on=sw1, every=50)
    return net


def test_a_frame_is_dropped_exactly_when_it_would_exceed_the_buffer(monkeypatch) -> None:
    seen = _Admissions(monkeypatch)
    _lossy_plant().simulate(duration=300)
    assert seen.records
    for queued, size, buffer, _, dropped in seen.records:
        assert dropped == (queued + size > buffer)
    dropped = [f for *_, f, d in seen.records if d]
    kept = [f for *_, f, d in seen.records if not d]
    assert dropped and kept
    # whatever its priority: tail drop takes priority frames too
    assert any(f.priority == 6 for f in dropped)
    # sw1 floods frames to b, which it never learns: the copies on c's port
    # (5 m, sw1's buffer) are admitted or dropped on their own
    to_c = [
        d
        for (*_, f, d), ch in zip(seen.records, seen.ports, strict=True)
        if f.dst_mac == "02:00:0a:00:00:02"
        and ch.prop_delay_ns == 25
        and ch.buffer_bytes == 6 * SIZE
    ]
    assert any(to_c) and not all(to_c)


def test_the_queue_never_holds_more_than_its_buffer(monkeypatch) -> None:
    seen = _Admissions(monkeypatch)
    _lossy_plant().simulate(duration=300)
    for queued, size, buffer, _, dropped in seen.records:
        assert queued <= buffer
        assert dropped or queued + size <= buffer


def test_queueing_delay_is_bounded_by_the_buffer(monkeypatch) -> None:
    """ADR-0011: a frame waits at most tx(q + 20 n) for the frames ahead of
    it, plus one frame in transmission and its gap."""
    latencies: list[int] = []
    original = engine._AdapterCounters.record

    def record(self, frame, now: int) -> None:
        latencies.append(now - frame.created_at_ns)
        original(self, frame, now)

    monkeypatch.setattr(engine._AdapterCounters, "record", record)
    buffer = 75 * (SIZE + 20)  # 115 350 B: 75 frames
    net, kw = _bottleneck(buffer, rho=1.2, duration=1_000)
    result = net.simulate(**kw)
    assert sum(_values(result, "drops:sw")) > 0  # the buffer was full
    n = buffer // SIZE
    wait = (
        tb.transmission_ns(n * SIZE + 20 * n, 100)
        + tb.transmission_ns(8 + SIZE, 100)
        + tb.transmission_ns(12, 100)
    )
    own = tb.transmission_ns(8 + SIZE, 1_000) + tb.transmission_ns(8 + SIZE, 100)
    bound = wait + own + 2 * tb.propagation_ns(5)
    assert max(latencies) <= bound
    assert max(latencies) > 0.95 * bound  # and it is reached: the buffer caps the delay


def test_loss_is_frames_sent_less_frames_received() -> None:
    """At every sample: drops_total = frames_sent - frames_received - in
    flight, with at most the buffer and a frame on each link in flight."""
    buffer = 20 * SIZE
    net, kw = _bottleneck(buffer, rho=1.1, duration=1_000, every=10)
    result = net.simulate(**kw)
    sent = _values(result, "frames_sent:a")
    received = _values(result, "frames_received:b")
    drops = _values(result, "drops_total:sw")
    window = _values(result, "drops:sw")
    assert drops[-1] > 0
    for s, r, d in zip(sent, received, drops, strict=True):
        assert 0 <= s - r - d <= buffer // SIZE + 3
    # drops is drops_total by window
    assert [d - p for d, p in zip(drops, [0.0, *drops[:-1]], strict=True)] == window
    assert max(_values(result, "queue_depth:sw")) <= buffer // SIZE


def test_without_a_buffer_nothing_is_dropped_and_delay_grows() -> None:
    net, kw = _bottleneck(None, rho=1.1, duration=1_000)
    free = net.simulate(**kw)
    net, kw = _bottleneck(115_000, rho=1.1, duration=1_000)
    bounded = net.simulate(**kw)
    assert set(_values(free, "drops_total:sw")) == {0.0}
    assert max(_values(free, "latency:b")) > 5 * max(_values(bounded, "latency:b"))


def _polled(buffer: int | None) -> Network:
    """The testbed: UDP from the Pi, and the laptop polling a Modbus server
    on the Pi every 10 ms, through the laptop's 100 Mb/s port."""
    net = Network("testbed")
    sw = net.switch("sw", ports=3, buffer=buffer)
    pi = net.adapter("pi", ip="10.0.0.1")
    server = net.modbus_slave("server", ip="10.0.0.3", unit_id=1)
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=1_000, length=2)
    net.link(server, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=100, length=2)
    pi.sends(laptop, rate=1.02 * 100e6 / ((SIZE + 20) * 8), size=SIZE, pattern="poisson")
    laptop.polls(server, register=0, count=10, rate=100)
    net.observe("modbus_unanswered", on=laptop, every=100)
    net.observe("modbus_latency", on=laptop, every=100)
    net.observe("drops_total", on=sw, every=100)
    return net


def test_a_lost_poll_stays_unanswered(monkeypatch) -> None:
    # With 115 000 B, 75 frames of 1 518 B leave 1 150 B, and a response
    # (86 B) always fits: tail drop by bytes drops only the large frames. With
    # 50 B left over, a response finds no room when the queue is full.
    seen = _Admissions(monkeypatch)
    result = _polled(75 * SIZE + 50).simulate(duration=3_000)
    lost = sum(1 for *_, f, d in seen.records if d and f.modbus_unit_id is not None)
    unanswered = _values(result, "modbus_unanswered:laptop")
    assert lost > 0
    # each lost poll lost exactly one frame, its request or its response;
    # the rest are polls in flight
    assert 0 <= unanswered[-1] - lost <= 1
    assert max(_values(result, "modbus_latency:laptop")) < 10_000  # µs: capped by the buffer


def test_a_buffer_in_bytes_keeps_room_for_small_frames(monkeypatch) -> None:
    seen = _Admissions(monkeypatch)
    result = _polled(115_000).simulate(duration=3_000)
    assert any(d for *_, d in seen.records)
    assert not any(d for *_, f, d in seen.records if f.modbus_unit_id is not None)
    assert _values(result, "modbus_unanswered:laptop")[-1] <= 1


def test_without_loss_every_poll_is_answered() -> None:
    net = _polled(None)
    result = net.simulate(duration=500)
    assert set(_values(result, "modbus_unanswered:laptop")) <= {0.0, 1.0}


def test_a_lost_profinet_frame_expires_the_watchdog() -> None:
    def plant(buffer: int | None) -> Network:
        net = Network("pn")
        sw = net.switch("sw", ports=3, buffer=buffer)
        plc = net.adapter("plc", ip="10.0.0.1")
        io = net.profinet_device("io", ip="10.0.0.2")
        cam = net.adapter("cam", ip="10.0.0.3")
        net.link(plc, sw, speed=100, length=5)
        net.link(io, sw, speed=100, length=5)
        net.link(cam, sw, speed=1_000, length=5)
        plc.controls(io, cycle=1, inputs=20, outputs=20, hold=2)  # 1 cycle: jitter alone expires it
        # the camera overloads the controller's port, which carries the inputs
        cam.sends(plc, rate=1.3 * 100e6 / ((SIZE + 20) * 8), size=SIZE, pattern="poisson")
        net.observe("pn_watchdog_expiries", on=plc, every=100)
        return net

    free = plant(None).simulate(duration=500)
    bounded = plant(4 * SIZE).simulate(duration=500)
    # strict priority keeps PROFINET on time while nothing is dropped
    assert _values(free, "pn_watchdog_expiries:plc")[-1] == 0
    # a full buffer drops it whatever its priority, and the watchdog expires
    assert _values(bounded, "pn_watchdog_expiries:plc")[-1] > 0


# --- step 2: M/D/1/K ---


def md1k(rho: float, k: int) -> tuple[float, float]:
    """Blocking probability and mean time in system (in service times) of an
    M/D/1/K queue, from the embedded Markov chain at departures."""
    a = [math.exp(-rho) * rho**j / math.factorial(j) for j in range(k + 1)]
    # P[i][j]: from i left behind by a departure to j left by the next
    p = [[0.0] * k for _ in range(k)]
    for i in range(k):
        start = max(i - 1, 0)
        for j in range(start, k - 1):
            p[i][j] = a[j - start]
        p[i][k - 1] = 1 - sum(p[i][: k - 1])
    # pi (P - I) = 0 with sum(pi) = 1, by Gauss-Jordan elimination
    m = [[p[j][i] - (i == j) for j in range(k)] for i in range(k)]
    m[-1] = [1.0] * k
    rhs = [0.0] * (k - 1) + [1.0]
    for c in range(k):
        r = max(range(c, k), key=lambda r: abs(m[r][c]))
        m[c], m[r], rhs[c], rhs[r] = m[r], m[c], rhs[r], rhs[c]
        for r in range(k):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
                rhs[r] -= f * rhs[c]
    pi = [rhs[i] / m[i][i] for i in range(k)]
    # time averages: p_j = pi_j / (pi_0 + rho) below K; p_K = 1 - 1 / (pi_0 + rho)
    occupancy = [x / (pi[0] + rho) for x in pi] + [1 - 1 / (pi[0] + rho)]
    blocking = occupancy[k]
    in_system = sum(j * x for j, x in enumerate(occupancy))
    return blocking, in_system / (rho * (1 - blocking))  # Little's law


def test_md1k_solution() -> None:
    # K = 1 is a loss system: blocking rho / (1 + rho), time in system one service
    blocking, w = md1k(0.5, 1)
    assert blocking == pytest.approx(1 / 3)
    assert w == pytest.approx(1)
    # a large K tends to M/D/1: W = 1 + rho / (2 (1 - rho))
    blocking, w = md1k(0.5, 60)
    assert blocking < 1e-12
    assert w == pytest.approx(1.5)


JUMBO = 9_000  # frames: the gap is 0.13 % of a service time


def _md1k_run(rho: float, k: int, seed: int, frames: int = 40_000) -> tuple[float, float]:
    """One port of a switch as an M/D/1/K queue: Poisson arrivals of fixed
    size from a 100 Gb/s port (its own queueing is negligible), served at
    1 Gb/s with K - 1 frames of buffer. Returns the share of frames dropped
    and the mean time in system, in service times."""
    service = tb.transmission_ns(8 + JUMBO, 1_000) + tb.transmission_ns(12, 1_000)
    rate = rho * 1e9 / service
    duration = frames / rate * 1_000
    net = Network("md1k")
    sw = net.switch("sw", ports=2, buffer=(k - 1) * JUMBO)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100_000, length=5)
    net.link(b, sw, speed=1_000, length=5)
    a.sends(b, rate=rate, size=JUMBO, pattern="poisson")
    every = 0.999 * duration  # one window: (almost) the whole run
    for metric, node in (("latency", b), ("frames_sent", a), ("drops_total", sw)):
        net.observe(metric, on=node, every=every)
    result = net.simulate(duration=duration, seed=seed)
    latency_ns = result.metrics["latency:b"][0][1] * 1_000
    in_system = latency_ns - tb.transmission_ns(8 + JUMBO, 100_000) - 2 * tb.propagation_ns(5)
    dropped = result.metrics["drops_total:sw"][0][1] / result.metrics["frames_sent:a"][0][1]
    return dropped, in_system / service


@pytest.mark.parametrize("rho, k", [(0.9, 5), (1.3, 3)])
def test_one_port_is_an_md1k_queue(rho: float, k: int) -> None:
    """Blocking probability and mean time in system against M/D/1/K.

    Five seeds of 40 000 frames each. The tolerance is four standard errors
    of the mean over the seeds, plus 0.5 % for what the model does
    differently: the first frame of a busy period finds less than the
    inter-frame gap left to wait, which is at most 0.13 % of a service time
    with 9 000 B frames."""
    blocking, w = md1k(rho, k)
    runs = [_md1k_run(rho, k, seed) for seed in range(1, 6)]
    for got, want in ((runs_of(runs, 0), blocking), (runs_of(runs, 1), w)):
        mean = statistics.mean(got)
        error = statistics.stdev(got) / math.sqrt(len(got))
        assert abs(mean - want) <= 4 * error + 0.005 * want, (mean, want, error)


def runs_of(runs: list[tuple[float, float]], i: int) -> list[float]:
    return [r[i] for r in runs]


# --- step 3: Rust, bit for bit ---


def _deterministic_losses() -> dict[str, Scenario]:
    from tests.equivalence.scenarios import lossy_protocols, tail_drop

    def bottleneck() -> Scenario:
        """Four constant sources reach one full port at the same instants;
        flooded copies; a priority flow; three bounded switches."""
        net = Network("ties")
        core = net.switch("core", ports=6, buffer=3 * SIZE + 64)
        edge = net.switch("edge", ports=3, buffer=2 * SIZE, qos="fifo")
        far = net.switch("far", ports=2, buffer=1_522)
        sink = net.adapter("sink", ip="10.0.0.1")
        hosts = [net.adapter(f"h{i}", ip=f"10.0.0.{i + 2}") for i in range(4)]
        for h in hosts:
            net.link(h, core, speed=1_000, length=5)
        net.link(sink, core, speed=100, length=5)
        net.link(core, edge, speed=100, length=5)
        lone = net.adapter("lone", ip="10.0.0.9")
        net.link(edge, far, speed=10, length=5)
        net.link(lone, far, speed=10, length=5)
        for i, h in enumerate(hosts):
            h.sends(sink, rate=2_500, size=SIZE)  # all four: the same instants
            h.sends(lone, rate=300, size=64 + 100 * i, priority=i)
        net.observe("drops", on=core, every=1)
        for sw in (core, edge, far):
            net.observe("drops_total", on=sw, every=1)
            net.observe("queue_depth", on=sw, every=0.5)
        net.observe("frames_received", on=sink, every=1)
        net.observe("frames_received", on=lone, every=1)
        net.observe("latency", on=lone, every=1)
        return net.to_scenario(duration=30)

    return {
        "ties": bottleneck(),
        "tail-drop": tail_drop(),
        "lossy-protocols": lossy_protocols(),
        "testbed": _polled_constant(),
    }


def _polled_constant() -> Scenario:
    """The testbed with a constant UDP stream instead of Poisson: Rust draws
    random numbers from its own generator, so only deterministic traffic can
    be compared bit for bit."""
    import dataclasses

    from smolpy.model import Flow

    sc = _polled(75 * SIZE + 50).to_scenario(duration=1_500)
    traffic = tuple(
        dataclasses.replace(t, pattern="constant") if isinstance(t, Flow) else t for t in sc.traffic
    )
    return dataclasses.replace(sc, traffic=traffic)


@pytest.mark.parametrize("name", ["ties", "tail-drop", "lossy-protocols", "testbed"])
def test_rust_drops_what_simpy_drops(name: str) -> None:
    _backend("rust")
    sc = _deterministic_losses()[name]
    simpy = backends.run(sc, "simpy")
    rust = backends.run(sc, "rust")
    assert any(v for k, s in simpy.metrics.items() if k.startswith("drops_total") for _, v in s), (
        "the scenario drops frames"
    )
    assert rust.metrics == simpy.metrics
    assert rust.stats.events == simpy.stats.events
