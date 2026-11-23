"""Modbus polling with one request in flight (ADR-0012).

- Step 1: the scenario. ``outstanding`` and ``timeout`` in the Python API and
  in .smol, their validation, the IR (still version 2: optional fields,
  written only when set) and the capability that a backend needs to run them.
"""

from __future__ import annotations

import pytest

from smolpy import Network, backends
from smolpy.lang import SMOLSemanticError
from smolpy.lang.interpreter import interpret_to_scenario
from smolpy.model import Poll, Scenario
from smolpy.model.validate import ScenarioError, validate


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
