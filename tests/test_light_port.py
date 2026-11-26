"""ADR-0017, Part 2: the protection of a light port in the switch's drop
state: the pool's use counted from the total occupancy, a dynamic
threshold, and a mark at the entry of the state."""

from __future__ import annotations

import json
import math
import os
from fractions import Fraction
from pathlib import Path

import pytest

from smolpy import Cells, Frames, Network, backends
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Scenario, SwitchNode
from smolpy.model.validate import ScenarioError

from .equivalence.scenarios import LIGHT_PORT
from .light_port_audit import LightAudit

SIZE = 1_518  # 12 cells of 128 B
CELL = 128


def _engines() -> list[str]:
    return [
        name
        for name in backends.installed()
        if "light_port" in backends.REGISTRY[name].capabilities().features
    ]


ENGINES = _engines()
NET = {"ingress_drop": "switch", "ingress_drain_count": "net"}
DRAINED = {
    "cell": CELL,
    "pool": Cells(120),
    "ingress_reserve": Cells(24),
    "ingress_drain": Cells(36),
}
TESTBED = {"pool_count": "total", "alpha": 24.0, "mark": 204}  # the LS108GP's values


def _final(result, key: str) -> float:
    return result.metrics[key][-1][1]


# --- the model ---


def test_the_parameters_are_switch_parameters() -> None:
    sw = Network("t").switch(
        "sw",
        ports=3,
        **DRAINED,
        **NET,
        ingress_pool_count="total",
        ingress_alpha=24,
        ingress_mark=Cells(204),
    )
    spec = sw.to_spec()
    assert (spec.ingress_pool_count, spec.ingress_alpha, spec.ingress_mark) == ("total", 24.0, 204)
    assert isinstance(spec.ingress_alpha, float)
    plain = Network("t").switch("sw", ports=2).to_spec()
    assert (plain.ingress_pool_count, plain.ingress_alpha, plain.ingress_mark) == (None,) * 3


def test_smol_spells_them(tmp_path: Path) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        'network "t" {\n'
        "    switch sw ports=8 cell=128 pool=740cells ingress_reserve=60cells"
        " ingress_drain=72cells ingress_drop=switch ingress_drain_count=net"
        " ingress_pool_count=total ingress_alpha=24 ingress_mark=204cells\n"
        "    simulate duration=10\n}\n"
    )
    sw = interpret_to_scenario(path).nodes[0]
    assert isinstance(sw, SwitchNode)
    assert (sw.ingress_pool_count, sw.ingress_alpha, sw.ingress_mark) == ("total", 24.0, 204)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"ingress_pool_count": "all"}, "invalid ingress_pool_count 'all'"),
        ({"ingress_alpha": 0}, "ingress_alpha must be a positive number"),
        ({"ingress_alpha": -2.0}, "ingress_alpha must be a positive number"),
        ({"ingress_alpha": math.inf}, "ingress_alpha must be a positive number"),
        ({"ingress_alpha": math.nan}, "ingress_alpha must be a positive number"),
        ({"ingress_alpha": "24"}, "ingress_alpha must be a positive number"),
        ({"ingress_mark": Cells(0)}, "ingress_mark must be a whole number of cells, at least 1"),
        ({"ingress_mark": 204}, "ingress_mark is given in the unit of its scope's limit"),
    ],
)
def test_the_values_are_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        Network("t").switch("sw", ports=2, **DRAINED, **NET, **kwargs)


@pytest.mark.parametrize(
    "parameter",
    [("ingress_pool_count", "total"), ("ingress_alpha", 2.0), ("ingress_mark", Cells(48))],
)
@pytest.mark.parametrize(
    "scope",
    [{}, {"ingress_drop": "switch"}, {"ingress_drop": "switch", "ingress_drain_count": "freed"}],
)
def test_each_needs_the_drop_state_counted_net(parameter: tuple, scope: dict) -> None:
    label, value = parameter
    with pytest.raises(
        ScenarioError, match=f"{label} needs ingress_drop=switch and ingress_drain_count=net"
    ):
        Network("t").switch("sw", ports=2, **DRAINED, **scope, **{label: value})


@pytest.mark.parametrize("label, value", [("ingress_pool_count", "total"), ("ingress_alpha", 2.0)])
def test_the_count_and_the_threshold_need_a_pool(label: str, value: object) -> None:
    with pytest.raises(ScenarioError, match=f"{label} needs pool"):
        Network("t").switch(
            "sw",
            ports=2,
            ingress_buffer=Frames(20),
            ingress_drain=Frames(3),
            **NET,
            **{label: value},
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ingress_pool_count": "excess"},
        {"ingress_pool_count": "total", "ingress_alpha": 1.5},
        {"ingress_alpha": 24.0, "ingress_mark": Cells(204)},
        {"ingress_mark": Cells(1)},
    ],
)
def test_valid_combinations(kwargs: dict) -> None:
    Network("t").switch("sw", ports=2, **DRAINED, **NET, **kwargs)


def test_a_mark_without_a_pool_is_in_the_cap_s_unit() -> None:
    sw = Network("t").switch(
        "sw",
        ports=2,
        ingress_buffer=Frames(20),
        ingress_drain=Frames(3),
        **NET,
        ingress_mark=Frames(4),
    )
    assert sw.to_spec().ingress_mark == 4


def test_the_fields_are_written_only_when_set() -> None:
    net = Network("t")
    net.switch("a", ports=2, **DRAINED, **NET)
    net.switch(
        "b",
        ports=2,
        **DRAINED,
        **NET,
        ingress_pool_count="total",
        ingress_alpha=24,
        ingress_mark=Cells(204),
    )
    sc = net.to_scenario(duration=1)
    doc = json.loads(sc.to_json())
    a, b = doc["nodes"]
    assert not {"ingress_pool_count", "ingress_alpha", "ingress_mark"} & set(a)
    assert (b["ingress_pool_count"], b["ingress_alpha"], b["ingress_mark"]) == ("total", 24.0, 204)
    assert doc["version"] == 2
    assert Scenario.from_json(sc.to_json()) == sc


@pytest.mark.parametrize(
    "parameter",
    [("ingress_pool_count", "excess"), ("ingress_alpha", 2.0), ("ingress_mark", Cells(48))],
)
def test_a_backend_without_the_feature_rejects_each(
    parameter: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    simpy = backends.REGISTRY["simpy"]
    caps = simpy.capabilities()
    reduced = backends.Capabilities(**{**caps.__dict__, "features": caps.features - {"light_port"}})
    monkeypatch.setattr(type(simpy), "capabilities", lambda self: reduced)
    label, value = parameter
    net = Network("t")
    net.switch("sw", ports=2, **DRAINED, **NET, **{label: value})
    with pytest.raises(ScenarioError, match="the protection of a light port"):
        backends.check(net.to_scenario(duration=10), "simpy")
    plain = Network("t")
    plain.switch("sw", ports=2, **DRAINED, **NET)
    backends.check(plain.to_scenario(duration=10), "simpy")  # Part 1 needs no such feature


def test_ns3_lowers_them_at_the_end_of_the_switch_line() -> None:
    from smolpy import ns3

    def line(**kwargs) -> str:
        net = Network("t")
        net.switch("sw", ports=2, **DRAINED, **NET, **kwargs)
        text, _ = ns3.lower(net.to_scenario(duration=1))
        assert text.splitlines()[0] == "smol-ns3 9"
        return next(x for x in text.splitlines() if x.startswith("switch "))

    assert line().endswith(" 120 24 36 1 1 0 0 0")
    full = line(ingress_pool_count="total", ingress_alpha=24, ingress_mark=Cells(204))
    assert full.endswith(" 120 24 36 1 1 1 24.0 204")
    assert line(ingress_alpha=1.25).endswith(" 1 1 0 1.25 0")


# --- the rule, on SimPy's accounts ---


def _switch(
    *,
    pool: int = 120,
    reserve: int = 24,
    drain: int = 36,
    total: bool = True,
    alpha: float | None = None,
    mark: int | None = None,
    ports: int = 3,
):
    """A switch's ingress accounts in cells, with its drop state counted
    net, built by hand; frames are charged and released in cells."""
    from smolpy.sim import engine

    env = engine._Environment()
    ingress = engine._Ingress({}, engine._Pool(pool, reserve, total))
    for port in range(ports):
        ingress.accounts[port] = engine._Account(None, "cells", env, CELL, None, ingress, None)
    ingress.state = engine._Account(None, "cells", env, switch=ingress, drain=drain)
    ingress.net = True
    ingress.alpha = alpha
    ingress.mark = mark
    return ingress


def _hold(ingress, port: int, *frames: int) -> None:
    for cells in frames:
        ingress.accounts[port].charge(cells * CELL)


def _admit(ingress, port: int, cells: int) -> bool:
    """Decide a frame of *cells*, charging it if admitted."""
    account = ingress.accounts[port]
    ok = account.admit(cells * CELL)
    if ok:
        account.charge(cells * CELL)
    return ok


def _marks(ingress) -> list[bool]:
    return [a.marked for a in ingress.accounts.values()]


def test_total_counts_the_whole_occupancy_less_one_reserve() -> None:
    """Two ports above R of 24 and one within it: by each port's excess the
    pool holds 16 + 6, by the total 40 + 30 + 10 - 24."""
    excess, total = _switch(total=False, pool=50), _switch(total=True, pool=50)
    for sw in (excess, total):
        _hold(sw, 0, 12, 12, 12, 4)
        _hold(sw, 1, 12, 12, 6)
        _hold(sw, 2, 10)
    assert excess.pool.use() == 22 and total.pool.use() == 56
    # by the excess the pool has room for a frame of 12 more; by the total
    # it is full, yet a port within its reserve is still admitted
    assert excess.accounts[0].fits(12 * CELL)
    assert not total.accounts[0].fits(12 * CELL)
    assert total.accounts[2].fits(12 * CELL)  # 10 + 12 <= R
    assert not total.accounts[2].fits(15 * CELL)  # beyond R, and the pool is full


def _bound(alpha: float, pool: int, reserve: int, u: int) -> int:
    """The most one sender may hold before a frame of *u* beyond its reserve,
    with the pool counted by the total: (1 + α) h + u <= α (P + R) + R."""
    a = Fraction(alpha)
    return math.floor((a * (pool + reserve) + reserve - u) / (1 + a))


@pytest.mark.parametrize(
    "alpha, pool, reserve, u",
    [
        (2.0, 120, 24, 12),
        (1.5, 120, 24, 5),
        (24.0, 740, 60, 12),
        (24.0, 740, 60, 1),
        (0.5, 120, 24, 12),
    ],
)
def test_the_threshold_at_its_boundary(alpha: float, pool: int, reserve: int, u: int) -> None:
    """A port alone holding h: a frame of u fits while h + u <= R + α (P -
    (h - R)), so up to the bound and not one cell above it."""
    h = _bound(alpha, pool, reserve, u)
    assert h + u > reserve and h - reserve + u <= pool  # the threshold binds, not the pool
    for held, fits in ((h, True), (h + 1, False)):
        sw = _switch(pool=pool, reserve=reserve, alpha=alpha, ports=1)
        _hold(sw, 0, held)
        assert sw.accounts[0].fits(u * CELL) is fits, held
    assert _bound(24.0, 740, 60, 12) == 769  # 768 held in frames of 12: 780 after it


def test_the_threshold_counts_the_pool_by_its_count() -> None:
    """With two ports above R, the threshold falls with the other's
    occupancy by the total count and only with its excess by the excess."""
    for total, fits in ((False, True), (True, False)):
        sw = _switch(total=total, alpha=1.0)
        _hold(sw, 0, 12, 12, 12, 12)  # 48
        _hold(sw, 1, 12, 12, 6)  # 30: excess 6, total 54
        # threshold: excess 24 + (120 - 30) = 114; total 24 + (120 - 54) = 90
        assert sw.accounts[0].fits(60 * CELL) is fits  # 48 + 60 = 108


def _enter(sw, port: int = 2, cells: int = 12) -> None:
    assert not _admit(sw, port, cells)
    assert sw.state.dropping


def test_the_marks_at_the_entry_at_the_level_and_one_above() -> None:
    """At the entry the refused port is marked, and each port holding more
    than the level; a port at the level is not."""
    sw = _switch(mark=36)
    _hold(sw, 0, 12, 12, 12)  # 36: at the level
    _hold(sw, 1, 12, 12, 13)  # 37: one above
    _hold(sw, 2, 12, 12, 12, 12, 12)  # 60, refused: 133 - 24 + 12 > 120
    _enter(sw)
    assert _marks(sw) == [False, True, True]


def test_a_marked_port_is_refused_within_its_reserve() -> None:
    """Port 1, marked at the entry, falls to 13 cells: a frame of 5 would
    fit in its reserve, and ADR-0016's rule admits it, the mark does not."""
    for mark, admitted in ((36, False), (None, True)):
        sw = _switch(mark=mark, drain=200)
        _hold(sw, 0, 12, 12, 12)
        _hold(sw, 1, 12, 12, 13)
        _hold(sw, 2, 12, 12, 12, 12, 12)
        _enter(sw)
        for _ in range(2):
            sw.accounts[1].release(12 * CELL)
        assert sw.accounts[1].held == 13 and sw.state.dropping
        assert _admit(sw, 1, 5) is admitted


def test_an_unmarked_port_is_admitted_while_it_fits_and_marked_at_its_misfit() -> None:
    """Port 0 is unmarked. It is admitted while its frame fits, beyond its
    reserve; at its first misfit it is marked, and then refused even when
    its frame would fit again."""
    sw = _switch(mark=36, drain=200)
    _hold(sw, 0, 12, 12, 12)
    _hold(sw, 1, 12, 12, 13)
    _hold(sw, 2, 12, 12, 12, 12, 12)
    _enter(sw)
    assert sw.pool.use() == 109
    sw.accounts[2].release(12 * CELL)  # use 97: room for one frame of 12
    assert _admit(sw, 0, 12) and not sw.accounts[0].marked  # 48 held, beyond R
    assert not _admit(sw, 0, 12)  # use 109: no room
    assert sw.accounts[0].marked
    sw.accounts[2].release(12 * CELL)
    assert sw.accounts[0].fits(12 * CELL)  # it would fit, but it is marked
    assert not _admit(sw, 0, 12)


def test_the_marks_end_with_the_state() -> None:
    """Once the occupancy has fallen by D the state ends and a port marked
    in it is decided by its fit alone; at the next entry the marks are
    taken afresh."""
    sw = _switch(mark=36, drain=36)
    _hold(sw, 0, 12, 12, 12)
    _hold(sw, 1, 12, 12, 13)
    _hold(sw, 2, 12, 12, 12, 12, 12)
    _enter(sw)
    for _ in range(3):
        sw.accounts[2].release(12 * CELL)
    assert not sw.state.dropping
    assert sw.accounts[1].marked  # left as it was, and unused outside the state
    assert _admit(sw, 1, 12)  # 49 held: it fits
    for _ in range(3):
        sw.accounts[1].release(12 * CELL)  # 13 held
    _hold(sw, 2, 12, 12, 12, 12, 12)  # 84
    _enter(sw)
    assert _marks(sw) == [False, False, True]


def test_without_a_mark_the_state_admits_within_the_reserve() -> None:
    """With the pool's count and the threshold but no mark, a frame in the
    state is admitted only within its port's reserve, as in ADR-0016."""
    sw = _switch(alpha=24.0, drain=200)
    _hold(sw, 0, 12)
    _hold(sw, 2, *[12] * 11)  # 132 held, use 120
    assert not _admit(sw, 2, 12)  # 144 - 24 + 12 > 120: the entry
    for _ in range(4):
        sw.accounts[2].release(12 * CELL)
    assert sw.accounts[0].fits(12 * CELL)  # 24 held after: within the reserve
    assert _admit(sw, 0, 12)
    assert sw.accounts[1].fits(30 * CELL)  # it would fit by the pool, not by the reserve
    assert not _admit(sw, 1, 30)


# --- the rule, audited on simulated runs ---

EXPECTED = {
    "light-port-mark": (
        "marked a port that was not refused",
        "a marked port refused within its reserve",
    ),
    "light-port-admitted": ("an unmarked port admitted beyond its reserve",),
    "light-port-misfit": (
        "an unmarked port marked at a misfit",
        "refused by the dynamic threshold",
    ),
    "light-port-excess": (
        "an unmarked port marked at a misfit",
        "marked a port that was not refused",
    ),
}


@pytest.mark.parametrize("name", LIGHT_PORT)
def test_every_decision_follows_the_rule(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The equivalence scenarios, each decision against the oracle; each
    has the case it was made for."""
    audit = LightAudit(monkeypatch)
    backends.get("simpy").run(LIGHT_PORT[name]())
    assert len(audit.episodes) >= 5
    for case in EXPECTED[name]:
        assert audit.seen[case] > 0, (case, dict(audit.seen))


def _testbed_like(
    *,
    rates: tuple[float, float],
    light_size: int = SIZE,
    light_pattern: str = "constant",
    responses: float = 0,
    pool: int = 740,
    reserve: int = 60,
    drain: int = 72,
    count: str | None = "net",
    pool_count: str | None = None,
    alpha: float | None = None,
    mark: int | None = None,
    source_mbps: float = 1_000,
) -> Network:
    """A heavy Poisson Pi, a light one of *light_size* and *light_pattern*
    and, with *responses*, a port of 1-cell frames at that constant rate, the
    Waveshare's, into one 100 Mb/s port; the switch's drop state counted by
    *count*, and Part 2's parameters as given."""
    net = Network("light-port")
    kw: dict = {}
    if pool_count is not None:
        kw["ingress_pool_count"] = pool_count
    if alpha is not None:
        kw["ingress_alpha"] = alpha
    if mark is not None:
        kw["ingress_mark"] = Cells(mark)
    sw = net.switch(
        "sw",
        ports=4,
        cell=CELL,
        pool=Cells(pool),
        ingress_reserve=Cells(reserve),
        ingress_drain=Cells(drain),
        ingress_drop="switch",
        ingress_drain_count=count,  # type: ignore[arg-type]
        mac_table="topology",
        **kw,
    )
    heavy = net.adapter("heavy", ip="10.0.0.1")
    light = net.adapter("light", ip="10.0.0.2")
    ws = net.adapter("ws", ip="10.0.0.3")
    sink = net.adapter("sink", ip="10.0.0.4")
    for node in (heavy, light, ws):
        net.link(node, sw, speed=source_mbps, length=2)
    net.link(sink, sw, speed=100, length=2)
    heavy.sends(sink, rate=rates[0], size=SIZE, pattern="poisson")
    if rates[1]:
        light.sends(sink, rate=rates[1], size=light_size, pattern=light_pattern)  # type: ignore[arg-type]
    if responses:
        ws.sends(sink, rate=responses, size=64)
    for metric in (
        "ingress_drops_total",
        "ingress_drop_episodes_total",
        "ingress_drop_time_total",
        "pool_occupancy",
    ):
        net.observe(metric, on=sw, every=10)
    net.observe("frames_received", on=sink, every=10)
    return net


def test_the_testbed_s_rule_with_random_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LS108GP's values with a heavy Poisson Pi in overload, a light CBR
    one and the Waveshare's 1-cell responses: every decision follows the
    rule, the light port is protected beyond its reserve, and the 1-cell
    port, never marked, loses nothing."""
    audit = LightAudit(monkeypatch)
    net = _testbed_like(
        rates=(6_200, 2_200), responses=50, pool_count="total", alpha=24.0, mark=204
    )
    net.simulate(duration=2_000, seed=5)
    assert len(audit.episodes) > 50
    for case in (
        "an unmarked port admitted beyond its reserve",
        "refused by the dynamic threshold",
    ):
        assert audit.seen[case] > 0, (case, dict(audit.seen))
    ws = [d for d in audit.decisions if d.port == 2]
    assert len(ws) > 50 and all(d.ok for d in ws)


def test_the_total_count_holds_p_plus_r(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Pis above their reserves: counted by each port's excess the
    switch holds up to P + 2R, by the total at most P + R."""
    held = {}
    for pool_count in ("excess", "total"):
        audit = LightAudit(monkeypatch)
        net = _testbed_like(
            rates=(4_300, 4_300), light_pattern="poisson", pool=710, pool_count=pool_count
        )
        net.simulate(duration=1_000, seed=2)
        held[pool_count] = audit.max_total
        monkeypatch.undo()
    assert held["total"] <= 710 + 60 < held["excess"] <= 710 + 2 * 60


# --- identities ---


def _run(engine_name: str, net: Network, seed: int, ms: float = 300):
    return backends.get(engine_name).run(net.to_scenario(duration=ms, seed=seed))


@pytest.mark.parametrize("engine_name", ENGINES)
def test_the_defaults_are_part_1(engine_name: str) -> None:
    """ingress_pool_count=excess with no threshold and no mark is Part 1's
    run, with a light port admitted in the state."""
    kw = {"rates": (6_200, 2_200)}
    part1 = _run(engine_name, _testbed_like(**kw), 3, ms=1_000)
    excess = _run(engine_name, _testbed_like(**kw, pool_count="excess"), 3, ms=1_000)
    assert part1.metrics == excess.metrics and part1.stats.events == excess.stats.events
    marked = _run(engine_name, _testbed_like(**kw, pool_count="excess", mark=204), 3, ms=1_000)
    assert _final(part1, "ingress_drop_episodes_total:sw") > 0
    assert marked.metrics != part1.metrics


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize(
    "extra",
    [
        {"pool_count": "total"},
        {"pool_count": "total", "mark": 204},
        {"pool_count": "excess", "mark": 204},
        {"mark": 60},
    ],
)
def test_one_sender_without_alpha_is_adr_0016(engine_name: str, extra: dict) -> None:
    """One sender under ADR-0016's condition L - R >= D + u_max - u_min - 1
    (P = 710, R = 60, D = 72, frames of 12 cells): the counts are equal, its
    occupancy stays above R in the state, and it is the refused port, so
    with or without a mark nothing is admitted in it. The run is ADR-0016's,
    drained by what is freed."""
    adr_0016 = _run(engine_name, _testbed_like(rates=(9_000, 0), pool=710, count=None), 4)
    part2 = _run(engine_name, _testbed_like(rates=(9_000, 0), pool=710, **extra), 4)
    assert part2.metrics == adr_0016.metrics
    assert part2.stats.events == adr_0016.stats.events
    assert _final(part2, "ingress_drop_episodes_total:sw") > 10


def test_one_sender_with_alpha_stops_at_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """One sender with α = 24, P = 740, R = 60: beyond its reserve a frame of
    u cells is admitted exactly while the port holds at most
    ⌊(α (P + R) + R - u) / (1 + α)⌋, 769 for u = 12, so with frames of 12 it
    holds at most 780 after a frame: one frame more than ADR-0016's 768,
    with P = 710. The identity with ADR-0016 holds only without α."""
    tops = {}
    for alpha, pool in ((24.0, 740), (None, 710)):
        audit = LightAudit(monkeypatch)
        net = _testbed_like(rates=(9_000, 0), pool=pool, pool_count="total", alpha=alpha, mark=204)
        net.simulate(duration=600, seed=6)
        outside = [d for d in audit.decisions if not d.dropping and d.held[0] + d.units > 60]
        assert len(outside) > 1_000
        if alpha is not None:
            for d in outside:
                assert d.ok == (d.held[0] <= _bound(alpha, pool, 60, d.units)), d
        tops[alpha] = max(d.held[0] + d.units for d in outside if d.ok)
        monkeypatch.undo()
    assert tops == {24.0: 780, None: 768}


# --- against the quick model (stanowisko/model/symulacja_puli.py) ---

QUICK_MODEL = os.environ.get("SMOLPY_QUICK_MODEL")


@pytest.mark.skipif(QUICK_MODEL is None, reason="SMOLPY_QUICK_MODEL: the path of symulacja_puli.py")
@pytest.mark.parametrize("light, pattern", [(2_400, "const"), (3_300, "const"), (2_800, "poisson")])
def test_the_quick_model_s_loss_and_runs(light: int, pattern: str) -> None:
    """The quick model with the same streams (res_shared=True, α = 24, snap =
    204) and SMOLPy on Rust (or SimPy) agree on each stream's loss and mean
    run within their sampling errors; see benchmarks/light_port.py."""
    from benchmarks import light_port

    rows = light_port.against_quick_model(
        Path(QUICK_MODEL),  # type: ignore[arg-type]
        [(8_400 - light, light, 1_472, pattern)],
        seeds=3,
        seconds=20,
    )
    for row in rows:
        for stream in ("heavy", "light"):
            smol, quick = row[stream]["smolpy"], row[stream]["quick"]
            assert abs(smol["loss"] - quick["loss"]) <= 4 * math.hypot(
                smol["loss_se"], quick["loss_se"]
            )
