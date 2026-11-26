"""ADR-0017, Part 1: the drain of the switch's drop state counted net."""

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

from .equivalence.scenarios import drain_net_tie

SIZE = 1_518


def _engines() -> list[str]:
    return [
        name
        for name in backends.installed()
        if "drain_net" in backends.REGISTRY[name].capabilities().features
    ]


ENGINES = _engines()


def _final(result, key: str) -> float:
    return result.metrics[key][-1][1]


# --- the model ---


def test_the_count_is_a_switch_parameter() -> None:
    sw = Network("t").switch(
        "sw",
        ports=3,
        cell=128,
        pool=Cells(710),
        ingress_reserve=Cells(60),
        ingress_drain=Cells(72),
        ingress_drop="switch",
        ingress_drain_count="net",
    )
    assert sw.to_spec().ingress_drain_count == "net"
    assert Network("t").switch("sw", ports=2).to_spec().ingress_drain_count is None


def test_smol_spells_the_count(tmp_path: Path) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        'network "t" {\n'
        "    switch sw ports=8 cell=128 pool=710cells ingress_reserve=60cells"
        " ingress_drain=72cells ingress_drop=switch ingress_drain_count=net\n"
        "    simulate duration=10\n}\n"
    )
    sw = interpret_to_scenario(path).nodes[0]
    assert isinstance(sw, SwitchNode)
    assert (sw.ingress_drain, sw.ingress_drop, sw.ingress_drain_count) == (72, "switch", "net")


DRAINED = {"pool": Frames(10), "ingress_drain": Frames(2)}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({**DRAINED, "ingress_drain_count": "net"}, "needs ingress_drop=switch"),
        ({**DRAINED, "ingress_drain_count": "freed"}, "needs ingress_drop=switch"),
        (
            {**DRAINED, "ingress_drop": "port", "ingress_drain_count": "net"},
            "needs ingress_drop=switch",
        ),
        (
            {
                "ingress_buffer": Frames(5),
                "ingress_resume": Frames(2),
                "ingress_drain_count": "net",
            },
            "needs ingress_drop=switch",
        ),
        (
            {**DRAINED, "ingress_drop": "switch", "ingress_drain_count": "all"},
            "invalid ingress_drain_count 'all'",
        ),
        (
            {"pool": Frames(10), "ingress_drop": "switch", "ingress_drain_count": "net"},
            "ingress_drop=switch needs ingress_drain",
        ),
    ],
)
def test_the_count_is_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        Network("t").switch("sw", ports=2, **kwargs)


@pytest.mark.parametrize("count", ["freed", "net"])
@pytest.mark.parametrize(
    "kwargs",
    [
        DRAINED,
        # per-port caps only: no reserve, so the state admits nothing
        {"ingress_buffer": Frames(5), "ingress_drain": Frames(2)},
        {**DRAINED, "ingress_reserve": Frames(2), "ingress_buffer": Frames(6)},
    ],
)
def test_valid_combinations(kwargs: dict, count: str) -> None:
    Network("t").switch("sw", ports=2, ingress_drop="switch", ingress_drain_count=count, **kwargs)


def test_the_field_is_written_only_when_set() -> None:
    net = Network("t")
    net.switch("a", ports=2, ingress_drop="switch", **DRAINED)
    net.switch("b", ports=2, ingress_drop="switch", ingress_drain_count="net", **DRAINED)
    net.switch("c", ports=2, ingress_drop="switch", ingress_drain_count="freed", **DRAINED)
    sc = net.to_scenario(duration=1)
    a, b, c = json.loads(sc.to_json())["nodes"]
    assert "ingress_drain_count" not in a
    assert (b["ingress_drain_count"], c["ingress_drain_count"]) == ("net", "freed")
    assert json.loads(sc.to_json())["version"] == 2
    assert Scenario.from_json(sc.to_json()) == sc


@pytest.mark.parametrize("count", ["freed", "net"])
def test_a_backend_without_the_feature_rejects_either_count(
    count: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    simpy = backends.REGISTRY["simpy"]
    caps = simpy.capabilities()
    reduced = backends.Capabilities(**{**caps.__dict__, "features": caps.features - {"drain_net"}})
    monkeypatch.setattr(type(simpy), "capabilities", lambda self: reduced)
    net = Network("t")
    net.switch("sw", ports=2, ingress_drop="switch", ingress_drain_count=count, **DRAINED)
    with pytest.raises(ScenarioError, match="the count of the drain"):
        backends.check(net.to_scenario(duration=10), "simpy")
    plain = Network("t")
    plain.switch("sw", ports=2, ingress_drop="switch", **DRAINED)
    backends.check(plain.to_scenario(duration=10), "simpy")  # no field, no feature needed


# --- the net drain ---


def _two_sources(
    *,
    count: str | None = "net",
    scope: str | None = "switch",
    pool=None,
    reserve=None,
    drain=None,
    ingress=None,
    cell: int | None = None,
    rate_a: float = 9_500,
    rate_b: float = 800,
    size_b: int = 64,
    extra_a: tuple[float, int] | None = None,
    source_mbps: float = 1_000,
) -> Network:
    """A heavy Poisson source A and a light one B into one 100 Mb/s port, as
    tests.test_switch_drop; *extra_a* is a second flow of A, (rate, size)."""
    net = Network("drain-net")
    sw = net.switch(
        "sw",
        ports=3,
        cell=cell,
        pool=pool,
        ingress_reserve=reserve,
        ingress_drain=drain,
        ingress_buffer=ingress,
        ingress_drop=scope,
        ingress_drain_count=count,
        mac_table="topology",
    )
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sink = net.adapter("sink", ip="10.0.0.3")
    net.link(a, sw, speed=source_mbps, length=2)
    net.link(b, sw, speed=source_mbps, length=2)
    net.link(sink, sw, speed=100, length=2)
    a.sends(sink, rate=rate_a, size=SIZE, pattern="poisson")
    if extra_a is not None:
        a.sends(sink, rate=extra_a[0], size=extra_a[1], pattern="poisson")
    b.sends(sink, rate=rate_b, size=size_b, pattern="poisson")
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


class _NetAudit:
    """Every decision and every release of SimPy's switch-wide drop state
    with its drain counted net (or freed, for comparison). At each release
    in the state the fall of the occupancy since the entry is what has been
    freed less what has been charged in the state, and the state ends
    exactly at the first release at which that reaches the drain. In the
    state a frame is admitted exactly when it fits in its port's reserve,
    and a refusal moves neither the entry's occupancy nor its freed count."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from smolpy.sim import engine

        self.episodes: list[dict] = []
        self.refused_in_state = 0
        self.refused: dict[int, int] = {}  # by port, in or out of the state
        self.max_held: dict[int, int] = {}  # each port's occupancy at its decisions
        admit, charge = engine._Account._admit_in_switch, engine._Account.charge
        free = engine._Ingress.free
        audit = self

        def checked_admit(account, size: int, state) -> bool:
            sw = account.switch
            port = next(p for p, a in sw.accounts.items() if a is account)
            assert sw.held == sum(a.held for a in sw.accounts.values())
            in_reserve = account.held + account.units(size) <= sw.reserve
            audit.max_held[port] = max(audit.max_held.get(port, 0), account.held)
            dropping = state.dropping
            before = (state.drain_start, sw.entry_held)
            ok = admit(account, size, state)
            now = account.env._now
            if not ok:
                audit.refused[port] = audit.refused.get(port, 0) + 1
            if dropping:
                episode = audit.episodes[-1]
                assert ok == in_reserve
                assert state.dropping and (state.drain_start, sw.entry_held) == before
                freed = sw.freed - episode["freed"]
                if ok:
                    episode["admitted"].append((now, port, freed))
                else:
                    audit.refused_in_state += 1
            elif not ok:
                assert state.dropping and sw.entry_held == sw.held
                audit.episodes.append(
                    {
                        "entry": now,
                        "held": sw.held,
                        "freed": sw.freed,
                        "charged": 0,
                        "admitted": [],  # (time, port, freed since the entry)
                        "freed_end": None,  # when the freed count reached the drain
                    }
                )
            return ok

        def checked_charge(account, size: int) -> None:
            sw = account.switch
            if sw is not None and sw.state is not None and sw.state.dropping:
                audit.episodes[-1]["charged"] += account.units(size)
            charge(account, size)

        def checked_free(sw, u: int) -> None:
            state = sw.state
            dropping = state is not None and state.dropping
            free(sw, u)
            if not dropping:
                return
            episode = audit.episodes[-1]
            now = state.env._now
            freed = sw.freed - episode["freed"]
            net = episode["held"] - sw.held
            assert net == freed - episode["charged"]
            if episode["freed_end"] is None and freed >= state.drain:
                episode["freed_end"] = now
            fallen = net if sw.net else freed
            assert state.dropping == (fallen < state.drain)
            if not state.dropping:
                episode.update(end=now, net=net, freed_total=freed, last=u)

        monkeypatch.setattr(engine._Account, "_admit_in_switch", checked_admit)
        monkeypatch.setattr(engine._Account, "charge", checked_charge)
        monkeypatch.setattr(engine._Ingress, "free", checked_free)

    def ended(self) -> list[dict]:
        return [e for e in self.episodes if "end" in e]


def test_the_state_ends_when_the_occupancy_has_fallen_by_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In frames: each state ends at the release at which the occupancy is
    exactly D below its value at the entry, and a light frame admitted in
    the state delays that by one frame: the switch has then freed D plus
    the frames admitted in it."""
    audit = _NetAudit(monkeypatch)
    d = 6
    net = _two_sources(pool=Frames(10), reserve=Frames(3), drain=Frames(d), rate_b=2_000)
    net.simulate(duration=300)
    ended = audit.ended()
    assert len(ended) > 50
    assert all(e["net"] == d for e in ended)
    assert all(e["freed_total"] == d + e["charged"] for e in ended)
    delayed = [e for e in ended if e["charged"] > 0]
    assert delayed and all(e["end"] > e["freed_end"] for e in delayed)
    assert all(e["end"] == e["freed_end"] for e in ended if e["charged"] == 0)


def test_a_frame_admitted_in_the_state_delays_the_end_by_its_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In cells, with A's frames of 12 and 1 cells and B's of 3: each state
    ends at the first release at which the switch has freed D plus the
    units admitted in it, so the net fall is at least D and was below D
    before the last frame taken."""
    audit = _NetAudit(monkeypatch)
    d = 72
    net = _two_sources(
        pool=Cells(300),
        reserve=Cells(40),
        drain=Cells(d),
        cell=128,
        extra_a=(1_000, 64),
        rate_b=2_000,
        size_b=300,
    )
    net.simulate(duration=300)
    ended = audit.ended()
    assert len(ended) > 50
    for e in ended:
        assert e["net"] == e["freed_total"] - e["charged"]
        assert d <= e["net"] < d + e["last"]
    assert any(e["charged"] > 0 and e["freed_total"] >= d + e["charged"] for e in ended)


def test_a_refusal_in_the_state_does_not_restart_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A is refused in the state again and again; the audit checks that no
    refusal moves the entry's occupancy or freed count, so each state ends
    at the fall of D from the entry."""
    audit = _NetAudit(monkeypatch)
    net = _two_sources(pool=Frames(10), reserve=Frames(3), drain=Frames(6), rate_b=2_000)
    net.simulate(duration=200)
    assert audit.refused_in_state > len(audit.episodes) > 0
    assert all(e["net"] == 6 for e in audit.ended())


def test_with_freed_admissions_do_not_delay_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same run counted freed: each state ends once D has been freed,
    whatever was admitted in it."""
    audit = _NetAudit(monkeypatch)
    net = _two_sources(
        count="freed", pool=Frames(10), reserve=Frames(3), drain=Frames(6), rate_b=2_000
    )
    net.simulate(duration=200)
    ended = audit.ended()
    assert any(e["charged"] for e in ended)
    assert all(e["freed_total"] == 6 and e["end"] == e["freed_end"] for e in ended)


def test_a_light_frame_is_admitted_at_the_release_that_ends_freed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The equivalence scenario drain-net-tie: a frame of B arrives at the
    release at which the switch has freed D, after it in event order, and
    is admitted in the state, which goes on; counted freed it finds the
    state ended."""
    audit = _NetAudit(monkeypatch)
    drain = 6
    backends.get("simpy").run(drain_net_tie())
    tie = [
        (e, when)
        for e in audit.ended()
        for when, port, freed in e["admitted"]
        if port == 1 and when == e["freed_end"] and freed >= drain
    ]
    assert tie and all(e["charged"] > 0 for e, _ in tie)
    monkeypatch.undo()
    audit = _NetAudit(monkeypatch)
    backends.get("simpy").run(drain_net_tie("freed"))
    assert all(freed < drain for e in audit.episodes for _, _, freed in e["admitted"])


@pytest.mark.parametrize("engine_name", ENGINES)
def test_the_tie_differs_from_freed(engine_name: str) -> None:
    """drain-net-tie counted net is not the same run as counted freed: the
    states are longer and fewer, and SimPy's and every engine's event counts
    are the same."""
    engine = backends.get(engine_name)
    net, freed = engine.run(drain_net_tie()), engine.run(drain_net_tie("freed"))
    assert net.metrics != freed.metrics
    reference = backends.get("simpy").run(drain_net_tie())
    assert net.metrics == reference.metrics
    if engine_name == "rust":
        assert net.stats.events == reference.stats.events


# --- identities ---


def _run(engine_name: str, net: Network, seed: int, *, b_silent: bool = False):
    sc = net.to_scenario(duration=300, seed=seed)
    if b_silent:  # B sends nothing within the run: one ingress source
        sc = sc.with_traffic("flow", {"source": "b"}, delay_ms=1e6)
    return backends.get(engine_name).run(sc)


@pytest.mark.parametrize("engine_name", ENGINES)
def test_freed_is_the_default(engine_name: str) -> None:
    """No count is the same as ingress_drain_count=freed, with a light port
    admitted in the state, where net would differ."""
    kw = {"pool": Frames(10), "reserve": Frames(3), "drain": Frames(6), "rate_b": 2_000}
    plain = _run(engine_name, _two_sources(count=None, **kw), 3)
    freed = _run(engine_name, _two_sources(count="freed", **kw), 3)
    net = _run(engine_name, _two_sources(count="net", **kw), 3)
    assert plain.metrics == freed.metrics and plain.stats.events == freed.stats.events
    assert _final(plain, "ingress_drop_episodes_total:sw") > 0
    assert net.metrics != freed.metrics


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize(
    "case",
    [
        # frames, one size: P >= D - 1
        {"pool": Frames(30), "reserve": Frames(6), "drain": Frames(8)},
        # cells, A's frames of 12 and 1 cells: P >= D + 12 - 1 - 1
        {
            "pool": Cells(400),
            "reserve": Cells(60),
            "drain": Cells(72),
            "cell": 128,
            "extra_a": (2_000, 64),
        },
        # a per-port cap, in frames: L - R = 12 - 2 >= D - 1
        {"pool": Frames(30), "reserve": Frames(2), "ingress": Frames(12), "drain": Frames(6)},
    ],
)
def test_one_sender_net_is_freed(engine_name: str, case: dict) -> None:
    """One sender under ADR-0016's condition L - R >= D + u_max - u_min - 1:
    nothing is admitted in the state, so the net fall is what was freed at
    every release, and net gives freed's run exactly."""
    kw = {"rate_a": 9_500, **case}
    net = _run(engine_name, _two_sources(count="net", **kw), 4, b_silent=True)
    freed = _run(engine_name, _two_sources(count="freed", **kw), 4, b_silent=True)
    assert net.metrics == freed.metrics
    assert net.stats.events == freed.stats.events
    assert _final(net, "ingress_drop_episodes_total:sw") > 0


# --- the duration of the state with a light port ---

RATE_A = 8_000  # frames/s of the heavy stream
RATE_L = 800  # of the light one


def test_the_state_lasts_t0_over_one_less_the_light_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heavy Poisson stream overloads the port, and a light Poisson port
    of the same frames (u_l = 12 cells) always fits in its reserve, so all
    its frames are admitted in the state, which ends once the egress has
    taken D plus what they added. Its duration T then has the mean
    T0 / (1 - λ_l u_l τ), τ the egress time per cell, T0 the duration with
    no admissions (Wald's identity: each light frame admitted adds a
    service). T0 is each episode's own: the time at which the switch had
    freed D."""
    d, u = 72, 12
    service = (tb.transmission_ns(8 + SIZE, 100) + tb.transmission_ns(12, 100)) / 1e9
    load = RATE_L * u * (service / u)  # λ_l u_l τ
    episodes = []
    for seed in range(1, 6):
        audit = _NetAudit(monkeypatch)
        net = _two_sources(
            pool=Cells(480),
            reserve=Cells(360),
            drain=Cells(d),
            cell=128,
            rate_a=RATE_A,
            rate_b=RATE_L,
            size_b=SIZE,
            source_mbps=10_000,
        )
        net.simulate(duration=4_000, seed=seed)
        assert audit.refused.get(1, 0) == 0  # the light port always fits
        assert audit.max_held[1] + u <= 360
        episodes += audit.ended()
        monkeypatch.undo()
    n = len(episodes)
    t = [(e["end"] - e["entry"]) / 1e9 for e in episodes]
    t0 = [(e["freed_end"] - e["entry"]) / 1e9 for e in episodes]
    # the egress is busy throughout: T0 is the residual and D/u - 1 services,
    # and each light frame admitted adds one service
    assert all((d // u - 1) * service - 2e-9 < x <= d // u * service + 2e-9 for x in t0)
    for e, x, x0 in zip(episodes, t, t0, strict=True):
        assert x - x0 == pytest.approx(e["charged"] // u * service, abs=2e-9 * (1 + e["charged"]))
    want = statistics.mean(t0) / (1 - load)
    error = statistics.stdev(a * (1 - load) - b for a, b in zip(t, t0, strict=True))
    error /= (1 - load) * math.sqrt(n)
    assert abs(statistics.mean(t) - want) <= 4 * error, (statistics.mean(t), want, error)
    assert statistics.mean(t) > statistics.mean(t0) * (1 + load / 2)  # longer than freed's
    assert n > 1_000
