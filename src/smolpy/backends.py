"""Execution backends (ADR-0004, ADR-0007, ADR-0008).

A backend runs a :class:`~smolpy.model.Scenario` and returns a
:class:`~smolpy.core.network.SimulationResult`. Every backend declares its
:class:`Capabilities`; a scenario that uses anything outside them is rejected
before the run, with a message naming the element. Nothing is skipped
silently.

- ``simpy``: the reference implementation, always available.
- ``rust``: the native engine of ADR-0008, from the optional package
  ``smolpy-engine-rs`` (``pip install smolpy[rust]``). It declares its
  own capabilities, so this module never claims more than the engine
  supports, and the Scenario IR version it reads, which must be this
  smolpy's.
- ``ns3``: ns-3's IPv4/UDP stack on SMOL's own full-duplex Ethernet
  (ADR-0004, ``smolpy.ns3``), where ``NS3_HOME`` names an ns-3 build.

The Rust module's contract is::

    smolpy_engine_rs.IR_VERSION: int      # the Scenario IR version it reads
    smolpy_engine_rs.CAPABILITIES: dict   # the fields of Capabilities, as lists
    smolpy_engine_rs.run(scenario_json: str) -> (metrics, events, wall_s)

with *metrics* a ``dict[str, list[tuple[float, float]]]``, as in
``SimulationResult.metrics``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Protocol

from smolpy.model import Scenario

if TYPE_CHECKING:
    from smolpy.core.network import SimulationResult

DEFAULT = "simpy"


class BackendUnavailable(RuntimeError):
    """The selected backend is known but not installed."""


@dataclass(frozen=True)
class Capabilities:
    """What a backend can run. Kinds are the IR's ``kind`` values."""

    nodes: frozenset[str]
    traffic: frozenset[str]
    patterns: frozenset[str]  # of flows
    metrics: frozenset[str]
    # "imix": IMIX frame sizes; "bounded_buffer": switch buffers (ADR-0011);
    # "modbus_outstanding": polls with one request in flight (ADR-0012)
    features: frozenset[str] = frozenset()
    priorities: bool = True  # 802.1Q priorities above 0 on flows
    half_duplex: bool = False
    dashboards: bool = False  # the live and text modes of simulate()
    notes: dict[str, str] = field(default_factory=dict)  # kind -> "not yet" detail


class Backend(Protocol):
    name: str

    def available(self) -> bool: ...

    def capabilities(self) -> Capabilities: ...

    def run(self, scenario: Scenario) -> SimulationResult: ...


# --- the backends ---

_NODE_KINDS = frozenset({"adapter", "switch", "mqtt_broker", "modbus_slave", "profinet_device"})
_TRAFFIC_KINDS = frozenset({"flow", "publish", "route", "poll", "profinet_ar"})
_PATTERNS = frozenset({"constant", "poisson", "bursty", "batch"})


class SimPyBackend:
    """The reference implementation (``smolpy.sim.engine``)."""

    name = "simpy"

    def available(self) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        from smolpy.model.validate import METRIC_TARGETS

        return Capabilities(
            nodes=_NODE_KINDS,
            traffic=_TRAFFIC_KINDS,
            patterns=_PATTERNS,
            metrics=frozenset(METRIC_TARGETS),
            features=frozenset({"imix", "bounded_buffer"}),
            dashboards=True,
        )

    def run(self, scenario: Scenario) -> SimulationResult:
        from smolpy.sim.engine import run_simulation

        return run_simulation(scenario, _checked=True)  # check() has run


class RustBackend:
    """The native engine of ADR-0008, if ``smolpy_engine_rs`` is installed."""

    name = "rust"
    module_name = "smolpy_engine_rs"
    install = "pip install smolpy[rust]"

    def _module(self) -> ModuleType:
        from smolpy.model import VERSION

        try:
            module = importlib.import_module(self.module_name)
        except ImportError as exc:
            raise BackendUnavailable(
                f"the rust backend is not installed: {self.install}"
                f" (the {self.module_name} module could not be imported: {exc})"
            ) from exc
        reads = getattr(module, "IR_VERSION", None)
        if reads != VERSION:
            raise BackendUnavailable(
                f"the installed smolpy-engine-rs {getattr(module, '__version__', '?')} reads"
                f" Scenario IR version {reads}, but this smolpy writes version {VERSION}:"
                " install the smolpy-engine-rs that matches this smolpy"
            )
        return module

    def available(self) -> bool:
        try:
            self._module()
        except BackendUnavailable:
            return False
        return True

    def capabilities(self) -> Capabilities:
        caps = self._module().CAPABILITIES
        return Capabilities(
            nodes=frozenset(caps["nodes"]),
            traffic=frozenset(caps["traffic"]),
            patterns=frozenset(caps["patterns"]),
            metrics=frozenset(caps["metrics"]),
            features=frozenset(caps.get("features", ())),
            priorities=bool(caps.get("priorities", True)),
            half_duplex=bool(caps.get("half_duplex", False)),
            dashboards=False,
            notes=dict(caps.get("notes", {})),
        )

    def run(self, scenario: Scenario) -> SimulationResult:
        from smolpy.core.network import RunStats, SimulationResult

        metrics, events, wall_s = self._module().run(scenario.to_json(indent=None))
        stats = RunStats(backend=self.name, events=int(events), wall_s=float(wall_s))
        return SimulationResult(dict(metrics), scenario, stats)


class Ns3Backend:
    """ns-3 (ADR-0004, ``smolpy.ns3``), where ``NS3_HOME`` names an ns-3 build.

    It runs the whole model, headless: Ethernet (adapters, switches and flows
    with every pattern, IMIX and 802.1Q priorities) over UDP, Modbus TCP and
    MQTT over ns-3's TCP, and PROFINET RT on layer 2.
    """

    name = "ns3"
    install = (
        "set NS3_HOME to a built ns-3 3.49 tree, or run in the image"
        " ghcr.io/jwszolek/smolpy-ns3-base"
    )

    def _locate(self) -> None:
        from smolpy.ns3 import Ns3Unavailable, locate

        try:
            locate()
        except Ns3Unavailable as exc:
            raise BackendUnavailable(
                f"the ns3 backend is not available: {exc} ({self.install})"
            ) from exc

    def available(self) -> bool:
        try:
            self._locate()
        except BackendUnavailable:
            return False
        return True

    def capabilities(self) -> Capabilities:
        self._locate()
        return Capabilities(
            nodes=_NODE_KINDS,
            traffic=_TRAFFIC_KINDS,
            patterns=_PATTERNS,
            metrics=frozenset(
                {
                    "throughput",
                    "latency",
                    "bytes_sent",
                    "bytes_received",
                    "queue_depth",
                    "utilization",
                    "modbus_latency",
                    "broker_queue",
                    "pn_latency",
                    "pn_jitter",
                    "pn_watchdog_expiries",
                    "drops",
                    "drops_total",
                    "frames_sent",
                    "frames_received",
                    "modbus_unanswered",
                }
            ),
            features=frozenset({"imix", "bounded_buffer"}),
        )

    def run(self, scenario: Scenario) -> SimulationResult:
        from smolpy import ns3

        try:
            return ns3.run(scenario)
        except ns3.Ns3Unavailable as exc:  # e.g. smol-ns3 cannot be built
            raise BackendUnavailable(f"the ns3 backend is not available: {exc}") from exc


REGISTRY: dict[str, Backend] = {b.name: b for b in (SimPyBackend(), RustBackend(), Ns3Backend())}


def get(name: str) -> Backend:
    """The backend called *name*: ``ValueError`` if there is none, and
    :class:`BackendUnavailable` if it is not installed."""
    backend = REGISTRY.get(name)
    if backend is None:
        raise ValueError(f"unknown backend {name!r}, expected one of {', '.join(REGISTRY)}")
    if not backend.available():
        backend.capabilities()  # raises BackendUnavailable with the reason
    return backend


def installed() -> list[str]:
    """The names of the backends that can run here."""
    return [name for name, b in REGISTRY.items() if b.available()]


# --- checks and running ---

_LABELS = {
    "mqtt_broker": "MQTT broker",
    "modbus_slave": "Modbus slave",
    "profinet_device": "PROFINET device",
    "publish": "publish (MQTT)",
    "route": "route (MQTT)",
    "poll": "poll (Modbus TCP)",
    "profinet_ar": "PROFINET AR",
}


def check(scenario: Scenario, backend: str = DEFAULT, *, dashboard: bool = False) -> Backend:
    """Raise unless *backend* can run *scenario*: the backend exists and is
    installed, the scenario has a run section and is valid
    (``smolpy.model.validate``), and it uses only what the backend supports.
    *dashboard* asks for the live or text mode. Returns the backend."""
    from smolpy.model.validate import ScenarioError, Section, describe, validate

    b = get(backend)
    if scenario.run is None:
        raise ValueError(f"scenario {scenario.name!r} has no run configuration")
    validate(scenario)
    caps = b.capabilities()

    def unsupported(element: object, what: str, where: tuple[Section, int]) -> ScenarioError:
        return ScenarioError(
            f"{describe(element)}: the {b.name} backend does not support {what}", where
        )

    def label(kind: str) -> str:
        text = _LABELS.get(kind, kind)
        return f"{text} ({caps.notes[kind]})" if kind in caps.notes else text

    for i, node in enumerate(scenario.nodes):
        if node.kind not in caps.nodes:
            raise unsupported(node, label(node.kind), ("nodes", i))
        bounded = getattr(node, "buffer_bytes", None) is not None
        if bounded and "bounded_buffer" not in caps.features:
            raise unsupported(node, "bounded switch buffers", ("nodes", i))
    for i, link in enumerate(scenario.links):
        if not link.duplex and not caps.half_duplex:
            raise unsupported(link, "half-duplex links", ("links", i))
    for i, t in enumerate(scenario.traffic):
        if t.kind not in caps.traffic:
            raise unsupported(t, label(t.kind), ("traffic", i))
        pattern = getattr(t, "pattern", None)
        if pattern is not None and pattern not in caps.patterns:
            raise unsupported(t, f"pattern {pattern!r}", ("traffic", i))
        if getattr(t, "size", None) == "imix" and "imix" not in caps.features:
            raise unsupported(t, "IMIX frame sizes", ("traffic", i))
        closed = getattr(t, "outstanding", None) is not None
        if closed and "modbus_outstanding" not in caps.features:
            raise unsupported(t, "polls with one request in flight", ("traffic", i))
        if getattr(t, "priority", 0) > 0 and not caps.priorities:
            raise unsupported(t, "802.1Q priorities", ("traffic", i))
    for i, o in enumerate(scenario.observations):
        if o.metric not in caps.metrics:
            raise unsupported(o, f"metric {o.metric!r}", ("observations", i))
    if dashboard and not caps.dashboards:
        raise ValueError(
            f"the {b.name} backend runs headless: the live and text modes need backend='simpy'"
        )
    return b


def run(scenario: Scenario, backend: str = DEFAULT) -> SimulationResult:
    """Check *scenario* against *backend* and run it there."""
    return check(scenario, backend).run(scenario)
