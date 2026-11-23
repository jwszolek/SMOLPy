"""Bounded switch buffers with tail drop (ADR-0011).

Step 1: the scenario. The parameter in the Python API and in .smol, its
validation, the IR (still version 2: an optional field, written only when
set) and the capability that a backend needs to run it.
"""

from __future__ import annotations

import pytest

from smolpy import Network, backends
from smolpy.lang import SMOLSemanticError
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Scenario, SwitchNode
from smolpy.model.validate import ScenarioError, validate


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


def test_no_engine_runs_a_bounded_switch_as_unbounded_when_called_directly() -> None:
    """Past backends.check(): every engine still refuses a buffer it cannot
    model. ns-3's lowering needs no ns-3 build."""
    from smolpy import ns3
    from smolpy.sim.engine import run_simulation

    sc = _cell().to_scenario(duration=10)
    with pytest.raises(ValueError, match="does not support bounded switch buffers"):
        ns3.lower(sc)
    with pytest.raises(ValueError, match="does not support bounded switch buffers"):
        run_simulation(sc)
    try:
        import smolpy_engine_rs
    except ImportError:
        return
    with pytest.raises(ValueError, match="does not support bounded switch buffers"):
        smolpy_engine_rs.run(sc.to_json())


@pytest.mark.parametrize("name", ["simpy", "rust", "ns3"])
def test_an_unbounded_switch_runs_as_before(name: str) -> None:
    _backend(name)
    backends.check(_cell(None).to_scenario(duration=10), name)
