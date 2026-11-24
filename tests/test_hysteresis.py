"""ADR-0014: switch buffer limits in cells, and a resume threshold."""

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
from smolpy.model.frames import frame_cells
from smolpy.model.validate import ScenarioError, check_node, validate
from smolpy.sim import engine

SIZE = 1_518


def _engines() -> list[str]:
    """The engines with the features of ADR-0014 installed here."""
    wanted = {"buffer_cells", "buffer_hysteresis"}
    return [
        name
        for name in backends.installed()
        if wanted <= backends.REGISTRY[name].capabilities().features
    ]


ENGINES = _engines()


def _final(result, key: str) -> float:
    return result.metrics[key][-1][1]


# --- the model ---


@pytest.mark.parametrize(
    "size, cells", [(64, 1), (127, 1), (128, 1), (129, 2), (512, 4), (594, 5), (1_518, 12)]
)
def test_a_frame_takes_its_cells_rounded_up(size: int, cells: int) -> None:
    assert frame_cells(size, 128) == cells


def test_cells_and_resume_levels() -> None:
    net = Network("t")
    sw = net.switch("sw", ports=2, cell=128, ingress_buffer=Cells(740), ingress_resume=Cells(668))
    spec = sw.to_spec()
    assert spec.cell_bytes == 128 and spec.ingress_limit == (740, "cells")
    assert spec.ingress_buffer_resume == 668 and spec.buffer_resume is None
    eg = net.switch("eg", ports=2, buffer=Frames(40), resume=Frames(34)).to_spec()
    assert eg.egress_limit == (40, "frames") and eg.buffer_resume == 34
    by = net.switch("by", ports=2, buffer=20_000, resume=10_000).to_spec()
    assert by.egress_limit == (20_000, "bytes") and by.buffer_resume == 10_000


def test_smol_spells_cells_and_resume_levels(tmp_path: Path) -> None:
    path = tmp_path / "t.smol"
    path.write_text(
        'network "t" {\n'
        "    switch sw ports=2 cell=128 ingress_buffer=740cells ingress_resume=668cells"
        " buffer=40frames resume=34frames\n"
        "    simulate duration=10\n}\n"
    )
    sw = interpret_to_scenario(path).nodes[0]
    assert isinstance(sw, SwitchNode)
    assert (sw.cell_bytes, sw.ingress_limit, sw.ingress_buffer_resume) == (128, (740, "cells"), 668)
    assert (sw.egress_limit, sw.buffer_resume) == ((40, "frames"), 34)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"ingress_buffer": Cells(5)}, "a limit in cells needs cell"),
        ({"cell": 128, "buffer": 4_000}, "no limit is in cells"),
        ({"cell": 0, "buffer": Cells(4)}, "cell must be a whole number of bytes, at least 1"),
        ({"cell": 128, "buffer": Cells(0)}, "buffer in cells must be a whole number of at least 1"),
        ({"resume": 3_000}, "resume needs buffer"),
        ({"buffer": Frames(5), "resume": Frames(5)}, "resume must be a whole number of frames"),
        ({"buffer": Frames(5), "resume": Frames(-1)}, "resume must be a whole number of frames"),
        ({"buffer": Frames(5), "resume": 3}, "resume is given in the unit of its scope's limit"),
        (
            {"cell": 128, "ingress_buffer": Cells(10), "ingress_resume": Frames(3)},
            "ingress_resume is given in the unit of its scope's limit: Cells",
        ),
    ],
)
def test_cells_and_resume_levels_are_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        Network("t").switch("sw", ports=2, **kwargs)


def test_one_account_has_one_unit_of_three() -> None:
    with pytest.raises(ScenarioError, match="given in more than one unit"):
        check_node(SwitchNode("sw", 2, buffer_frames=4, cell_bytes=128, buffer_cells=40))


def test_a_cell_limit_holds_the_largest_frame() -> None:
    net, _ = _one_flow(cell=128, ingress=Cells(11))  # 1518 B take 12 cells
    with pytest.raises(ScenarioError, match="ingress_buffer 11 cells is smaller than the largest"):
        validate(net.to_scenario(duration=10))


def test_the_fields_are_written_only_when_set() -> None:
    net = Network("t")
    net.switch("a", ports=2, ingress_buffer=Frames(3))
    net.switch("b", ports=2, cell=128, ingress_buffer=Cells(40), ingress_resume=Cells(30))
    a, b = json.loads(net.to_scenario(duration=1).to_json())["nodes"]
    new = {"cell_bytes", "buffer_cells", "ingress_buffer_cells", "buffer_resume"}
    assert not new & set(a) and "ingress_buffer_resume" not in a
    assert (b["cell_bytes"], b["ingress_buffer_cells"], b["ingress_buffer_resume"]) == (128, 40, 30)
    sc = net.to_scenario(duration=1)
    assert Scenario.from_json(sc.to_json()) == sc


@pytest.mark.parametrize(
    "feature, kwargs, message",
    [
        ("buffer_cells", {"cell": 128, "ingress": Cells(60)}, "buffer limits in cells"),
        ("buffer_hysteresis", {"ingress": Frames(6), "ingress_resume": Frames(3)}, "resume levels"),
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
        backends.check(_one_flow(**kwargs)[0].to_scenario(duration=10), "simpy")


# --- the drop state ---


def _one_flow(
    ingress=None,
    egress=None,
    *,
    cell: int | None = None,
    ingress_resume=None,
    resume=None,
    size: int | str = SIZE,
    rate: float = 9_000,
    pattern: str = "poisson",
    source_mbps: float = 1_000,
) -> tuple[Network, dict]:
    """One flow from a fast port to a 100 Mb/s one."""
    net = Network("one")
    sw = net.switch(
        "sw",
        ports=2,
        buffer=egress,
        ingress_buffer=ingress,
        cell=cell,
        resume=resume,
        ingress_resume=ingress_resume,
    )
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=source_mbps, length=5)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=rate, size=size, pattern=pattern)
    for metric, node in (
        ("drops_total", sw),
        ("latency", b),
        ("frames_received", b),
        ("frames_sent", a),
        ("queue_depth", sw),
    ):
        net.observe(metric, on=node, every=5)
    for scope in ("ingress", "egress"):
        for what in ("drops", "drop_episodes", "drop_time"):
            net.observe(f"{scope}_{what}_total", on=sw, every=5)
    return net, {"sw": sw, "a": a, "b": b}


class _Audit:
    """Every decision and release of every account: the drop state is
    entered exactly when a frame is refused while the account holds more than
    its resume level, and left exactly at the release that brings it to that
    level; in it, every frame is refused, even one that fits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.decisions: list[tuple[int, int, int, bool, bool, bool]] = []
        self.runs: list[int] = []  # the drops of each finished episode
        self.small_in_drop_state = 0  # refused although they fit
        self.tight = 0  # admitted at the instant of the release that ended a drop state
        self.left_at: dict[int, int] = {}
        self.running: dict[int, int] = {}  # account -> drops of its current episode
        self.starts: list[int] = []  # the instant each episode began
        admit, release = engine._Account.admit, engine._Account.release
        audit = self

        def checked_admit(account: engine._Account, size: int) -> bool:
            fits, dropping = account.fits(size), account.dropping
            ok = admit(account, size)
            assert ok == (fits and not dropping)
            if not ok:
                entered = (
                    not dropping and account.resume is not None and account.held > account.resume
                )
                assert account.dropping == (dropping or entered)
                if dropping and fits:
                    audit.small_in_drop_state += 1
                if id(account) not in audit.running:
                    audit.starts.append(account.env._now)
                audit.running[id(account)] = audit.running.get(id(account), 0) + 1
            else:
                if id(account) in audit.running:
                    audit.runs.append(audit.running.pop(id(account)))
                if audit.left_at.get(id(account)) == account.env._now:
                    audit.tight += 1
            return ok

        def checked_release(account: engine._Account, size: int) -> None:
            dropping = account.dropping
            release(account, size)
            if dropping:
                # left exactly at the release that reaches the resume level
                assert account.dropping == (account.held > account.resume)
                if not account.dropping:
                    audit.left_at[id(account)] = account.env._now

        monkeypatch.setattr(engine._Account, "admit", checked_admit)
        monkeypatch.setattr(engine._Account, "release", checked_release)


def test_the_drop_state_follows_its_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """IMIX through 60 cells of 128 B with a resume level 20 cells lower:
    small frames are dropped in the drop state although they fit, and the
    mean run is drops over episodes."""
    audit = _Audit(monkeypatch)
    net, _ = _one_flow(
        cell=128, ingress=Cells(60), ingress_resume=Cells(40), size="imix", rate=40_000
    )
    r = net.simulate(duration=300)
    assert audit.small_in_drop_state > 0
    drops, episodes = (
        _final(r, "ingress_drops_total:sw"),
        _final(r, "ingress_drop_episodes_total:sw"),
    )
    assert drops > episodes > 0
    assert _final(r, "ingress_drop_time_total:sw") > 0
    assert min(audit.runs) >= 1 and statistics.mean(audit.runs) > 2


def test_without_a_resume_level_episodes_are_runs_of_tail_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tail drop has no drop state, and each run of refusals is an episode,
    single frames among them."""
    audit = _Audit(monkeypatch)
    r = _one_flow(ingress=Frames(5), rate=9_500)[0].simulate(duration=300)
    assert _final(r, "ingress_drop_time_total:sw") == 0
    t_last = tb.ms_to_ns(r.metrics["ingress_drop_episodes_total:sw"][-1][0])
    assert _final(r, "ingress_drop_episodes_total:sw") == sum(s <= t_last for s in audit.starts)
    assert 1 in audit.runs and max(audit.runs) > 1


def hysteresis_tie() -> Network:
    """Two constant sources of 1230 B frames at 10 000 frames/s into one
    100 Mb/s port, where a frame and its gap take exactly 100 us: the port
    is overloaded twice over and takes a frame every 100 us. B's link is
    19 810 m long, 99 050 ns, so that B's frames arrive exactly at the
    instants the port takes a frame. Each ingress port holds 3 frames and
    resumes at 1 (ADR-0014)."""
    net = Network("hysteresis-tie")
    sw = net.switch(
        "sw", ports=3, ingress_buffer=Frames(3), ingress_resume=Frames(1), mac_table="topology"
    )
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sink = net.adapter("sink", ip="10.0.0.3")
    net.link(a, sw, speed=1_000, length=2)
    net.link(b, sw, speed=1_000, length=19_810)
    net.link(sink, sw, speed=100, length=2)
    a.sends(sink, rate=10_000, size=1_230)
    b.sends(sink, rate=10_000, size=1_230)
    for metric in ("ingress_drops_total", "ingress_drop_episodes_total", "ingress_drop_time_total"):
        net.observe(metric, on=sw, every=1)
    net.observe("frames_received", on=sink, every=1)
    net.observe("latency", on=sink, every=1)
    return net


def test_an_arrival_at_the_release_that_ends_the_drop_state_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At those instants the take comes first, so B's frame arriving at the
    release that ends B's drop state is admitted."""
    audit = _Audit(monkeypatch)
    hysteresis_tie().simulate(duration=50)
    assert audit.tight > 0


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize("limit", [1, 4, 12])
def test_in_frames_a_resume_level_one_below_the_limit_is_tail_drop(
    engine_name: str, limit: int
) -> None:
    """resume = limit - 1: an account refuses a frame only when full, enters
    the drop state, and leaves it at the next release, when it has room
    again. Every sample and the event count are tail drop's; only the drop
    time is new."""
    kw = {"size": "imix", "rate": 40_000, "egress": Frames(limit + 3)}
    tail = _one_flow(ingress=Frames(limit), **kw)[0].to_scenario(duration=300, seed=5)
    hyst = _one_flow(ingress=Frames(limit), ingress_resume=Frames(limit - 1), **kw)[0]
    hyst_sc = hyst.to_scenario(duration=300, seed=5)
    a, b = backends.get(engine_name).run(tail), backends.get(engine_name).run(hyst_sc)
    time = "ingress_drop_time_total:sw"
    assert {k: v for k, v in a.metrics.items() if k != time} == {
        k: v for k, v in b.metrics.items() if k != time
    }
    assert a.stats.events == b.stats.events
    assert _final(a, "ingress_drops_total:sw") > 0 and _final(b, time) > 0


@pytest.mark.parametrize("engine_name", ENGINES)
@pytest.mark.parametrize("resume", [None, 30])
def test_cells_of_the_only_frame_size_are_frames(engine_name: str, resume) -> None:
    """With one frame size and a cell of that size, every frame takes one
    cell: cells give the run frames give."""
    in_frames = _one_flow(
        ingress=Frames(40),
        ingress_resume=None if resume is None else Frames(resume),
        rate=9_000,
    )[0].to_scenario(duration=300, seed=2)
    in_cells = _one_flow(
        cell=SIZE,
        ingress=Cells(40),
        ingress_resume=None if resume is None else Cells(resume),
        rate=9_000,
    )[0].to_scenario(duration=300, seed=2)
    a, b = backends.get(engine_name).run(in_frames), backends.get(engine_name).run(in_cells)
    assert a.metrics == b.metrics and a.stats.events == b.stats.events
    assert _final(a, "ingress_drops_total:sw") > 0


def test_a_flooded_frame_in_an_egress_drop_state_loses_only_that_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """b is behind a second switch and never sends, so the first one floods
    a's frames; c's egress port is overloaded by d and goes into its drop
    state. The copies to c are dropped there, the copies to the far switch
    are not."""
    audit = _Audit(monkeypatch)
    net = Network("flood")
    sw = net.switch("sw", ports=4, buffer=Frames(6), resume=Frames(2))
    far = net.switch("far", ports=2)
    a = net.adapter("a", ip="10.0.0.1")
    c = net.adapter("c", ip="10.0.0.3")
    d = net.adapter("d", ip="10.0.0.4")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(c, sw, speed=100, length=5)
    net.link(d, sw, speed=1_000, length=5)
    net.link(sw, far, speed=1_000, length=5)
    net.link(b, far, speed=1_000, length=5)
    a.sends(b, rate=2_000, size=SIZE)
    d.sends(c, rate=9_000, size=SIZE, pattern="poisson")
    net.observe("frames_received", on=b, every=10)
    net.observe("egress_drop_episodes_total", on=sw, every=10)
    r = net.simulate(duration=301)
    assert _final(r, "egress_drop_episodes_total:sw") > 0
    # every one of a's frames reached b through the far switch
    assert r.metrics["frames_received:b"][-1] == (300.0, 599)
    assert audit.runs


# --- run lengths and the exact chain ---


def _poisson(k: int, mean: float) -> float:
    return math.exp(-mean + k * math.log(mean) - math.lgamma(k + 1)) if mean > 0 else float(k == 0)


def hysteresis_chain(rho: float, limit: int, resume: int) -> dict[str, object]:
    """One port with room for *limit* queued frames and one in transmission,
    resume level *resume*, deterministic service S = 1 and Poisson arrivals
    of rate rho: the exact loss, mean run, run distribution and mean time in
    system, in service times (ADR-0014).

    A Markov chain on (frames queued, drop state) at the instants the
    transmitter takes a frame. Given the state and the k arrivals during a
    service, the frames admitted, the drops, the entry into the drop state,
    and the next state follow. A frame arriving at an idle port is taken at
    once, so it starts a service in (0, admitting)."""
    kmax = 60 + int(10 * rho)
    pk = [_poisson(k, rho) for k in range(kmax)]
    states = [(n, d) for n in range(limit + 1) for d in (0, 1) if d == 0 or n > resume]
    index = {s: i for i, s in enumerate(states)}

    def service(n: int, d: int, k: int) -> tuple[int, int, int, int]:
        """(frames queued at the end, drop state, admitted, refused)."""
        if d:
            return n, 1, 0, k
        admitted = min(k, limit - n)
        return n + admitted, int(k > admitted), admitted, k - admitted

    m = len(states)
    p = [[0.0] * m for _ in range(m)]
    for i, (n, d) in enumerate(states):
        for k in range(kmax):
            end, dd, _, _ = service(n, d, k)
            nxt = (0, 0) if end == 0 else (end - 1, int(dd and end - 1 > resume))
            p[i][index[nxt]] += pk[k]
    # pi P = pi, sum pi = 1
    a = [[p[j][i] - (i == j) for j in range(m)] for i in range(m)]
    a[-1] = [1.0] * m
    rhs = [0.0] * (m - 1) + [1.0]
    for c in range(m):
        r = max(range(c, m), key=lambda r: abs(a[r][c]))
        a[c], a[r], rhs[c], rhs[r] = a[r], a[c], rhs[r], rhs[c]
        for r in range(m):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c], strict=True)]
                rhs[r] -= f * rhs[c]
    pi = [rhs[i] / a[i][i] for i in range(m)]

    arrivals = refused = entries = idle_starts = area = admitted_frames = 0.0
    runs: dict[int, float] = {}
    later = (limit - resume - 1) * rho  # arrivals in the services after the entering one
    for (n, d), w in zip(states, pi, strict=True):
        for k in range(kmax):
            end, dd, adm, ref = service(n, d, k)
            q = w * pk[k]
            arrivals += q * k
            refused += q * ref
            admitted_frames += q * adm
            # the queue's area: n all along, plus each admitted arrival from
            # its time, the i-th of k uniform arrivals at i / (k + 1)
            area += q * (n + sum(1 - i / (k + 1) for i in range(1, adm + 1)))
            if end == 0:
                idle_starts += q
            if not d and ref:
                entries += q
                first = ref  # refusals in the entering service, the entering one included
                for j in range(40):
                    runs[first + j] = runs.get(first + j, 0.0) + q * _poisson(j, later)
    arrivals += idle_starts  # the frame that ends an idle period
    admitted_frames += idle_starts
    return {
        "loss": refused / arrivals,
        "mean_run": refused / entries,
        "runs": {length: w / entries for length, w in runs.items()},
        "in_system": area / admitted_frames + 1,  # waiting, then its own service
    }


def test_hysteresis_chain_solution() -> None:
    """With resume = limit - 1 it is M/D/1/K (tests.test_buffers)."""
    from tests.test_buffers import md1k

    for rho, limit in ((0.5, 0), (0.9, 4), (1.3, 2)):
        want, w = md1k(rho, limit + 1)
        got = hysteresis_chain(rho, limit, limit - 1) if limit else None
        if got is not None:
            assert got["loss"] == pytest.approx(want, rel=1e-9)
            assert got["in_system"] == pytest.approx(w, rel=1e-9)
    chain = hysteresis_chain(1.1, 8, 4)
    assert sum(chain["runs"].values()) == pytest.approx(1)  # type: ignore[union-attr]
    mean = sum(k * v for k, v in chain["runs"].items())  # type: ignore[union-attr]
    assert mean == pytest.approx(chain["mean_run"], rel=1e-9)


JUMBO = 9_000  # the gap is 0.13 % of a service time
SEEDS = range(1, 6)


def _chain_run(rho: float, limit: int, resume: int, seed: int, monkeypatch, frames: int = 40_000):
    """One port as the chain: Poisson jumbo frames from a 100 Gb/s port into
    a 1 Gb/s one, through an ingress limit in frames with a resume level."""
    service = tb.transmission_ns(8 + JUMBO, 1_000) + tb.transmission_ns(12, 1_000)
    rate = rho * 1e9 / service
    duration = frames / rate * 1_000
    audit = _Audit(monkeypatch)
    net = Network("chain")
    sw = net.switch("sw", ports=2, ingress_buffer=Frames(limit), ingress_resume=Frames(resume))
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100_000, length=5)
    net.link(b, sw, speed=1_000, length=5)
    a.sends(b, rate=rate, size=JUMBO, pattern="poisson")
    every = 0.999 * duration
    for metric, node in (
        ("frames_sent", a),
        ("latency", b),
        ("ingress_drops_total", sw),
        ("ingress_drop_episodes_total", sw),
    ):
        net.observe(metric, on=node, every=every)
    m = net.simulate(duration=duration, seed=seed).metrics
    latency_ns = m["latency:b"][0][1] * 1_000
    in_system = latency_ns - tb.transmission_ns(8 + JUMBO, 100_000) - 2 * tb.propagation_ns(5)
    drops = m["ingress_drops_total:sw"][0][1]
    return {
        "loss": drops / m["frames_sent:a"][0][1],
        "mean_run": drops / m["ingress_drop_episodes_total:sw"][0][1],
        "in_system": in_system / service,
        "runs": list(audit.runs),
    }


def _within(got: list[float], want: float, slack: float) -> None:
    mean = statistics.mean(got)
    error = statistics.stdev(got) / math.sqrt(len(got))
    assert abs(mean - want) <= 4 * error + slack * want, (got, want, error)


@pytest.mark.parametrize("rho, limit, resume", [(1.1, 8, 4), (0.95, 10, 5)])
def test_a_port_with_hysteresis_is_the_exact_chain(
    rho: float, limit: int, resume: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loss, mean run and mean time in system against the chain, over five
    seeds of 40 000 frames, within four standard errors of the mean plus
    0.5 % for the first frame of a busy period, which waits less than a gap;
    and the run distribution, pooled, within three times the total variation
    distance expected of a sample of that many runs."""
    chain = hysteresis_chain(rho, limit, resume)
    runs = [_chain_run(rho, limit, resume, seed, monkeypatch) for seed in SEEDS]
    for key in ("loss", "mean_run", "in_system"):
        _within([r[key] for r in runs], chain[key], 0.005)  # type: ignore[arg-type]
    pooled = [length for r in runs for length in r["runs"]]
    counts = {length: pooled.count(length) / len(pooled) for length in set(pooled)}
    want = chain["runs"]
    assert isinstance(want, dict)
    tv = sum(abs(counts.get(k, 0) - want.get(k, 0)) for k in set(counts) | set(want)) / 2
    # E|p^ - p| is about sqrt(2 p (1 - p) / (pi n)) for each length
    expected = sum(math.sqrt(2 * q * (1 - q) / (math.pi * len(pooled))) for q in want.values()) / 2
    assert tv < 3 * expected, (tv, expected, len(pooled))


def test_the_mean_run_is_one_plus_the_arrivals_while_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 + lambda E[T]: T is the rest of the service during which the account
    is refused, then limit - resume - 1 full services; each run lasts until
    the release that brings the account to the resume level."""
    rho, limit, resume = 1.1, 8, 4
    chain = hysteresis_chain(rho, limit, resume)
    # E[T] in services: the residual is E[refused in the entering service] - 1 over rho
    runs = [_chain_run(rho, limit, resume, seed, monkeypatch, frames=20_000) for seed in SEEDS]
    pooled = [length for r in runs for length in r["runs"]]
    assert min(pooled) >= 1
    assert statistics.mean(pooled) == pytest.approx(chain["mean_run"], rel=0.03)
    assert chain["mean_run"] > 1 + (limit - resume - 1) * rho  # the residual adds to it


@pytest.mark.skipif("ns3" not in ENGINES, reason="needs ns-3 (NS3_HOME)")
def test_ns3_gives_simpys_run_in_cells_with_hysteresis() -> None:
    """Poisson IMIX and bursty traffic through ingress and egress limits in
    cells with resume levels: ns-3 draws SimPy's streams, so it drops the
    same frames, in the same episodes."""
    net = Network("random-cells")
    sw = net.switch(
        "sw",
        ports=3,
        cell=128,
        ingress_buffer=Cells(30),
        ingress_resume=Cells(20),
        buffer=Cells(40),
        resume=Cells(32),
    )
    a = net.adapter("a", ip="10.0.0.1")
    c = net.adapter("c", ip="10.0.0.3")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=1_000, length=5)
    net.link(c, sw, speed=1_000, length=5)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=20_000, size="imix", pattern="poisson")
    c.sends(b, rate=6_000, size=SIZE, pattern="bursty")
    for scope in ("ingress", "egress"):
        for what in ("drops", "drop_episodes", "drop_time"):
            net.observe(f"{scope}_{what}_total", on=sw, every=5)
    net.observe("latency", on=b, every=5)
    sc = net.to_scenario(duration=300, seed=11)
    ns3, simpy = backends.get("ns3").run(sc), backends.get("simpy").run(sc)
    assert ns3.metrics == simpy.metrics
    for scope in ("ingress", "egress"):
        assert _final(simpy, f"{scope}_drop_episodes_total:sw") > 0
