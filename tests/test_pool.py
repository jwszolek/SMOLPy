"""ADR-0015: a shared switch buffer pool, a reserve per ingress port, and a
drop state ended by a drain."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import pytest

from smolpy import Cells, Frames, Network, backends
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Scenario, SwitchNode
from smolpy.model import timebase as tb
from smolpy.model.validate import ScenarioError, validate
from smolpy.sim import engine

SIZE = 1_518


def _engines() -> list[str]:
    wanted = {"buffer_pool", "drain_resume"}
    return [
        name
        for name in backends.installed()
        if wanted <= backends.REGISTRY[name].capabilities().features
    ]


ENGINES = _engines()


def _final(result, key: str) -> float:
    return result.metrics[key][-1][1]


# --- the model ---


def test_a_pool_with_a_reserve_and_a_drain() -> None:
    sw = Network("t").switch(
        "sw", ports=3, cell=128, pool=Cells(710), ingress_reserve=Cells(30), ingress_drain=Cells(72)
    )
    spec = sw.to_spec()
    assert spec.pool_limit == (710, "cells") and spec.ingress_unit == "cells"
    assert (spec.ingress_reserve, spec.ingress_drain) == (30, 72)
    assert spec.ingress_accounted and spec.bounded and spec.ingress_limit is None


def test_smol_spells_the_pool(tmp_path: Path) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        'network "t" {\n'
        "    switch sw ports=3 cell=128 pool=710cells ingress_reserve=30cells"
        " ingress_drain=72cells\n"
        "    simulate duration=10\n}\n"
    )
    sw = interpret_to_scenario(path).nodes[0]
    assert isinstance(sw, SwitchNode)
    assert (sw.pool_cells, sw.ingress_reserve, sw.ingress_drain) == (710, 30, 72)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"ingress_reserve": Frames(3)}, "ingress_reserve needs pool"),
        ({"ingress_drain": Frames(3)}, "ingress_drain needs pool or ingress_buffer"),
        ({"pool": Frames(10), "ingress_buffer": 4_000}, "the ingress scope has one unit"),
        (
            {
                "pool": Frames(10),
                "ingress_buffer": Frames(5),
                "ingress_resume": Frames(2),
                "ingress_drain": Frames(2),
            },
            "ingress_drain and ingress_resume both end a drop state",
        ),
        (
            {"pool": Frames(10), "ingress_buffer": Frames(5), "ingress_reserve": Frames(6)},
            "ingress_reserve \\(6\\) is more than ingress_buffer",
        ),
        ({"pool": Frames(0)}, "pool in frames must be a whole number of at least 1"),
        ({"pool": Frames(10), "ingress_reserve": Cells(3)}, "ingress_reserve is given in the unit"),
        ({"pool": Frames(10), "ingress_drain": Frames(0)}, "ingress_drain must be a whole number"),
        ({"pool": Cells(10)}, "a limit in cells needs cell"),
    ],
)
def test_the_pool_is_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        Network("t").switch("sw", ports=2, **kwargs)


def test_the_pool_and_reserve_hold_the_largest_frame() -> None:
    net, _ = _two_sources(pool=Cells(8), reserve=Cells(3), cell=128)  # 11 cells < 12
    with pytest.raises(ScenarioError, match="pool and ingress_reserve hold 11 cells"):
        validate(net.to_scenario(duration=10))


def test_the_fields_are_written_only_when_set() -> None:
    net = Network("t")
    net.switch("a", ports=2, ingress_buffer=Frames(3))
    net.switch("b", ports=2, pool=Frames(40), ingress_reserve=Frames(2), ingress_drain=Frames(6))
    a, b = json.loads(net.to_scenario(duration=1).to_json())["nodes"]
    assert not {
        "pool_bytes",
        "pool_frames",
        "pool_cells",
        "ingress_reserve",
        "ingress_drain",
    } & set(a)
    assert (b["pool_frames"], b["ingress_reserve"], b["ingress_drain"]) == (40, 2, 6)
    sc = net.to_scenario(duration=1)
    assert Scenario.from_json(sc.to_json()) == sc


@pytest.mark.parametrize(
    "feature, kwargs, message",
    [
        ("buffer_pool", {"pool": Frames(40)}, "a shared buffer pool"),
        ("drain_resume", {"pool": Frames(40), "drain": Frames(6)}, "drop states ended by a drain"),
    ],
)
def test_a_backend_without_the_feature_rejects_it(
    feature: str, kwargs: dict, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    simpy = backends.REGISTRY["simpy"]
    caps = simpy.capabilities()
    reduced = backends.Capabilities(**{**caps.__dict__, "features": caps.features - {feature}})
    monkeypatch.setattr(type(simpy), "capabilities", lambda self: reduced)
    with pytest.raises(ScenarioError, match=message):
        backends.check(_two_sources(**kwargs)[0].to_scenario(duration=10), "simpy")


# --- the pool and the drain ---


def _two_sources(
    *,
    pool=None,
    reserve=None,
    drain=None,
    resume=None,
    ingress=None,
    cell: int | None = None,
    rate_a: float = 9_000,
    rate_b: float = 500,
    size_b: int = SIZE,
    pattern_a: str = "poisson",
    pattern_b: str = "poisson",
    source_mbps: float = 1_000,
) -> tuple[Network, dict]:
    """Two sources into one 100 Mb/s port: A heavy, B light."""
    net = Network("pool")
    sw = net.switch(
        "sw",
        ports=3,
        cell=cell,
        pool=pool,
        ingress_reserve=reserve,
        ingress_drain=drain,
        ingress_resume=resume,
        ingress_buffer=ingress,
        mac_table="topology",
    )
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sink = net.adapter("sink", ip="10.0.0.3")
    net.link(a, sw, speed=source_mbps, length=2)
    net.link(b, sw, speed=source_mbps, length=2)
    net.link(sink, sw, speed=100, length=2)
    a.sends(sink, rate=rate_a, size=SIZE, pattern=pattern_a)
    b.sends(sink, rate=rate_b, size=size_b, pattern=pattern_b)
    for metric in (
        "ingress_drops_total",
        "ingress_drop_episodes_total",
        "ingress_drop_time_total",
        "pool_occupancy",
        "queue_depth",
    ):
        net.observe(metric, on=sw, every=5)
    net.observe("frames_received", on=sink, every=5)
    net.observe("latency", on=sink, every=5)
    net.observe("frames_sent", on=a, every=5)
    net.observe("frames_sent", on=b, every=5)
    return net, {"sw": sw, "a": a, "b": b, "sink": sink}


class _Audit:
    """Every decision of every ingress account with a pool: the pool's use is
    what the ports hold beyond their reserves; a frame is admitted exactly
    when its port is not dropping and has room in its reserve or the pool;
    a drained drop state ends at the release at which the drain is freed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.in_reserve_while_full = 0  # admitted in the reserve with the pool full
        self.dropped_in_reserve = 0  # dropped in the drop state though it fit the reserve
        self.left: list[int] = []  # the freed count since entry, at each end of a drop state
        self.runs: dict[int, list[int]] = {}  # by account, its runs of drops
        self.running: dict[int, int] = {}
        self.intervals: dict[int, list[tuple[int, int]]] = {}  # drop states, by account
        self.entered: dict[int, int] = {}
        admit, leave = engine._Account.admit, engine._Account.leave
        audit = self

        def checked_admit(account: engine._Account, size: int) -> bool:
            sw = account.switch
            pool = None if sw is None else sw.pool
            if pool is not None:
                assert pool.used == sum(
                    max(0, acc.held - pool.reserve) for acc in sw.accounts.values()
                )
            u = account.units(size)
            dropping = account.dropping
            in_reserve = pool is not None and account.held + u <= pool.reserve
            ok = admit(account, size)
            if pool is not None:
                cap = account.limit is None or account.held + u <= account.limit
                room = (
                    in_reserve
                    or pool.used
                    + max(0, account.held + u - pool.reserve)
                    - max(0, account.held - pool.reserve)
                    <= pool.limit
                )
                assert ok == (not dropping and cap and room)
                if ok and in_reserve and pool.used + 1 > pool.limit:
                    audit.in_reserve_while_full += 1
                if not ok and dropping and in_reserve and cap:
                    audit.dropped_in_reserve += 1
            key = id(account)
            if ok:
                if key in audit.running:
                    audit.runs.setdefault(key, []).append(audit.running.pop(key))
            else:
                audit.running[key] = audit.running.get(key, 0) + 1
            if not dropping and account.dropping:
                audit.entered[key] = account.env._now
            return ok

        def checked_leave(account: engine._Account) -> None:
            if account.drain is not None:
                assert account.switch is not None
                freed = account.switch.freed - account.drain_start
                assert freed >= account.drain
                audit.left.append(freed)
            key = id(account)
            audit.intervals.setdefault(key, []).append(
                (audit.entered.pop(key, 0), account.env._now)
            )
            leave(account)

        monkeypatch.setattr(engine._Account, "admit", checked_admit)
        monkeypatch.setattr(engine._Account, "leave", checked_leave)


def test_the_pool_follows_its_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """A overloads the port; B's light stream of small frames is admitted in
    its reserve while the pool is full. A's drop states end at the release at
    which 8 frames have been freed, more than A holds beyond its reserve, so
    they drop frames that would fit in its reserve."""
    audit = _Audit(monkeypatch)
    net, _ = _two_sources(
        pool=Frames(6), reserve=Frames(3), drain=Frames(8), rate_a=9_500, size_b=64, rate_b=2_000
    )
    r = net.simulate(duration=300)
    assert audit.in_reserve_while_full > 0
    assert audit.dropped_in_reserve > 0
    assert audit.left and set(audit.left) == {8}  # one frame at a time: exactly the drain
    assert _final(r, "ingress_drop_episodes_total:sw") > 0
    assert 0 < max(v for _, v in r.metrics["pool_occupancy:sw"]) <= 6


def test_a_pool_without_a_reserve_is_shared_by_every_port(monkeypatch) -> None:
    """With no reserve, B's frames find no room once A has filled the pool."""
    audit = _Audit(monkeypatch)
    net, _ = _two_sources(pool=Frames(20), rate_a=9_500, rate_b=2_000, size_b=64)
    r = net.simulate(duration=300)
    assert audit.in_reserve_while_full == 0
    assert _final(r, "ingress_drops_total:sw") > 0


def _drain_tie() -> Network:
    """Two constant sources of 1230 B frames into one 100 Mb/s port that takes
    a frame every 100 us (as tests.test_hysteresis.hysteresis_tie), B's link
    long enough that its frames arrive at the instants of the releases: a
    drained drop state ends at the release at which a frame arrives."""
    net = Network("drain-tie")
    sw = net.switch(
        "sw",
        ports=3,
        pool=Frames(4),
        ingress_reserve=Frames(1),
        ingress_drain=Frames(2),
        mac_table="topology",
    )
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sink = net.adapter("sink", ip="10.0.0.3")
    net.link(a, sw, speed=1_000, length=2)
    net.link(b, sw, speed=1_000, length=19_810)
    net.link(sink, sw, speed=100, length=2)
    a.sends(sink, rate=10_000, size=1_230)
    b.sends(sink, rate=10_000, size=1_230)
    for metric in ("ingress_drops_total", "ingress_drop_episodes_total", "pool_occupancy"):
        net.observe(metric, on=sw, every=1)
    net.observe("frames_received", on=sink, every=1)
    return net


def test_a_drained_drop_state_ends_at_the_instant_of_an_arrival(monkeypatch) -> None:
    """At those instants the take comes first: the frame arriving there is
    decided by a port that has left its drop state."""
    from smolpy.sim import engine as e

    ends: list[int] = []
    admitted_at_end = []
    leave, admit = e._Account.leave, e._Account.admit

    def recording_leave(account: e._Account) -> None:
        ends.append(account.env._now)
        leave(account)

    def recording_admit(account: e._Account, size: int) -> bool:
        ok = admit(account, size)
        if ok and ends and ends[-1] == account.env._now:
            admitted_at_end.append(account.env._now)
        return ok

    monkeypatch.setattr(e._Account, "leave", recording_leave)
    monkeypatch.setattr(e._Account, "admit", recording_admit)
    _drain_tie().simulate(duration=50)
    assert ends and admitted_at_end


# --- identities ---


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize("unit", ["frames", "cells"])
def test_one_source_with_a_pool_and_reserve_is_an_ingress_buffer(engine_name, unit) -> None:
    """One port holding frames: pool_use = max(0, held - R), so a frame fits
    exactly when held + u <= P + R."""

    def run(**kw) -> object:
        net, _ = _two_sources(rate_a=9_500, rate_b=1, size_b=SIZE, **kw)
        # B sends nothing within the run: one ingress source
        sc = net.to_scenario(duration=300, seed=4).with_traffic(
            "flow", {"source": "b"}, delay_ms=1e6
        )
        return backends.get(engine_name).run(sc)

    wrap = Frames if unit == "frames" else Cells
    cell = None if unit == "frames" else 128
    p, r = (30, 6) if unit == "frames" else (400, 60)
    pooled = run(pool=wrap(p), reserve=wrap(r), cell=cell)
    capped = run(ingress=wrap(p + r), cell=cell)
    skip = {"pool_occupancy:sw"}
    assert {k: v for k, v in pooled.metrics.items() if k not in skip} == {
        k: v for k, v in capped.metrics.items() if k not in skip
    }
    assert pooled.stats.events == capped.stats.events
    assert _final(pooled, "ingress_drops_total:sw") > 0


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize("limit, drain", [(10, 3), (20, 8), (6, 1)])
def test_in_frames_a_drain_is_a_resume_level(engine_name, limit: int, drain: int) -> None:
    """One source, one size, in frames: a port is refused only when full, so
    it enters at L; it admits nothing while dropping, and every release is
    one of its own frames, so D freed is its occupancy at L - D."""

    def run(**kw) -> object:
        net, _ = _two_sources(rate_a=9_500, rate_b=1, ingress=Frames(limit), **kw)
        sc = net.to_scenario(duration=300, seed=6).with_traffic(
            "flow", {"source": "b"}, delay_ms=1e6
        )
        return backends.get(engine_name).run(sc)

    drained = run(drain=Frames(drain))
    resumed = run(resume=Frames(limit - drain))
    assert drained.metrics == resumed.metrics
    assert drained.stats.events == resumed.stats.events
    assert _final(drained, "ingress_drop_episodes_total:sw") > 0


# --- two sources: the run lengths ---


def _two_poisson(seed: int, monkeypatch, d: int = 6, duration: float = 4_000):
    """Two Poisson 1518 B streams of 4 000 frames/s each, from 10 Gb/s ports,
    share a pool of 40 frames with a reserve of 2 each and a drain of d."""
    audit = _Audit(monkeypatch)
    net, nodes = _two_sources(
        pool=Frames(40),
        reserve=Frames(2),
        drain=Frames(d),
        rate_a=4_100,
        rate_b=4_100,
        source_mbps=10_000,
    )
    net.simulate(duration=duration, seed=seed)
    return audit


def test_two_sources_have_runs_of_one_plus_their_arrivals_while_draining(monkeypatch) -> None:
    """A port refused while the other keeps the shared port busy: its run is
    1 + the arrivals of its own stream while d frames are freed, the residual
    of the frame in transmission (uniform, by PASTA) then d - 1 services:
    1 + (lambda / 2) (S / 2 + (d - 1) S)."""
    d = 6
    service = (tb.transmission_ns(8 + SIZE, 100) + tb.transmission_ns(12, 100)) / 1e9
    want = 1 + 4_100 * (service / 2 + (d - 1) * service)
    means = []
    for seed in range(1, 6):
        audit = _two_poisson(seed, monkeypatch, d)
        runs = [n for rs in audit.runs.values() for n in rs]
        means.append(statistics.mean(runs))
        monkeypatch.undo()
    error = statistics.stdev(means) / math.sqrt(len(means))
    assert abs(statistics.mean(means) - want) <= 4 * error + 0.02 * want, (means, want)
