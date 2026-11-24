"""ADR-0016: a drop state shared by the whole switch."""

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
from smolpy.model.validate import ScenarioError

SIZE = 1_518


def _engines() -> list[str]:
    return [
        name
        for name in backends.installed()
        if "switch_drop_state" in backends.REGISTRY[name].capabilities().features
    ]


ENGINES = _engines()


def _final(result, key: str) -> float:
    return result.metrics[key][-1][1]


# --- the model ---


def test_the_scope_is_a_switch_parameter() -> None:
    sw = Network("t").switch(
        "sw",
        ports=3,
        cell=128,
        pool=Cells(716),
        ingress_reserve=Cells(54),
        ingress_drain=Cells(72),
        ingress_drop="switch",
    )
    assert sw.to_spec().ingress_drop == "switch"
    assert Network("t").switch("sw", ports=2).to_spec().ingress_drop is None


def test_smol_spells_the_scope(tmp_path: Path) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        'network "t" {\n'
        "    switch sw ports=3 cell=128 pool=716cells ingress_reserve=54cells"
        " ingress_drain=72cells ingress_drop=switch\n"
        "    simulate duration=10\n}\n"
    )
    sw = interpret_to_scenario(path).nodes[0]
    assert isinstance(sw, SwitchNode)
    assert (sw.pool_cells, sw.ingress_reserve, sw.ingress_drain, sw.ingress_drop) == (
        716,
        54,
        72,
        "switch",
    )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"pool": Frames(10), "ingress_drop": "switch"}, "ingress_drop=switch needs ingress_drain"),
        (
            {"ingress_buffer": Frames(5), "ingress_resume": Frames(2), "ingress_drop": "switch"},
            "ingress_drop=switch needs ingress_drain",
        ),
        (
            {"pool": Frames(10), "ingress_drain": Frames(2), "ingress_drop": "egress"},
            "invalid ingress_drop 'egress'",
        ),
        ({"ingress_drop": "switch"}, "ingress_drop=switch needs ingress_drain"),
    ],
)
def test_the_scope_is_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        Network("t").switch("sw", ports=2, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ingress_drop": "port"},  # the default, spelled out
        {"pool": Frames(10), "ingress_drain": Frames(2), "ingress_drop": "switch"},
        # per-port caps only: no reserve, so the state admits nothing
        {"ingress_buffer": Frames(5), "ingress_drain": Frames(2), "ingress_drop": "switch"},
        {
            "pool": Frames(10),
            "ingress_reserve": Frames(2),
            "ingress_buffer": Frames(6),
            "ingress_drain": Frames(2),
            "ingress_drop": "switch",
        },
    ],
)
def test_valid_combinations(kwargs: dict) -> None:
    Network("t").switch("sw", ports=2, **kwargs)


def test_the_field_is_written_only_when_set() -> None:
    net = Network("t")
    net.switch("a", ports=2, pool=Frames(40), ingress_drain=Frames(6))
    net.switch("b", ports=2, pool=Frames(40), ingress_drain=Frames(6), ingress_drop="switch")
    a, b = json.loads(net.to_scenario(duration=1).to_json())["nodes"]
    assert "ingress_drop" not in a
    assert b["ingress_drop"] == "switch"
    sc = net.to_scenario(duration=1)
    assert Scenario.from_json(sc.to_json()) == sc


@pytest.mark.parametrize("scope", ["port", "switch"])
def test_a_backend_without_the_feature_rejects_either_scope(
    scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    simpy = backends.REGISTRY["simpy"]
    caps = simpy.capabilities()
    reduced = backends.Capabilities(
        **{**caps.__dict__, "features": caps.features - {"switch_drop_state"}}
    )
    monkeypatch.setattr(type(simpy), "capabilities", lambda self: reduced)
    net = Network("t")
    net.switch("sw", ports=2, pool=Frames(40), ingress_drain=Frames(6), ingress_drop=scope)
    with pytest.raises(ScenarioError, match="the scope of the ingress drop state"):
        backends.check(net.to_scenario(duration=10), "simpy")


# --- the switch's drop state ---


def _two_sources(
    *,
    pool=None,
    reserve=None,
    drain=None,
    ingress=None,
    scope: str | None = "switch",
    cell: int | None = None,
    rate_a: float = 9_000,
    rate_b: float = 500,
    size_b: int = SIZE,
    extra_a: tuple[float, int] | None = None,
    pattern: str = "poisson",
    source_mbps: float = 1_000,
) -> Network:
    """Two sources into one 100 Mb/s port, as tests.test_pool; *extra_a* is a
    second flow of A, (rate, size)."""
    net = Network("switch-drop")
    sw = net.switch(
        "sw",
        ports=3,
        cell=cell,
        pool=pool,
        ingress_reserve=reserve,
        ingress_drain=drain,
        ingress_buffer=ingress,
        ingress_drop=scope,
        mac_table="topology",
    )
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sink = net.adapter("sink", ip="10.0.0.3")
    net.link(a, sw, speed=source_mbps, length=2)
    net.link(b, sw, speed=source_mbps, length=2)
    net.link(sink, sw, speed=100, length=2)
    a.sends(sink, rate=rate_a, size=SIZE, pattern=pattern)
    if extra_a is not None:
        a.sends(sink, rate=extra_a[0], size=extra_a[1], pattern=pattern)
    b.sends(sink, rate=rate_b, size=size_b, pattern=pattern)
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
    return net


class _Audit:
    """Every decision of SimPy's ingress accounts with a switch-wide drop
    state: outside it a frame is admitted exactly when it fits by the cap,
    the reserve and the pool, and a refusal enters the state; in it, exactly
    when it fits in its port's reserve. Each state ends at the release at
    which the switch has freed the drain since the entry, which no refusal
    in the state moves."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from smolpy.sim import engine

        self.entries: list[tuple[int, int, str]] = []  # (time, port, "cap"|"pool")
        self.left: list[int] = []  # the freed count since entry, at each end
        self.episodes: list[dict] = []  # each state: entry, end, refusals by port
        self.in_reserve = 0  # admitted in the state, in the reserve
        self.refused_with_room = 0  # refused in the state although the pool had room
        self.refused_in_state = 0
        self.ends: list[int] = []
        self.admitted_at_end = 0
        admit, leave = engine._Account._admit_in_switch, engine._Account.leave
        audit = self

        def checked(account, size: int, state) -> bool:
            sw = account.switch
            port = next(p for p, a in sw.accounts.items() if a is account)
            pool = sw.pool
            if pool is not None:
                assert pool.used == sum(max(0, a.held - pool.reserve) for a in sw.accounts.values())
            u = account.units(size)
            cap = account.limit is None or account.held + u <= account.limit
            in_reserve = account.held + u <= sw.reserve
            room = pool is None or (
                in_reserve
                or pool.used
                + max(0, account.held + u - pool.reserve)
                - max(0, account.held - pool.reserve)
                <= pool.limit
            )
            dropping, start = state.dropping, state.drain_start
            ok = admit(account, size, state)
            now = account.env._now
            if dropping:
                assert ok == in_reserve
                assert state.dropping and state.drain_start == start  # no restart
                episode = audit.episodes[-1]
                if ok:
                    audit.in_reserve += 1
                else:
                    audit.refused_in_state += 1
                    episode["refused"][port] = episode["refused"].get(port, 0) + 1
                    if cap and room and pool is not None:
                        audit.refused_with_room += 1
            else:
                assert ok == (cap and room)
                assert state.dropping == (not ok)
                if not ok:
                    audit.entries.append((now, port, "pool" if cap else "cap"))
                    audit.episodes.append(
                        {"entry": now, "first": port, "refused": {port: 1}, "freed": sw.freed}
                    )
            if ok and audit.ends and audit.ends[-1] == now:
                audit.admitted_at_end += 1
            return ok

        def checked_leave(account) -> None:
            sw = account.switch
            if sw is not None and sw.state is account:
                freed = sw.freed - account.drain_start
                assert freed >= account.drain
                assert account.drain_start == audit.episodes[-1]["freed"]
                audit.left.append(freed)
                audit.episodes[-1]["end"] = account.env._now
                audit.ends.append(account.env._now)
            leave(account)

        monkeypatch.setattr(engine._Account, "_admit_in_switch", checked)
        monkeypatch.setattr(engine._Account, "leave", checked_leave)


def test_the_switch_follows_its_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two heavy sources: either port's refusal enters the state, the other
    port is refused in it although the pool has room, each state ends at the
    release at which exactly the drain has been freed, one frame at a time,
    and the metrics count the switch's entries and its time in the state."""
    audit = _Audit(monkeypatch)
    net = _two_sources(pool=Frames(10), drain=Frames(4), rate_a=4_500, rate_b=4_500)
    r = net.simulate(duration=300)
    assert {port for _, port, _ in audit.entries} == {0, 1}
    assert audit.refused_with_room > 0 and audit.refused_in_state > 0
    assert audit.left and set(audit.left) == {4}
    both = [e for e in audit.episodes if len(e["refused"]) == 2]
    assert both  # two ports refused in one episode
    sampled = r.metrics["ingress_drop_time_total:sw"][-1][0] * 1e6  # ns
    seen = [e for e in audit.episodes if e["entry"] <= sampled]
    assert _final(r, "ingress_drop_episodes_total:sw") == len(seen)
    time = sum(min(e.get("end", sampled), sampled) - e["entry"] for e in seen)
    assert _final(r, "ingress_drop_time_total:sw") == pytest.approx(time / 1e6, abs=1e-6)
    drops = _final(r, "ingress_drops_total:sw")
    assert drops / _final(r, "ingress_drop_episodes_total:sw") > 1  # not a stream's run


def test_a_light_port_is_admitted_in_its_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    """B's light stream of small frames is admitted in its reserve while A's
    refusals hold the switch in its drop state."""
    audit = _Audit(monkeypatch)
    net = _two_sources(
        pool=Frames(10), reserve=Frames(3), drain=Frames(6), rate_a=9_500, rate_b=2_000, size_b=64
    )
    net.simulate(duration=300)
    assert audit.in_reserve > 0


def test_a_refusal_by_the_cap_enters_the_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-port cap below what the pool and reserve hold: A is refused by
    its cap, which enters the switch's state as a pool refusal does."""
    audit = _Audit(monkeypatch)
    net = _two_sources(
        pool=Frames(30), reserve=Frames(2), ingress=Frames(8), drain=Frames(3), rate_a=9_500
    )
    net.simulate(duration=200)
    assert {kind for *_, kind in audit.entries} == {"cap"}


def test_without_a_pool_the_state_admits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _Audit(monkeypatch)
    net = _two_sources(ingress=Frames(8), drain=Frames(3), rate_a=9_500, rate_b=2_000, size_b=64)
    net.simulate(duration=200)
    assert audit.entries and audit.in_reserve == 0 and audit.refused_in_state > 0


def _drop_tie() -> Network:
    """tests.test_pool's drain-tie with a switch-wide state: B's frames
    arrive at the instants of the releases, so a state ends at the release
    at which a frame arrives."""
    net = Network("drop-tie")
    sw = net.switch(
        "sw",
        ports=3,
        pool=Frames(4),
        ingress_reserve=Frames(1),
        ingress_drain=Frames(2),
        ingress_drop="switch",
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


def test_the_state_ends_at_the_instant_of_an_arrival(monkeypatch: pytest.MonkeyPatch) -> None:
    """At those instants the take comes first: the frame arriving there is
    decided by a switch that has left its state."""
    audit = _Audit(monkeypatch)
    _drop_tie().simulate(duration=50)
    assert audit.ends and audit.admitted_at_end > 0


# --- identities ---


def _run(engine_name: str, net: Network, seed: int, *, b_silent: bool = False):
    sc = net.to_scenario(duration=300, seed=seed)
    if b_silent:  # B sends nothing within the run: one ingress source
        sc = sc.with_traffic("flow", {"source": "b"}, delay_ms=1e6)
    return backends.get(engine_name).run(sc)


@pytest.mark.parametrize("engine_name", ENGINES)
def test_the_port_scope_is_the_default(engine_name: str) -> None:
    """ingress_drop=port is the same as no parameter, on two sources."""
    kw = {"pool": Frames(12), "reserve": Frames(2), "drain": Frames(4), "rate_b": 4_500}
    plain = _run(engine_name, _two_sources(scope=None, rate_a=4_500, **kw), 3)
    port = _run(engine_name, _two_sources(scope="port", rate_a=4_500, **kw), 3)
    assert plain.metrics == port.metrics and plain.stats.events == port.stats.events
    assert _final(plain, "ingress_drops_total:sw") > 0


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize(
    "case",
    [
        # frames, one size: P >= D - 1
        {"pool": Frames(30), "reserve": Frames(6), "drain": Frames(8)},
        # cells, A's frames of 12 and 1 cells: P >= D + 12 - 1 - 1; B's light
        # small frames always fit in its reserve
        {
            "pool": Cells(400),
            "reserve": Cells(60),
            "drain": Cells(72),
            "cell": 128,
            "extra_a": (2_000, 64),
            "rate_b": 200,
            "size_b": 64,
        },
        # a per-port cap, in frames: L - R = 12 - 2 >= D - 1
        {"pool": Frames(30), "reserve": Frames(2), "ingress": Frames(12), "drain": Frames(6)},
    ],
)
def test_one_sender_is_adr_0015(engine_name: str, case: dict) -> None:
    """One sender: its port stays beyond its reserve for the whole drain, so
    the switch's state admits nothing, as the port's does (ADR-0016's
    identity). With D at least the largest frame the first frame after a
    state is admitted, so the episodes agree too."""
    silent = "rate_b" not in case
    kw = {"rate_a": 9_500, **case}
    switch = _run(engine_name, _two_sources(scope="switch", **kw), 4, b_silent=silent)
    port = _run(engine_name, _two_sources(scope="port", **kw), 4, b_silent=silent)
    assert switch.metrics == port.metrics
    assert switch.stats.events == port.stats.events
    assert _final(switch, "ingress_drop_episodes_total:sw") > 0


@pytest.mark.parametrize("engine_name", ENGINES)
def test_the_identity_needs_its_condition(engine_name: str) -> None:
    """In frames, P = 4 < D - 1 = 7: the drain lets A's port fall into its
    reserve, where the switch's state admits and the port's does not."""
    kw = {"pool": Frames(4), "reserve": Frames(3), "drain": Frames(8), "rate_a": 9_500}
    switch = _run(engine_name, _two_sources(scope="switch", **kw), 4, b_silent=True)
    port = _run(engine_name, _two_sources(scope="port", **kw), 4, b_silent=True)
    assert _final(switch, "frames_received:sink") > _final(port, "frames_received:sink")


# --- two sources: an episode holds both streams' losses ---

RATE = 4_100  # frames/s of each stream


def test_two_senders_lose_their_arrivals_while_the_switch_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Poisson streams of RATE, no reserve: in the state nothing is
    admitted, so its duration T is fixed at the entry by the frames queued,
    and the arrivals after it do not depend on it. Given T, the stream
    refused first loses 1 + Poisson(RATE T), the other Poisson(RATE T)."""
    d = 6
    service = (tb.transmission_ns(8 + SIZE, 100) + tb.transmission_ns(12, 100)) / 1e9
    episodes = []
    for seed in range(1, 6):
        audit = _Audit(monkeypatch)
        net = _two_sources(
            pool=Frames(40),
            drain=Frames(d),
            rate_a=RATE,
            rate_b=RATE,
            source_mbps=10_000,
        )
        net.simulate(duration=4_000, seed=seed)
        episodes += [e for e in audit.episodes if "end" in e]
        monkeypatch.undo()
    n = len(episodes)
    t = [(e["end"] - e["entry"]) / 1e9 for e in episodes]
    first = [e["refused"][e["first"]] for e in episodes]
    other = [e["refused"].get(1 - e["first"], 0) for e in episodes]
    mean_a = RATE * statistics.mean(t)

    def close(values: list[float], want: float) -> None:
        error = statistics.stdev(values) / math.sqrt(n)
        assert abs(statistics.mean(values) - want) <= 4 * error, (statistics.mean(values), want)

    close([k - 1 for k in first], mean_a)
    close(other, mean_a)
    # the other stream is in the episode with probability 1 - E[exp(-RATE T)]
    p_both = statistics.mean(1 - math.exp(-RATE * x) for x in t)
    both = sum(k > 0 for k in other) / n
    assert abs(both - p_both) <= 4 * math.sqrt(p_both * (1 - p_both) / n), (both, p_both)
    # a stream's runs: its losses in each episode it is in
    runs = first + [k for k in other if k]
    want_runs = n + n * p_both
    want_mean = (n * (1 + mean_a) + n * mean_a) / want_runs
    single = sum(math.exp(-RATE * x) + RATE * x * math.exp(-RATE * x) for x in t)
    assert statistics.mean(runs) == pytest.approx(want_mean, rel=0.04)
    assert sum(k == 1 for k in runs) / len(runs) == pytest.approx(single / want_runs, abs=0.03)
    # T: the residual of the frame in transmission, then d - 1 services. At a
    # refusal the residual is shorter than uniform, so it is bounded, not
    # averaged: a full pool is likelier late in a service.
    residuals = [x - (d - 1) * service for x in t]
    assert all(-2e-9 < r <= service + 2e-9 for r in residuals)
    assert statistics.mean(residuals) < service / 2
    assert n > 300
