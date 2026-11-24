"""Modbus polling with one request in flight (ADR-0012).

- Step 1: the scenario. ``outstanding`` and ``timeout`` in the Python API and
  in .smol, their validation, the IR (still version 2: optional fields,
  written only when set) and the capability that a backend needs to run them.
- Step 2: the reference engine. Without loss and with an RTT under the period
  nothing changes; poll instants are skipped while a request is outstanding,
  and at the instant one completes; timeouts fire exactly at their instant;
  late responses are discarded; the counts are consistent.
- Step 3: the Rust engine. The same tests where they need no SimPy internals,
  and SimPy's results bit for bit with loss.
- Step 4: ns-3. Format 3 and the TCP settings; the same rules over real TCP,
  which retransmits a lost response after Linux's RTO, as on the testbed.
"""

from __future__ import annotations

import pytest

from smolpy import Network, backends, ns3
from smolpy.lang import SMOLSemanticError
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Poll, Scenario
from smolpy.model import timebase as tb
from smolpy.model.validate import ScenarioError, validate
from smolpy.sim import engine


def _master(
    outstanding: int | None = 1,
    timeout_ms: float | None = 50.0,
    rate: float = 100,
    speed: float = 100,
) -> Network:
    """A master polling a slave across one switch."""
    net = Network("master")
    sw = net.switch("sw", ports=2)
    m = net.adapter("m", ip="10.0.0.1")
    s = net.modbus_slave("s", ip="10.0.0.2", unit_id=1)
    net.link(m, sw, speed=speed, length=5)
    net.link(s, sw, speed=speed, length=5)
    m.polls(s, register=0, count=10, rate=rate, outstanding=outstanding, timeout_ms=timeout_ms)
    net.observe("modbus_latency", on=m, every=10)
    return net


def _backend(name: str) -> backends.Backend:
    try:
        return backends.get(name)
    except backends.BackendUnavailable as exc:
        pytest.skip(str(exc))


def _poll(sc: Scenario) -> Poll:
    (p,) = (t for t in sc.traffic if isinstance(t, Poll))
    return p


# --- the parameters ---


def test_a_poll_is_open_loop_by_default() -> None:
    p = _poll(_master(None, None).to_scenario(duration=10))
    assert (p.outstanding, p.timeout_ms) == (None, None)
    assert Poll("m", "s", 0, 1).outstanding is None


def test_one_request_in_flight_with_a_timeout() -> None:
    p = _poll(_master(1, 50.0).to_scenario(duration=10))
    assert (p.outstanding, p.timeout_ms) == (1, 50.0)


def test_one_request_in_flight_without_a_timeout() -> None:
    validate(_master(1, None).to_scenario(duration=10))


@pytest.mark.parametrize("outstanding", [0, 2, -1, 1.0, True, "1"])
def test_outstanding_other_than_one_is_rejected(outstanding) -> None:
    with pytest.raises(ScenarioError, match="outstanding must be 1 or unlimited"):
        _master(outstanding, None)


@pytest.mark.parametrize("timeout", [0, -5.0, float("inf"), float("nan"), 1e-7, True, "50"])
def test_a_bad_timeout_is_rejected(timeout) -> None:
    with pytest.raises(ScenarioError, match="timeout must be"):
        _master(1, timeout)


def test_one_nanosecond_is_the_shortest_timeout() -> None:
    validate(_master(1, 1e-6).to_scenario(duration=10))


def test_a_timeout_fits_in_the_time_base() -> None:
    """As a duration: 2^63 - 1 ns, which every engine can hold."""
    validate(_master(1, 9.2e12).to_scenario(duration=10))
    with pytest.raises(ScenarioError, match=r"timeout must be at most 2\^63 - 1 ns"):
        _master(1, 9.3e12)


def test_a_timeout_needs_one_request_in_flight() -> None:
    with pytest.raises(ScenarioError, match=r"poll m -> s: timeout needs outstanding=1"):
        _master(None, 50.0)


def test_a_pair_with_one_request_in_flight_has_one_poll() -> None:
    net = _master(1, 50.0)
    m, s = net._nodes["m"], net._nodes["s"]
    m.polls(s, register=100, count=5, rate=10)
    with pytest.raises(ScenarioError, match="'m' polls 's' more than once; with outstanding=1"):
        validate(net.to_scenario(duration=10))


def test_open_loop_polls_of_one_pair_may_be_several() -> None:
    net = _master(None, None)
    m, s = net._nodes["m"], net._nodes["s"]
    m.polls(s, register=100, count=5, rate=10)
    validate(net.to_scenario(duration=10))


# --- .smol ---


_SMOL = """
network "master" {
    switch       sw ports=2
    adapter      m  ip=10.0.0.1
    modbus_slave s  ip=10.0.0.2 unit_id=1
    link m -- sw speed=100 length=5
    link s -- sw speed=100 length=5
    poll m -> s register=0 count=10 rate=100 %s
    observe modbus_latency on m every=10
    simulate duration=100 mode=headless
}
"""


@pytest.mark.parametrize(
    "args, outstanding, timeout",
    [
        ("", None, None),
        ("outstanding=unlimited", None, None),
        ("outstanding=1", 1, None),
        ("outstanding=1 timeout=50", 1, 50.0),
        ("outstanding=1 timeout=0.5", 1, 0.5),
    ],
)
def test_smol_takes_outstanding_and_timeout(
    tmp_path, args: str, outstanding: int | None, timeout: float | None
) -> None:
    path = tmp_path / "master.smol"
    path.write_text(_SMOL % args)
    p = _poll(interpret_to_scenario(path))
    assert (p.outstanding, p.timeout_ms) == (outstanding, timeout)


@pytest.mark.parametrize(
    "args, message",
    [
        ("outstanding=2", "outstanding must be 1 or unlimited"),
        ("outstanding=many", "outstanding must be 1 or unlimited"),
        ("timeout=50", "timeout needs outstanding=1"),
        ("outstanding=1 timeout=0", "timeout must be a positive number"),
        ("outstanding=1 timeout=soon", "timeout must be a positive number"),
    ],
)
def test_smol_rejects_bad_values(tmp_path, args: str, message: str) -> None:
    path = tmp_path / "master.smol"
    path.write_text(_SMOL % args)
    with pytest.raises(SMOLSemanticError, match=message):
        interpret_to_scenario(path)


# --- the IR ---


def test_the_fields_round_trip_through_json() -> None:
    sc = _master(1, 50.0).to_scenario(duration=10)
    data = sc.to_dict()
    assert data["version"] == 2
    assert (data["traffic"][0]["outstanding"], data["traffic"][0]["timeout_ms"]) == (1, 50.0)
    assert Scenario.from_json(sc.to_json()) == sc


def test_an_open_loop_poll_is_written_as_before() -> None:
    """A document without the fields is what it was before ADR-0012, so
    readers that do not know them still read it."""
    data = _master(None, None).to_scenario(duration=10).to_dict()
    assert data["traffic"][0] == {
        "kind": "poll",
        "source": "m",
        "slave": "s",
        "register": 0,
        "count": 10,
        "rate": 100,
        "delay_ms": 0.0,
    }
    assert Scenario.from_dict(data) == _master(None, None).to_scenario(duration=10)


# --- capabilities ---


@pytest.mark.parametrize("name", ["simpy", "rust", "ns3"])
def test_a_backend_without_the_feature_rejects_one_request_in_flight(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _backend(name)
    caps = b.capabilities()
    if "modbus_outstanding" in caps.features:
        monkeypatch.setattr(
            type(b),
            "capabilities",
            lambda self: type(caps)(
                **{**caps.__dict__, "features": caps.features - {"modbus_outstanding"}}
            ),
        )
    with pytest.raises(
        ScenarioError,
        match="poll m -> s: the .* backend does not support polls with one request in flight",
    ):
        backends.check(_master().to_scenario(duration=10), name)


@pytest.mark.parametrize("name", ["simpy", "rust", "ns3"])
def test_an_open_loop_poll_runs_as_before(name: str) -> None:
    _backend(name)
    backends.check(_master(None, None).to_scenario(duration=10), name)


# --- step 2: the reference engine ---

# Two 100 Mb/s hops of 632 m each way: a request (69 B) and a response of ten
# registers (86 B) take 2 x 6 160 + 2 x 7 520 ns on the wire and 4 x 3 160 ns
# of propagation, so the round trip is exactly 40 us.
LONG = 632
RTT_NS = 40_000
COUNTS = ("modbus_unanswered", "modbus_timeouts", "modbus_skipped", "frames_received")


def _pair(
    rate: float,
    outstanding: int | None = 1,
    timeout_ms: float | None = None,
    length: float = LONG,
    duration: float = 1.0,
) -> Scenario:
    """A master polling a slave across one switch, sampled once at the end."""
    net = Network("pair")
    sw = net.switch("sw", ports=2)
    m = net.adapter("m", ip="10.0.0.1")
    s = net.modbus_slave("s", ip="10.0.0.2", unit_id=1)
    net.link(m, sw, speed=100, length=length)
    net.link(s, sw, speed=100, length=length)
    m.polls(s, register=0, count=10, rate=rate, outstanding=outstanding, timeout_ms=timeout_ms)
    end = duration - 0.001
    for metric in ("modbus_latency", *COUNTS):
        net.observe(metric, on=m, every=end)  # type: ignore[arg-type]
    return net.to_scenario(duration=duration)


ENGINES = ["simpy", "rust"]


def _final(sc: Scenario, engine: str = "simpy") -> dict[str, float | None]:
    """Each metric's one sample, None for an empty window."""
    result = backends.run(sc, _backend(engine).name)
    return {k.split(":")[0]: (v[-1][1] if v else None) for k, v in result.metrics.items()}


def _instants(rate: float, duration_ms: float, delay_ms: float = 0.0) -> int:
    """Poll instants in [0, duration)."""
    end = tb.ms_to_ns(duration_ms)
    n = 0
    for t in tb.instants(tb.ms_to_ns(delay_ms), tb.interval_ns(rate)):
        if t >= end:
            return n
        n += 1
    raise AssertionError("unreachable")


def _shared_port(outstanding: int | None, timeout_ms: float | None) -> Scenario:
    """Polls to two slaves sharing the master's port with UDP traffic, every
    round trip far under the period."""
    net = Network("shared")
    sw = net.switch("sw", ports=4)
    m = net.adapter("m", ip="10.0.0.1")
    cam = net.adapter("cam", ip="10.0.0.4")
    for node in (m, cam):
        net.link(node, sw, speed=100, length=20)
    for i, (rate, count) in enumerate(((100, 10), (250, 60)), start=2):
        s = net.modbus_slave(f"s{i}", ip=f"10.0.0.{i}", unit_id=i)
        net.link(s, sw, speed=100, length=20)
        m.polls(
            s, register=0, count=count, rate=rate, outstanding=outstanding, timeout_ms=timeout_ms
        )
    cam.sends(m, rate=4_000, size=1_200, pattern="poisson")
    for metric in ("modbus_latency", "modbus_unanswered", "latency", "throughput"):
        net.observe(metric, on=m, every=5)  # type: ignore[arg-type]
    return net.to_scenario(duration=500, seed=3)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("timeout_ms", [None, 5.0])
def test_without_loss_and_under_the_period_nothing_changes(
    timeout_ms: float | None, engine: str
) -> None:
    b = _backend(engine)
    open_loop = b.run(_shared_port(None, None))
    closed = b.run(_shared_port(1, timeout_ms))
    assert closed.metrics == open_loop.metrics
    if timeout_ms is None:  # a timeout is an event of its own
        assert closed.stats.events == open_loop.stats.events


@pytest.mark.parametrize("engine", ENGINES)
def test_an_instant_is_skipped_while_a_request_is_outstanding(engine: str) -> None:
    """RTT = 2.5 periods: each request is sent at instant 3k + 1, and the two
    instants after it are skipped."""
    rate = 62_500  # 16 us
    final = _final(_pair(rate), engine)
    n = _instants(rate, 1.0)
    sent = len(range(1, n + 1, 3))
    assert (n, sent) == (62, 21)
    assert final["modbus_skipped"] == n - sent
    assert final["modbus_latency"] == RTT_NS / 1_000  # every RTT is exact
    # the last request, sent at 976 us, is in flight at the end
    assert final["modbus_unanswered"] == 1
    assert final["frames_received"] == sent - 1


@pytest.mark.parametrize("engine", ENGINES)
def test_open_loop_sends_at_every_instant(engine: str) -> None:
    final = _final(_pair(62_500, outstanding=None), engine)
    assert final["modbus_skipped"] == 0
    assert final["frames_received"] == 59  # sent at 16k us, answered 40 us later


@pytest.mark.parametrize("engine", ENGINES)
def test_a_response_at_a_poll_instant_does_not_free_it(engine: str) -> None:
    """RTT = period: each response arrives at the next poll instant, which is
    skipped, so every other instant sends."""
    rate = 25_000  # 40 us
    final = _final(_pair(rate), engine)
    n = _instants(rate, 1.0)
    assert final["modbus_skipped"] == n // 2
    assert final["frames_received"] == n - n // 2
    assert final["modbus_unanswered"] == 0
    assert final["modbus_latency"] == RTT_NS / 1_000


@pytest.mark.parametrize(
    "timeout_ms, timeouts",
    [
        (0.04, 9),  # a response at exactly s + timeout is late
        (0.040001, 0),  # 1 ns later it is in time
    ],
)
@pytest.mark.parametrize("engine", ENGINES)
def test_a_response_at_the_timeout_is_late(timeout_ms: float, timeouts: int, engine: str) -> None:
    final = _final(_pair(10_000, timeout_ms=timeout_ms), engine)  # one request per 100 us
    assert final["modbus_timeouts"] == timeouts
    assert final["frames_received"] == 9  # the NIC accepts late responses too
    assert final["modbus_unanswered"] == timeouts
    assert final["modbus_latency"] == (None if timeouts else RTT_NS / 1_000)


@pytest.mark.parametrize(
    "timeout_ms, skipped",
    [
        (0.02, 24),  # the timeout falls on the next poll instant, which is skipped
        (0.019999, 0),  # 1 ns earlier, the next instant sends
    ],
)
@pytest.mark.parametrize("engine", ENGINES)
def test_a_timeout_fires_exactly_after_its_time(
    timeout_ms: float, skipped: int, engine: str
) -> None:
    """Period 20 us, RTT 40 us: every request times out, and the timeout
    instant decides whether the next poll instant is free."""
    rate = 50_000
    final = _final(_pair(rate, timeout_ms=timeout_ms), engine)
    n = _instants(rate, 1.0)
    sent = n - skipped
    assert final["modbus_skipped"] == skipped
    # the last request is outstanding at the end; all others timed out
    assert final["modbus_timeouts"] == sent - 1
    assert final["modbus_unanswered"] == sent
    assert final["modbus_latency"] is None  # late responses are not counted


def _lossy(timeout_ms: float | None, every: float = 1.0) -> Network:
    """Polls through a bounded port that a constant flow overloads: some
    responses are lost, some are late."""
    net = Network("lossy")
    sw = net.switch("sw", ports=4, buffer=6 * 1_500 + 40)
    m = net.adapter("m", ip="10.0.0.1")
    s = net.modbus_slave("s", ip="10.0.0.2", unit_id=1)
    cam = net.adapter("cam", ip="10.0.0.3")
    net.link(m, sw, speed=100, length=20)
    net.link(s, sw, speed=1_000, length=20)
    net.link(cam, sw, speed=1_000, length=20)
    cam.sends(m, rate=9_000, size=1_500)  # 110 % of the master's port
    m.polls(s, register=0, count=20, rate=500, outstanding=1, timeout_ms=timeout_ms)
    for metric in ("modbus_latency", *COUNTS):
        net.observe(metric, on=m, every=every)  # type: ignore[arg-type]
    return net


class _Recorded(engine._AdapterCounters):
    made: list[engine._AdapterCounters] = []

    def __init__(self, *args: object) -> None:
        super().__init__(*args)  # type: ignore[arg-type]
        self.made.append(self)


def _counters(sc: Scenario, monkeypatch: pytest.MonkeyPatch) -> engine._AdapterCounters:
    """The master's counters at the end of a run."""
    _Recorded.made = []
    monkeypatch.setattr(engine, "_AdapterCounters", _Recorded)
    backends.get("simpy").run(sc)
    return next(c for c in _Recorded.made if c.transactions)


@pytest.mark.parametrize("timeout_ms", [3.0, 1.0, 0.5])
def test_the_counts_are_consistent(timeout_ms: float, monkeypatch: pytest.MonkeyPatch) -> None:
    sc = _lossy(timeout_ms).to_scenario(duration=200)
    c = _counters(sc, monkeypatch)
    (t,) = c.transactions.values()
    assert c.modbus_timeouts > 0
    assert c.modbus_requests + c.modbus_skipped == _instants(500, 200)
    assert c.modbus_requests - c.modbus_responses == c.modbus_timeouts + t.outstanding
    samples = backends.get("simpy").run(sc).metrics
    for (_, unanswered), (_, timeouts) in zip(
        samples["modbus_unanswered:m"], samples["modbus_timeouts:m"], strict=True
    ):
        assert unanswered - timeouts in (0, 1)  # the one outstanding, if any


def test_late_responses_are_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout under the queueing delay: some responses arrive after it,
    and are discarded, the latency of the others is under it."""
    discarded: list[int] = []
    accept = engine._Transaction.accept

    def counting(self: engine._Transaction, frame: engine._Frame, now: int) -> bool:
        ok = accept(self, frame, now)
        if not ok:
            discarded.append(now - self.sent_at)
        return ok

    monkeypatch.setattr(engine._Transaction, "accept", counting)
    timeout_ms = 0.3
    sc = _lossy(timeout_ms, every=0.1).to_scenario(duration=200)
    result = backends.get("simpy").run(sc)
    assert discarded and all(late >= tb.ms_to_ns(timeout_ms) for late in discarded)
    rtts = [v for _, v in result.metrics["modbus_latency:m"]]
    assert rtts and max(rtts) < timeout_ms * 1_000


def test_without_a_timeout_a_lost_response_stops_the_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _counters(_lossy(None).to_scenario(duration=200), monkeypatch)
    (t,) = c.transactions.values()
    assert t.outstanding
    assert c.modbus_requests - c.modbus_responses == 1
    assert c.modbus_timeouts == 0
    assert c.modbus_skipped > c.modbus_requests  # every instant after the loss


@pytest.mark.parametrize("engine", ENGINES)
def test_the_engine_runs_one_request_in_flight(engine: str) -> None:
    _backend(engine)
    backends.check(_master().to_scenario(duration=10), engine)


# --- step 3: the Rust engine ---


@pytest.mark.parametrize("timeout_ms", [None, 3.0, 0.5, 0.3, 1e-6])
def test_rust_gives_simpys_results_with_loss(timeout_ms: float | None) -> None:
    """Lost, late and in-time responses, timeouts and skipped instants, and
    open-loop traffic beside them: the same samples, event counts included."""
    rust = _backend("rust")
    sc = _lossy(timeout_ms, every=0.5).to_scenario(duration=100)
    simpy_run, rust_run = backends.get("simpy").run(sc), rust.run(sc)
    assert rust_run.metrics == simpy_run.metrics
    assert rust_run.stats.events == simpy_run.stats.events


def test_a_timeout_past_the_end_is_never_processed() -> None:
    """The longest timeout, far beyond the run: an event that never comes, on
    both engines."""
    rust = _backend("rust")
    sc = _pair(62_500, timeout_ms=9.2e12)
    simpy_run, rust_run = backends.get("simpy").run(sc), rust.run(sc)
    assert rust_run.metrics == simpy_run.metrics
    assert rust_run.stats.events == simpy_run.stats.events


# --- step 4: ns-3 ---


def _poll_line(sc: Scenario) -> str:
    text, _ = ns3.lower(sc)
    (line,) = (line for line in text.splitlines() if line.startswith("poll"))
    return line


def test_ns3_lowers_one_request_in_flight() -> None:
    """Format 3: outstanding 0 or 1, and the timeout in ns, 0 for none."""
    assert _poll_line(_master(1, 50.0).to_scenario(duration=10)).endswith(" 1 50000000")
    assert _poll_line(_master(1, None).to_scenario(duration=10)).endswith(" 1 0")
    assert _poll_line(_master(None, None).to_scenario(duration=10)).endswith(" 0 0")


def test_ns3_tcp_settings() -> None:
    sc = _master().to_scenario(duration=10)
    assert ns3.lower(sc)[0].splitlines()[2] == "tcp 200000000 200000000 40000000 1000000000"
    ns3_defaults = ns3.TcpSettings(
        min_rto_ms=1000, clock_granularity_ms=1, delayed_ack_ms=200, syn_rto_ms=3000
    )
    assert ns3.lower(sc, ns3_defaults)[0].splitlines()[2] == (
        "tcp 1000000000 1000000 200000000 3000000000"
    )
    assert backends.Ns3Backend(tcp=ns3_defaults).tcp == ns3_defaults
    assert backends.REGISTRY["ns3"].tcp is None  # Linux's


@pytest.mark.parametrize("value", [0, -1.0, 1e-7, float("inf"), float("nan"), True, "200"])
def test_ns3_tcp_settings_are_positive_times(value) -> None:
    with pytest.raises(ValueError, match="TcpSettings.min_rto_ms must be from 1 ns"):
        ns3.TcpSettings(min_rto_ms=value)


needs_ns3 = pytest.mark.skipif(
    not backends.REGISTRY["ns3"].available(), reason="NS3_HOME names no ns-3 build"
)


@needs_ns3
def test_ns3_without_loss_and_under_the_period_nothing_changes() -> None:
    open_loop = backends.run(_shared_port(None, None), "ns3")
    assert backends.run(_shared_port(1, 5.0), "ns3").metrics == open_loop.metrics


@needs_ns3
@pytest.mark.parametrize(
    "rate, timeout_ms",
    [
        (25_000, None),  # each response arrives 2 ns after the next instant
        (10_000, 0.04),  # each response arrives 2 ns after its timeout
        (50_000, 0.02),  # each timeout falls on the next instant
    ],
)
def test_ns3_follows_the_rules_at_exact_round_trips(rate: float, timeout_ms: float | None) -> None:
    """TCP adds 1 ns per send to SimPy's 40 us round trip: no response moves
    across a poll instant or a timeout, so the counts are SimPy's."""
    sc = _pair(rate, timeout_ms=timeout_ms)
    simpy_final, ns3_final = _final(sc), _final(sc, "ns3")
    for metric in ("modbus_skipped", "modbus_timeouts", "modbus_unanswered"):
        assert ns3_final[metric] == simpy_final[metric], metric


@needs_ns3
@pytest.mark.parametrize("timeout_ms", [None, 3.0, 0.5])
def test_ns3_keeps_one_request_in_flight_with_loss(timeout_ms: float | None) -> None:
    """At every sample, at most one request awaits a response; without a
    timeout, TCP brings every lost response back."""
    m = backends.run(_lossy(timeout_ms, every=0.5).to_scenario(duration=100), "ns3").metrics
    timeouts = [v for _, v in m["modbus_timeouts:m"]]
    awaiting = [u - t for (_, u), t in zip(m["modbus_unanswered:m"], timeouts, strict=True)]
    assert set(awaiting) <= {0, 1}
    assert timeouts[-1] == 0 if timeout_ms is None else timeouts[-1] > 0
    # instants are skipped while a response is late, unless the timeout is
    # under the period of 2 ms
    assert (m["modbus_skipped:m"][-1][1] > 0) == (timeout_ms is None or timeout_ms > 2)


def _testbed(timeout_ms: float | None = None, outstanding: int | None = 1) -> Scenario:
    from benchmarks import closed_loop, loss

    return loss.scenario(
        0.99, 30_000, closed_loop.BUFFER, 1, outstanding=outstanding, timeout_ms=timeout_ms
    )


def _long_rtts(result, above_ms: float = 100.0) -> list[float]:
    rtts = (v / 1_000 for _, v in result.metrics["modbus_latency:laptop"])
    return [r for r in rtts if r > above_ms]


@needs_ns3
def test_ns3_a_lost_response_comes_back_after_linux_rto() -> None:
    """The testbed of ADR-0012: with one request in flight no duplicate ACKs
    follow a lost response, which TCP retransmits after the RTO, at least
    200 ms after its first transmission; the hardware measured about 215 ms.
    ns-3's own settings give 1 s, and an open-loop poll a fast retransmit."""
    result = backends.run(_testbed(), "ns3")
    long = _long_rtts(result)
    assert long and all(200 < r < 230 for r in long)
    assert result.metrics["modbus_unanswered:laptop"][-1][1] == 0
    ns3_own = ns3.TcpSettings(
        min_rto_ms=1000, clock_granularity_ms=1, delayed_ack_ms=200, syn_rto_ms=3000
    )
    own = backends.Ns3Backend(tcp=ns3_own).run(_testbed())
    assert _long_rtts(own) and all(1_000 < r < 1_030 for r in _long_rtts(own))
    assert not _long_rtts(backends.run(_testbed(outstanding=None), "ns3"), above_ms=50)


@needs_ns3
def test_ns3_a_timeout_under_the_rto_fires() -> None:
    result = backends.run(_testbed(timeout_ms=100), "ns3")
    assert result.metrics["modbus_timeouts:laptop"][-1][1] > 0
    assert not _long_rtts(result, above_ms=100)  # late responses are discarded
