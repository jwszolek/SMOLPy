"""The backend registry and capability checks (ADR-0008, step 2)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from smolpy import Network, backends, run_many
from smolpy.backends import BackendUnavailable, Capabilities
from smolpy.core.network import RunStats, SimulationResult
from smolpy.model.validate import METRIC_TARGETS, ScenarioError


def _net() -> Network:
    net = Network("cell")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    sw = net.switch("sw", ports=3)
    net.link(a, sw, speed=100, length=10)
    net.link(b, sw, speed=100, length=10)
    a.sends(b, rate=500, size=512)
    net.observe("throughput", on=b, every=10)
    return net


class _Fake:
    """A backend that supports only flows and throughput, and reports runs."""

    name = "fake"

    def __init__(self) -> None:
        self.runs: list = []

    def available(self) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            nodes=frozenset({"adapter", "switch"}),
            traffic=frozenset({"flow"}),
            patterns=frozenset({"constant"}),
            metrics=frozenset({"throughput"}),
            notes={"poll": "not yet"},
        )

    def run(self, scenario):
        self.runs.append(scenario)
        return SimulationResult({"throughput:b": [(10.0, 1.0)]}, scenario, RunStats("fake", 1, 0.0))


@pytest.fixture
def fake(monkeypatch) -> _Fake:
    backend = _Fake()
    monkeypatch.setitem(backends.REGISTRY, "fake", backend)
    return backend


@pytest.fixture
def no_rust(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "smolpy_engine_rs", None)  # import raises ImportError


def test_registry() -> None:
    assert list(backends.REGISTRY) == ["simpy", "rust"]
    assert "simpy" in backends.installed()
    with pytest.raises(ValueError, match="unknown backend 'ns3', expected one of simpy, rust"):
        backends.get("ns3")


def test_simpy_supports_the_whole_model() -> None:
    caps = backends.get("simpy").capabilities()
    assert caps.metrics == set(METRIC_TARGETS)
    assert caps.nodes == {"adapter", "switch", "mqtt_broker", "modbus_slave", "profinet_device"}
    assert caps.traffic == {"flow", "publish", "route", "poll", "profinet_ar"}
    assert caps.dashboards and not caps.half_duplex


def test_simulate_on_the_default_backend_is_simpy() -> None:
    net = _net()
    result = net.simulate(duration=50, backend="simpy")
    assert result.stats is not None and result.stats.backend == "simpy"
    assert result.metrics == _net().simulate(duration=50).metrics


def test_rust_reports_that_it_is_not_installed(no_rust) -> None:
    assert "rust" not in backends.installed()
    with pytest.raises(
        BackendUnavailable, match=r"rust backend is not installed: pip install smolpy\[rust\]"
    ):
        _net().simulate(duration=50, backend="rust")
    with pytest.raises(BackendUnavailable, match="not installed"):
        run_many(_net(), runs=2, duration=50, backend="rust", workers=1)


def test_a_backend_runs_the_checked_scenario(fake) -> None:
    result = _net().simulate(duration=50, seed=7, backend="fake")
    assert result.stats.backend == "fake"  # type: ignore[union-attr]
    (scenario,) = fake.runs
    assert scenario.run.duration_ms == 50 and scenario.run.seed == 7
    batch = run_many(_net(), runs=3, duration=50, backend="fake", workers=1)
    assert [r.seed for r in batch.runs] == [42, 43, 44] and len(fake.runs) == 4


def test_capabilities_are_checked_before_the_run(fake) -> None:
    net = _net()
    slave = net.modbus_slave("meter", ip="10.0.0.3", unit_id=1)
    net.observe("bytes_received", on=net._nodes["b"], every=10)
    with pytest.raises(ScenarioError, match="the fake backend does not support Modbus slave") as e:
        net.simulate(duration=50, backend="fake")
    assert e.value.where == ("nodes", 3)
    assert fake.runs == []
    del slave

    net = _net()
    net.observe("bytes_received", on=net._nodes["b"], every=10)
    with pytest.raises(
        ScenarioError,
        match="observe bytes_received on b: the fake backend does not support metric",
    ):
        net.simulate(duration=50, backend="fake")

    net = _net()
    net._nodes["a"].sends(net._nodes["b"], rate=10, size=64, pattern="poisson")
    with pytest.raises(ScenarioError, match="does not support pattern 'poisson'"):
        net.simulate(duration=50, backend="fake")


def test_a_note_explains_what_is_missing(fake, monkeypatch) -> None:
    from dataclasses import replace

    caps = fake.capabilities()
    caps = replace(caps, nodes=caps.nodes | {"modbus_slave"})
    monkeypatch.setattr(fake, "capabilities", lambda: caps)
    net = _net()
    meter = net.modbus_slave("meter", ip="10.0.0.3", unit_id=1)
    net.link(meter, net._nodes["sw"], speed=100, length=10)
    net._nodes["a"].polls(meter, register=0, count=1, rate=10)
    with pytest.raises(
        ScenarioError,
        match=r"poll a -> meter: the fake backend does not support poll \(Modbus TCP\) \(not yet\)",
    ):
        net.simulate(duration=50, backend="fake")


def test_dashboards_need_a_backend_that_has_them(fake) -> None:
    with pytest.raises(ValueError, match="the fake backend runs headless"):
        _net().simulate(duration=50, text=True, backend="fake")
    assert fake.runs == []


def test_half_duplex_links(fake) -> None:
    from dataclasses import replace

    sc = _net().to_scenario(duration=50)
    sc = replace(sc, links=(replace(sc.links[0], duplex=False), *sc.links[1:]))
    with pytest.raises(ScenarioError, match="the fake backend does not support half-duplex links"):
        backends.run(sc, "fake")


def _smolpy(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['smolpy_engine_rs'] = None\n"
            "from smolpy.cli import main; main()",
            "run",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


_SMOL = """
network "cell" {
    adapter a ip=10.0.0.1
    adapter b ip=10.0.0.2
    switch  sw ports=2
    link a -- sw speed=100 length=10
    link b -- sw speed=100 length=10
    flow a -> b rate=500 size=512
    observe throughput on b every=50
    simulate duration=200%s
}
"""


def _flat(done: subprocess.CompletedProcess[str]) -> str:
    return " ".join((done.stdout + done.stderr).split())


def test_cli_backend(tmp_path) -> None:
    f = tmp_path / "cell.smol"
    f.write_text(_SMOL % "")
    done = _smolpy(str(f), "--backend", "simpy")
    assert done.returncode == 0, _flat(done)
    done = _smolpy(str(f), "--backend", "rust")
    assert done.returncode == 1 and "rust backend is not installed" in _flat(done)
    done = _smolpy(str(f), "--backend", "ns3")
    assert done.returncode == 1 and "unknown backend 'ns3'" in _flat(done)
    script = tmp_path / "s.py"
    script.write_text("print('hi')\n")
    done = _smolpy("--backend", "simpy", str(script))
    assert done.returncode == 1 and "--backend apply to .smol files" in _flat(done)
