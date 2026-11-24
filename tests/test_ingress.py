"""ADR-0013: switch buffers accounted per ingress port, limits in frames,
and flows between any two end stations."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import pytest

from smolpy import Frames, Network, backends
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Scenario, SwitchNode
from smolpy.model import timebase as tb
from smolpy.model.frames import IMIX_SIZES
from smolpy.model.validate import ScenarioError, check_node, validate
from smolpy.sim import engine

SIZE = 1_518


def _engines() -> list[str]:
    """The engines with the features of ADR-0013 installed here."""
    wanted = {"ingress_buffer", "buffer_frames", "station_flows", "slave_metrics"}
    return [
        name
        for name in backends.installed()
        if wanted <= backends.REGISTRY[name].capabilities().features
    ]


ENGINES = _engines()


def _switch(sc: Scenario) -> SwitchNode:
    node = next(n for n in sc.nodes if isinstance(n, SwitchNode))
    return node


# --- step 1: the model ---


def test_limits_are_unbounded_by_default() -> None:
    sw = SwitchNode("sw", 8)
    assert sw.egress_limit is None and sw.ingress_limit is None and not sw.bounded


def test_a_limit_is_in_bytes_or_in_frames() -> None:
    net = Network("t")
    sw = net.switch("sw", ports=3, buffer=91_080, ingress_buffer=Frames(60))
    spec = sw.to_spec()
    assert (spec.buffer_bytes, spec.buffer_frames) == (91_080, None)
    assert (spec.ingress_buffer_bytes, spec.ingress_buffer_frames) == (None, 60)
    assert spec.egress_limit == (91_080, "bytes")
    assert spec.ingress_limit == (60, "frames")
    assert net.switch("s2", ports=2, buffer=Frames(5)).to_spec().egress_limit == (5, "frames")


@pytest.mark.parametrize(
    "text, ingress",
    [
        ("60frames", (60, "frames")),
        ("1frame", (1, "frames")),
        ("115kB", (115_000, "bytes")),
        ("112KiB", (114_688, "bytes")),
        ("91080", (91_080, "bytes")),
    ],
)
def test_smol_spells_both_units(tmp_path: Path, text: str, ingress: tuple[int, str]) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        f'network "t" {{\n    switch sw ports=2 ingress_buffer={text} buffer=40frames\n'
        "    simulate duration=10\n}\n"
    )
    sw = _switch(interpret_to_scenario(path))
    assert sw.ingress_limit == ingress
    assert sw.egress_limit == (40, "frames")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"ingress_buffer": Frames(0)}, "ingress_buffer in frames must be a whole number of at"),
        ({"buffer": Frames(2.5)}, "buffer in frames must be a whole number"),  # type: ignore[arg-type]
        ({"buffer": Frames(True)}, "buffer in frames must be a whole number"),
        ({"ingress_buffer": 1_000}, "ingress_buffer must be a whole number of bytes, at least"),
        ({"ingress_buffer": 2_000.5}, "ingress_buffer must be a whole number of bytes"),
    ],
)  # fmt: skip
def test_a_limit_is_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        Network("t").switch("sw", ports=2, **kwargs)


def test_one_account_has_one_unit() -> None:
    with pytest.raises(ScenarioError, match="buffer is given both in bytes and in frames"):
        check_node(SwitchNode("sw", 2, buffer_bytes=2_000, buffer_frames=2))
    with pytest.raises(ScenarioError, match="ingress_buffer is given both"):
        check_node(SwitchNode("sw", 2, ingress_buffer_bytes=2_000, ingress_buffer_frames=2))


def test_a_byte_limit_holds_the_largest_frame() -> None:
    net, *_ = _one_flow(ingress=2_000, size=9_000)
    with pytest.raises(ScenarioError, match="ingress_buffer 2000 B is smaller than the largest"):
        validate(net.to_scenario(duration=10))
    net, *_ = _one_flow(ingress=Frames(1), size=9_000)  # any frame fits in one frame
    validate(net.to_scenario(duration=10))


def test_the_fields_are_written_only_when_set() -> None:
    plain = json.loads(Network("t").to_scenario(duration=1).to_json())
    net = Network("t")
    net.switch("a", ports=2)
    net.switch("b", ports=2, ingress_buffer=Frames(3))
    data = json.loads(net.to_scenario(duration=1).to_json())
    assert "buffer_frames" not in json.dumps(plain)
    a, b = data["nodes"]
    assert not {"buffer_bytes", "buffer_frames", "ingress_buffer_bytes"} & set(a)
    assert b["ingress_buffer_frames"] == 3 and "ingress_buffer_bytes" not in b
    sc = net.to_scenario(duration=1)
    assert Scenario.from_json(sc.to_json()) == sc


@pytest.mark.parametrize(
    "feature, build, message",
    [
        ("ingress_buffer", lambda: _one_flow(ingress=Frames(4))[0], "ingress buffer limits"),
        ("buffer_frames", lambda: _one_flow(egress=Frames(4))[0], "buffer limits in frames"),
        ("buffer_frames", lambda: _one_flow(ingress=Frames(4))[0], "buffer limits in frames"),
        ("station_flows", lambda: _host_and_server()[0], "flows from or to a station"),
        ("slave_metrics", lambda: _host_and_server()[0], "on a station other than an adapter"),
    ],
)
def test_a_backend_without_the_feature_rejects_it(
    feature: str, build, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    simpy = backends.REGISTRY["simpy"]
    caps = simpy.capabilities()
    reduced = backends.Capabilities(**{**caps.__dict__, "features": caps.features - {feature}})
    monkeypatch.setattr(type(simpy), "capabilities", lambda self: reduced)
    with pytest.raises(ScenarioError, match=message):
        backends.check(build().to_scenario(duration=10), "simpy")


# --- step 2: flows between any two end stations ---


def _host_and_server(rate: float = 2_000) -> tuple[Network, dict]:
    """The testbed's Pi: one Modbus slave that also sends UDP to the laptop,
    on one port; a second slave on another port."""
    net = Network("host")
    sw = net.switch("sw", ports=4)
    pi = net.modbus_slave("pi", ip="10.0.0.1", unit_id=1)
    relay = net.modbus_slave("relay", ip="10.0.0.3", unit_id=2)
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=1_000, length=2)
    net.link(relay, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=100, length=2)
    pi.sends(laptop, rate=rate, size=SIZE)
    laptop.polls(pi, register=0, count=10, rate=100)
    laptop.polls(relay, register=0, count=10, rate=50)
    for metric in ("modbus_latency", "modbus_unanswered"):
        for node in (laptop, pi, relay):
            net.observe(metric, on=node, every=10)
    net.observe("frames_sent", on=pi, every=10)
    net.observe("frames_received", on=laptop, every=10)
    return net, {"pi": pi, "relay": relay, "laptop": laptop}


def test_a_modbus_slave_sends_a_flow() -> None:
    """At 100 ms the Pi has sent the UDP frames of 0.5 ms to 99.5 ms and the
    responses to the polls of 10 ms to 90 ms, on its one link: the frame
    and the poll of 100 ms are still on their way."""
    net, _ = _host_and_server()
    m = net.simulate(duration=101).metrics
    assert m["frames_sent:pi"][-1] == (100.0, 199 + 9)
    # and the laptop has the relay's responses to its polls of 20 ms to 80 ms
    assert m["frames_received:laptop"][-1] == (100.0, 199 + 9 + 4)


def test_the_modbus_metrics_of_a_slave_are_its_polls() -> None:
    net, _ = _host_and_server()
    m = net.simulate(duration=1_000).metrics
    master, pi, relay = (dict(m[f"modbus_latency:{n}"]) for n in ("laptop", "pi", "relay"))
    # the relay's polls are every 20 ms: in the windows without them, the
    # master's mean is the Pi's
    for t, v in master.items():
        if t not in relay:
            assert v == pi[t]
    assert relay and all(relay[t] < pi[t] for t in relay)  # no UDP ahead of its responses
    for (_, a), (_, b), (_, c) in zip(
        m["modbus_unanswered:laptop"],
        m["modbus_unanswered:pi"],
        m["modbus_unanswered:relay"],
        strict=True,
    ):
        assert a == b + c


def test_every_station_sends_and_receives_flows() -> None:
    """A flow from and to a broker, a slave and a device: the receiving
    station takes the frames at its NIC and ignores them."""
    net = Network("stations")
    sw = net.switch("sw", ports=5)
    host = net.adapter("host", ip="10.0.0.1")
    broker = net.mqtt_broker("broker", ip="10.0.0.2")
    slave = net.modbus_slave("slave", ip="10.0.0.3", unit_id=1)
    device = net.profinet_device("device", ip="10.0.0.4")
    for node in (host, broker, slave, device):
        net.link(node, sw, speed=100, length=2)
    broker.sends(host, rate=1_000, size=200)
    slave.sends(device, rate=1_000, size=300)
    device.sends(broker, rate=1_000, size=400, pattern="poisson")
    host.sends(slave, rate=1_000, size=500)
    host.controls(device, cycle=4, inputs=10, outputs=10)
    for node in (host, broker, slave, device):
        net.observe("frames_sent", on=node, every=10)
        net.observe("bytes_sent", on=node, every=10)
    net.observe("frames_received", on=host, every=10)
    net.observe("pn_watchdog_expiries", on=device, every=10)
    m = net.simulate(duration=101).metrics
    last = {k: v[-1][1] for k, v in m.items()}
    # at 100 ms: the frames of 1 ms to 99 ms, and the cyclic frames of 0 to 96 ms
    assert last["frames_sent:broker"] == 99
    assert last["bytes_sent:slave"] == 99 * 300 / 1e6
    assert last["frames_sent:host"] == 99 + 25  # its flow and its PROFINET outputs
    assert last["frames_received:host"] == 99 + 25  # the broker's flow and the inputs
    assert last["frames_sent:device"] > 0
    assert last["pn_watchdog_expiries:device"] == 0


def test_a_flow_from_a_station_has_its_own_stream() -> None:
    """Source ids are unchanged: a slave's flow is flow:<slave>-><to>#k."""
    from smolpy.model.streams import source_ids

    net, _ = _host_and_server()
    assert "flow:pi->laptop#0" in source_ids(net.to_scenario(duration=1))


def test_smol_flows_from_any_station(tmp_path: Path) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        'network "t" {\n'
        "    switch sw ports=2\n"
        "    modbus_slave pi ip=10.0.0.1 unit_id=1\n"
        "    adapter laptop ip=10.0.0.2\n"
        "    link pi -- sw speed=1000 length=2\n"
        "    link laptop -- sw speed=100 length=2\n"
        "    flow pi -> laptop rate=100 size=1518\n"
        "    observe frames_sent on pi every=10\n"
        "    simulate duration=10\n}\n"
    )
    sc = interpret_to_scenario(path)
    assert sc.traffic[0].source == "pi"
    bad = tmp_path / "bad.smol"
    bad.write_text(path.read_text().replace("flow pi", "flow sw"))
    from smolpy.lang.errors import SMOLSemanticError

    with pytest.raises(SMOLSemanticError, match="'sw' is a Switch, expected an end station"):
        interpret_to_scenario(bad)


# --- step 3: ingress accounts and limits in frames ---


def _one_flow(
    ingress=None, egress=None, *, size: int | str = SIZE, rate: float = 9_000, pattern="poisson"
) -> tuple[Network, dict]:
    """One flow from a 1 Gb/s port to a 100 Mb/s one."""
    net = Network("one")
    sw = net.switch("sw", ports=2, buffer=egress, ingress_buffer=ingress)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=rate, size=size, pattern=pattern)
    for metric, node in (
        ("drops_total", sw),
        ("drops", sw),
        ("queue_depth", sw),
        ("latency", b),
        ("frames_received", b),
        ("frames_sent", a),
    ):
        net.observe(metric, on=node, every=5)
    for metric in ("ingress_drops_total", "egress_drops_total"):
        net.observe(metric, on=sw, every=5)
    return net, {"sw": sw, "a": a, "b": b}


class _Audit:
    """Every admission to an account, checked against the frames its switch
    actually holds queued (ADR-0013): an egress account holds the frames on
    its queue, an ingress account the frames that came in on its port, each
    flooded frame once, the frames being transmitted in neither."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.switches: list[tuple[dict[int, engine._LinkChannel], engine._Ingress]] = []
        self.records: list[tuple[int, int, int, bool, bool]] = []  # held, size, limit, frames, fits
        self.envs: list[engine._Environment] = []
        self.times: list[int] = []  # the instant of each record
        self.releases: dict[int, int] = {}  # id(account) -> instant of its last release
        self.tight: int = 0  # admissions that fit only by a release at the same instant
        original_forwarder = engine._switch_forwarder
        fits, release = engine._Account.fits, engine._Account.release
        audit = self

        def forwarder(env, inbound, out, initial=None, ingress=None):  # type: ignore[no-untyped-def]
            if ingress is not None:
                audit.switches.append((out, ingress))
            audit.envs.append(env)
            return original_forwarder(env, inbound, out, initial, ingress)

        def checked_fits(account: engine._Account, size: int) -> bool:
            held = audit.held(account)
            assert account.held == held, (account.held, held)
            ok = fits(account, size)
            unit = 1 if account.in_frames else size
            assert ok == (held + unit <= account.limit)
            audit.records.append((held, size, account.limit, account.in_frames, ok))
            now = audit.envs[0].now
            audit.times.append(now)
            if ok and held + 2 * unit > account.limit and audit.releases.get(id(account)) == now:
                audit.tight += 1
            return ok

        def checked_release(account: engine._Account, size: int) -> None:
            release(account, size)
            audit.releases[id(account)] = audit.envs[0].now

        monkeypatch.setattr(engine, "_switch_forwarder", forwarder)
        monkeypatch.setattr(engine._Account, "fits", checked_fits)
        monkeypatch.setattr(engine._Account, "release", checked_release)

    def held(self, account: engine._Account) -> int:
        for out, ingress in self.switches:
            for port, acc in ingress.accounts.items():
                if acc is account:
                    frames: dict[int, int] = {}  # by the stored frame: its size
                    for ch in out.values():
                        for item in ch.queue.items:
                            f = item.item
                            if f.in_port == port:
                                assert f.stored is not None and f.stored.account is account
                                frames[id(f.stored)] = f.size_bytes
                    return len(frames) if account.in_frames else sum(frames.values())
        for out, _ in self.switches:
            for ch in out.values():
                if ch.egress is account:
                    sizes = [item.item.size_bytes for item in ch.queue.items]
                    return len(sizes) if account.in_frames else sum(sizes)
        # an egress account of a switch without ingress accounts
        return account.held


def _final(result, key: str) -> float:
    return result.metrics[key][-1][1]


def test_no_drops_below_the_limits() -> None:
    """Limits that are never reached change nothing, events included."""
    free = _one_flow(rate=3_000)[0].simulate(duration=200)
    for limit in (Frames(10_000), 10_000_000):
        both = _one_flow(ingress=limit, egress=limit, rate=3_000)[0].simulate(duration=200)
        assert both.metrics == free.metrics
        assert both.stats.events == free.stats.events
    assert _final(free, "drops_total:sw") == 0


@pytest.mark.parametrize("limit", [Frames(3), 3 * SIZE + 100])
def test_a_frame_is_dropped_exactly_when_its_ingress_account_is_full(
    limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = _Audit(monkeypatch)
    r = _one_flow(ingress=limit, rate=9_500)[0].simulate(duration=300)
    assert audit.records
    assert any(not ok for *_, ok in audit.records) and any(ok for *_, ok in audit.records)
    t_last = tb.ms_to_ns(r.metrics["ingress_drops_total:sw"][-1][0])
    dropped = sum(
        not ok for (*_, ok), t in zip(audit.records, audit.times, strict=True) if t <= t_last
    )
    assert _final(r, "ingress_drops_total:sw") == dropped
    assert _final(r, "egress_drops_total:sw") == 0
    assert _final(r, "drops_total:sw") == _final(r, "ingress_drops_total:sw")


def test_a_frame_limit_counts_a_small_frame_as_a_large_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IMIX through a frame limit: a 64 B frame is dropped at a full account
    exactly as a 1518 B one is."""
    audit = _Audit(monkeypatch)
    _one_flow(ingress=Frames(4), size="imix", rate=40_000)[0].simulate(duration=300)
    dropped = {size for _, size, _, _, ok in audit.records if not ok}
    assert {64, 594, 1_518} <= dropped
    for held, _, limit, in_frames, ok in audit.records:
        assert in_frames and ok == (held < limit)


def test_the_account_is_released_when_the_transmitter_takes_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames of 1242 B every 100 us, as long as their own transmission at
    100 Mb/s: each takes 960 ns of gap more, so the queue grows, and arrivals
    fall on the instants the transmitter takes a frame, every 625 frames.
    With one frame of ingress, such an arrival fits only because the frame
    taken at that instant has left the account."""
    audit = _Audit(monkeypatch)
    net, _ = _one_flow(ingress=Frames(1), size=1_242, rate=10_000, pattern="constant")
    net.simulate(duration=300)
    assert audit.tight > 0


def _flooding(ingress, egress) -> Network:
    """b sits behind a second switch and never sends, so the first one, which
    knows only its own stations, floods a's frames to it: a copy to every
    other port. d keeps c's port busy too."""
    net = Network("flood")
    sw = net.switch("sw", ports=4, buffer=egress, ingress_buffer=ingress)
    far = net.switch("far", ports=2)
    a = net.adapter("a", ip="10.0.0.1")
    c = net.adapter("c", ip="10.0.0.3")
    d = net.adapter("d", ip="10.0.0.4")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(c, sw, speed=100, length=5)
    net.link(d, sw, speed=1_000, length=5)
    net.link(sw, far, speed=100, length=5)
    net.link(b, far, speed=100, length=5)
    a.sends(b, rate=9_000, size=SIZE, pattern="poisson")
    d.sends(c, rate=6_000, size=SIZE, pattern="poisson")
    net.observe("ingress_drops_total", on=sw, every=5)
    net.observe("egress_drops_total", on=sw, every=5)
    return net


def test_a_flooded_frame_is_held_once_until_its_last_copy_is_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit checks every admission: a flooded frame counts once on its
    ingress port while any copy is queued. With egress limits too, some
    flooded frames lose every copy and are never charged."""
    audit = _Audit(monkeypatch)
    created: list[engine._Stored] = []
    charged: list[int] = []
    stored_init = engine._Stored.__init__
    charge = engine._Account.charge

    def init(self: engine._Stored, account: engine._Account) -> None:
        stored_init(self, account)
        created.append(self)

    def counting(self: engine._Account, size: int) -> None:
        if any(self is acc for _, ing in audit.switches for acc in ing.accounts.values()):
            charged.append(size)
        charge(self, size)

    monkeypatch.setattr(engine._Stored, "__init__", init)
    monkeypatch.setattr(engine._Account, "charge", counting)
    r = _flooding(Frames(4), Frames(3)).simulate(duration=300)
    assert _final(r, "ingress_drops_total:sw") > 0
    assert _final(r, "egress_drops_total:sw") > 0
    assert len(created) > len(charged) > 0  # some frames lost every copy


@pytest.mark.parametrize("limit", [Frames(5), 5 * SIZE, Frames(1)])
@pytest.mark.parametrize("engine_name", ENGINES)
def test_one_ingress_port_is_its_egress_port(limit, engine_name: str) -> None:
    """One flow: an ingress limit gives the run an egress limit gives, events
    included; only the split of the drops differs."""
    ingress = _one_flow(ingress=limit)[0].to_scenario(duration=300, seed=3)
    egress = _one_flow(egress=limit)[0].to_scenario(duration=300, seed=3)
    a = backends.get(engine_name).run(ingress)
    b = backends.get(engine_name).run(egress)
    split = ("ingress_drops_total:sw", "egress_drops_total:sw")
    assert {k: v for k, v in a.metrics.items() if k not in split} == {
        k: v for k, v in b.metrics.items() if k not in split
    }
    assert a.stats.events == b.stats.events
    assert a.metrics["ingress_drops_total:sw"] == b.metrics["egress_drops_total:sw"]
    assert _final(a, "drops_total:sw") > 0
    assert set(v for _, v in a.metrics["egress_drops_total:sw"]) == {0.0}


# --- the isolation case ---


def _isolation(ingress, egress, seed: int, duration: float = 2_000) -> dict[str, float]:
    """Two sources share one 100 Mb/s egress port: A offers 1.1 of it, B a
    light Poisson stream. Returns each one's share of frames lost."""
    net = Network("isolation")
    sw = net.switch("sw", ports=3, buffer=egress, ingress_buffer=ingress, mac_table="topology")
    far = net.switch("far", ports=3, mac_table="topology")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sink_a = net.adapter("sink_a", ip="10.0.1.1")
    sink_b = net.adapter("sink_b", ip="10.0.1.2")
    net.link(a, sw, speed=1_000, length=2)
    net.link(b, sw, speed=1_000, length=2)
    net.link(sw, far, speed=100, length=2)
    net.link(sink_a, far, speed=1_000, length=2)
    net.link(sink_b, far, speed=1_000, length=2)
    a.sends(sink_a, rate=1.1 * 100e6 / (1_538 * 8), size=SIZE, pattern="poisson")
    b.sends(sink_b, rate=400, size=SIZE, pattern="poisson")
    every = duration - 1
    for m, n in (("frames_sent", a), ("frames_sent", b), ("frames_received", sink_a)):
        net.observe(m, on=n, every=every)
    net.observe("frames_received", on=sink_b, every=every)
    net.observe("ingress_drops_total", on=sw, every=every)
    net.observe("egress_drops_total", on=sw, every=every)
    m = net.simulate(duration=duration, seed=seed).metrics
    last = {k: v[-1][1] for k, v in m.items()}
    return {
        "a": 1 - last["frames_received:sink_a"] / last["frames_sent:a"],
        "b": 1 - last["frames_received:sink_b"] / last["frames_sent:b"],
        # B's frames sent and not received: lost, or on their way at the sample
        "b_missing": last["frames_sent:b"] - last["frames_received:sink_b"],
        "ingress": last["ingress_drops_total:sw"],
        "egress": last["egress_drops_total:sw"],
    }


SEEDS = range(1, 6)


def test_an_ingress_limit_isolates_a_light_source() -> None:
    """Only A's port drops: B's frames missing at the end are the few on
    their way, behind at most 20 of A's frames."""
    for seed in SEEDS:
        r = _isolation(Frames(20), None, seed)
        assert r["a"] > 0.05 and r["ingress"] > 0 and r["egress"] == 0
        assert r["b_missing"] <= 2


@pytest.mark.parametrize("both", [False, True])
def test_an_egress_limit_drops_a_light_source_at_the_overload_rate(both: bool) -> None:
    """By PASTA, B's Poisson frames find the port full as often as A's do.
    With equal ingress and egress limits A's frames fill both, so B's pass
    the ingress check and are dropped at egress, as without the ingress
    limit; A's further frames are ingress drops."""
    runs = [_isolation(Frames(20) if both else None, Frames(20), seed) for seed in SEEDS]
    diff = [r["b"] - r["a"] for r in runs]
    error = statistics.stdev(diff) / math.sqrt(len(diff))
    assert abs(statistics.mean(diff)) <= 4 * error + 0.005, (diff, error)
    assert all(r["b"] > 0 for r in runs)
    if both:
        assert all(r["ingress"] > 0 and r["egress"] > 0 for r in runs)


def test_a_smaller_ingress_limit_keeps_room_for_the_light_source() -> None:
    for seed in SEEDS:
        r = _isolation(Frames(15), Frames(20), seed)
        assert r["a"] > 0.05 and r["ingress"] > 0 and r["egress"] == 0
        assert r["b_missing"] <= 2


# --- M/G/1/K ---


def _poisson(k: int, mean: float) -> float:
    return math.exp(-mean + k * math.log(mean) - math.lgamma(k + 1)) if mean > 0 else float(k == 0)


def mg1k_blocking(lam: float, sizes: list[tuple[float, float]], gap: float, k: int) -> float:
    """Blocking probability of one port with room for K - 1 queued frames
    and one in transmission, under Poisson arrivals at *lam* per ns.

    *sizes* are (probability, transmission time in ns) of the frames. The
    port is M/G/1/K with service gap + tx, except the first frame of a busy
    period, taken when it arrives, A after the last FCS: it waits only what
    is left of that gap, max(0, gap - A) + tx (ADR-0013). Embedded Markov
    chain at FCS instants; blocking is 1 - throughput / lam."""
    n = k  # states 0 .. K - 1 left behind by a departure

    def regular(j: int) -> float:
        return sum(p * _poisson(j, lam * (gap + tx)) for p, tx in sizes)

    steps = 400  # Simpson's rule over the arrival within the gap

    def first(j: int) -> float:
        total = 0.0
        for p, tx in sizes:
            h = gap / steps
            integral = 0.0
            for i in range(steps + 1):
                a = i * h
                w = 1 if i in (0, steps) else (4 if i % 2 else 2)
                integral += w * lam * math.exp(-lam * a) * _poisson(j, lam * (tx + gap - a))
            total += p * (math.exp(-lam * gap) * _poisson(j, lam * tx) + integral * h / 3)
        return total

    a_reg = [regular(j) for j in range(n)]
    a_first = [first(j) for j in range(n)]
    p = [[0.0] * n for _ in range(n)]
    for i in range(n):
        row = a_first if i == 0 else a_reg
        start = 0 if i == 0 else i - 1
        for j in range(start, n - 1):
            p[i][j] = row[j - start]
        p[i][n - 1] = 1 - sum(p[i][: n - 1])
    m = [[p[j][i] - (i == j) for j in range(n)] for i in range(n)]
    m[-1] = [1.0] * n
    rhs = [0.0] * (n - 1) + [1.0]
    for c in range(n):
        r = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[r], rhs[c], rhs[r] = m[r], m[c], rhs[r], rhs[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
                rhs[r] -= f * rhs[c]
    pi = [rhs[i] / m[i][i] for i in range(n)]
    mean_tx = sum(p * tx for p, tx in sizes)
    mean_first = mean_tx + gap - (1 - math.exp(-lam * gap)) / lam
    cycle = pi[0] * (1 / lam + mean_first) + (1 - pi[0]) * (gap + mean_tx)
    return 1 - 1 / (cycle * lam)


def test_mg1k_solution() -> None:
    """Without a gap and with one size it is M/D/1/K (tests.test_buffers)."""
    from tests.test_buffers import md1k

    for rho, k in ((0.5, 1), (0.9, 5), (1.3, 3)):
        want, _ = md1k(rho, k)
        assert mg1k_blocking(rho / 1_000, [(1.0, 1_000.0)], 0.0, k) == pytest.approx(want)


def _mg1k_run(rho: float, limit: int, seed: int, frames: int = 40_000) -> float:
    """IMIX Poisson frames from a 1 Tb/s port, whose own transmission is at
    most 13 ns, into a 1 Gb/s port with an ingress limit in frames."""
    mean = sum(
        p * (tb.transmission_ns(8 + s, 1_000) + tb.transmission_ns(12, 1_000))
        for s, p in IMIX_SIZES
    )
    rate = rho * 1e9 / mean
    duration = frames / rate * 1_000
    net = Network("mg1k")
    sw = net.switch("sw", ports=2, ingress_buffer=Frames(limit))
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000_000, length=0)
    net.link(b, sw, speed=1_000, length=0)
    a.sends(b, rate=rate, size="imix", pattern="poisson")
    every = 0.999 * duration
    net.observe("frames_sent", on=a, every=every)
    net.observe("ingress_drops_total", on=sw, every=every)
    m = net.simulate(duration=duration, seed=seed).metrics
    return m["ingress_drops_total:sw"][0][1] / m["frames_sent:a"][0][1]


@pytest.mark.parametrize("rho, limit", [(0.9, 4), (1.2, 2)])
def test_a_frame_limit_is_an_mg1k_queue(rho: float, limit: int) -> None:
    """Blocking against M/G/1/K with K = 1 + limit, over five seeds of 40 000
    frames, within four standard errors of the mean, plus 0.3 % for the
    rounding of draws to ns and the 1 Tb/s port."""
    sizes = [(p, float(tb.transmission_ns(8 + s, 1_000))) for s, p in IMIX_SIZES]
    gap = float(tb.transmission_ns(12, 1_000))
    mean = sum(p * (gap + tx) for p, tx in sizes)
    want = mg1k_blocking(rho / mean, sizes, gap, limit + 1)
    got = [_mg1k_run(rho, limit, seed) for seed in SEEDS]
    error = statistics.stdev(got) / math.sqrt(len(got))
    assert abs(statistics.mean(got) - want) <= 4 * error + 0.003 * want, (got, want, error)


@pytest.mark.skipif("ns3" not in ENGINES, reason="needs ns-3 (NS3_HOME)")
@pytest.mark.parametrize("limit", [Frames(5), 4 * SIZE])
def test_ns3_gives_simpys_run_with_random_ingress_drops(limit) -> None:
    """Poisson IMIX traffic from two sources through ingress and egress
    limits: ns-3 draws SimPy's streams, so it drops the same frames."""
    net = Network("random-ingress")
    sw = net.switch("sw", ports=3, ingress_buffer=limit, buffer=Frames(8))
    a = net.adapter("a", ip="10.0.0.1")
    c = net.adapter("c", ip="10.0.0.3")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(c, sw, speed=1_000, length=5)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=20_000, size="imix", pattern="poisson")
    c.sends(b, rate=6_000, size=SIZE, pattern="bursty")
    for metric in ("ingress_drops_total", "egress_drops_total", "queue_depth"):
        net.observe(metric, on=sw, every=5)
    net.observe("latency", on=b, every=5)
    net.observe("frames_received", on=b, every=5)
    sc = net.to_scenario(duration=300, seed=11)
    ns3, simpy = backends.get("ns3").run(sc), backends.get("simpy").run(sc)
    assert ns3.metrics == simpy.metrics
    assert _final(simpy, "ingress_drops_total:sw") > 0
    assert _final(simpy, "egress_drops_total:sw") > 0
