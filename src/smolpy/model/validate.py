"""Every semantic rule of the Scenario model, in one place (ADR-0007).

:func:`validate` checks a complete scenario; every backend runs it before
simulating, so nothing invalid is ever executed or silently ignored. The
``check_*`` functions hold the rules about a single element. The Python
builder calls them as each element is declared, so mistakes are reported at
the call that made them.

Errors are :class:`ScenarioError`, a ``ValueError`` that names the element
(``flow tx -> rx: priority 9 out of range 0-7``) and records where it sits in
the scenario, so the ``.smol`` front end can add the source line.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from typing import Literal

from smolpy.model.frames import PN_CYCLES_MS, PN_MAX_IO_BYTES
from smolpy.model.scenario import (
    AdapterNode,
    Flow,
    Link,
    ModbusSlaveNode,
    MqttBrokerNode,
    NodeSpec,
    Observation,
    Poll,
    ProfinetAR,
    ProfinetDeviceNode,
    Publish,
    Route,
    RunConfig,
    Scenario,
    SwitchNode,
    TrafficSpec,
)

Section = Literal["nodes", "links", "traffic", "observations", "run"]


class ScenarioError(ValueError):
    """An invalid scenario. *where* is ``(section, index)`` of the element,
    e.g. ``("traffic", 3)``; ``None`` when no single element is at fault."""

    def __init__(self, message: str, where: tuple[Section, int] | None = None) -> None:
        super().__init__(message)
        self.where = where


# Builder class names: what users write in Python and see in .smol errors.
KIND_NAMES = {
    "adapter": "Adapter",
    "switch": "Switch",
    "mqtt_broker": "MQTTBroker",
    "modbus_slave": "ModbusSlave",
    "profinet_device": "ProfinetDevice",
}

ENDPOINTS = (AdapterNode, MqttBrokerNode, ModbusSlaveNode, ProfinetDeviceNode)
BRIDGES = (SwitchNode,)

# Metric -> node types it can observe; None means any node. Only metrics the
# engines implement are listed: "frame_loss" has no implementation yet.
METRIC_TARGETS: dict[str, tuple[type, ...] | None] = {
    "throughput": (AdapterNode,),
    "latency": (AdapterNode,),
    "bytes_sent": (AdapterNode,),
    "bytes_received": (AdapterNode,),
    "modbus_latency": (AdapterNode,),
    "queue_depth": (SwitchNode,),
    "broker_queue": (MqttBrokerNode,),
    "pn_latency": (AdapterNode, ProfinetDeviceNode),
    "pn_jitter": (AdapterNode, ProfinetDeviceNode),
    "pn_watchdog_expiries": (AdapterNode, ProfinetDeviceNode),
    "utilization": None,
}

SWITCH_MODES = ("store-and-forward", "cut-through")
SWITCH_QOS = ("strict", "fifo")
MAC_TABLES = ("learning", "topology")
PATTERNS = ("constant", "poisson", "bursty", "batch")
MQTT_QOS = (0, 1)  # QoS 2 is not modelled
MODBUS_MAX_REGISTERS = 125  # Read Holding Registers limit

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", re.IGNORECASE)
_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def describe(element: object) -> str:
    """How errors refer to an element, e.g. ``flow tx -> rx``."""
    match element:
        case Flow(source=s, destination=d):
            return f"flow {s} -> {d}"
        case Publish(source=s, broker=b, topic=t):
            return f"publish {s} -> {b} topic {t!r}"
        case Route(broker=b, topic=t):
            return f"route {b} topic {t!r}"
        case Poll(source=s, slave=sl):
            return f"poll {s} -> {sl}"
        case ProfinetAR(controller=c, device=d):
            return f"PROFINET AR {c} -> {d}"
        case Link(a=a, b=b):
            return f"link {a} -- {b}"
        case Observation(metric=m, target=t):
            return f"observe {m} on {t}"
        case RunConfig():
            return "run"
    return f"{KIND_NAMES[element.kind]} {element.name!r}"  # type: ignore[attr-defined]


def _fail(element: object, problem: str) -> ScenarioError:
    return ScenarioError(f"{describe(element)}: {problem}")


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive(element: object, label: str, value: object) -> None:
    if not _number(value) or not value > 0:  # type: ignore[operator]
        raise _fail(element, f"{label} must be a positive number, got {value!r}")


def _non_negative(element: object, label: str, value: object) -> None:
    if not _number(value) or not value >= 0:  # type: ignore[operator]
        raise _fail(element, f"{label} must be zero or positive, got {value!r}")


def _in_range(element: object, label: str, value: object, lo: int, hi: int) -> None:
    if not _integer(value) or not lo <= value <= hi:  # type: ignore[operator]
        raise _fail(element, f"{label} {value!r} out of range {lo}-{hi}")


def _one_of(element: object, label: str, value: object, allowed: tuple[object, ...]) -> None:
    if value not in allowed:
        expected = ", ".join(repr(a) for a in allowed)
        raise _fail(element, f"invalid {label} {value!r}, expected one of {expected}")


# --- single elements ---


def check_node(node: NodeSpec) -> None:
    if not isinstance(node.name, str) or not node.name:
        raise ScenarioError(f"node name must be a non-empty string, got {node.name!r}")
    if isinstance(node, ENDPOINTS):
        m = _IPV4.match(node.ip) if isinstance(node.ip, str) else None
        if m is None or any(int(o) > 255 for o in m.groups()):
            raise _fail(node, f"{node.ip!r} is not an IPv4 address")
        if not isinstance(node.mac, str) or not _MAC.match(node.mac):
            raise _fail(
                node,
                f"MAC address {node.mac!r} must be six hex bytes, e.g. 02:00:0a:00:00:01",
            )
    if isinstance(node, BRIDGES):
        if not _integer(node.ports) or node.ports < 1:
            raise _fail(node, f"ports must be a positive integer, got {node.ports!r}")
    if isinstance(node, SwitchNode):
        _one_of(node, "mode", node.mode, SWITCH_MODES)
        _one_of(node, "qos", node.qos, SWITCH_QOS)
        _one_of(node, "mac_table", node.mac_table, MAC_TABLES)
    if isinstance(node, ModbusSlaveNode):
        _in_range(node, "unit_id", node.unit_id, 0, 255)


def check_link(link: Link) -> None:
    if link.a == link.b:
        raise _fail(link, "a node cannot be linked to itself")
    _positive(link, "speed", link.speed_mbps)
    _non_negative(link, "length", link.length_m)
    if not isinstance(link.duplex, bool):
        raise _fail(link, f"duplex must be true or false, got {link.duplex!r}")


def check_traffic(t: TrafficSpec, nodes: Mapping[str, NodeSpec] | None = None) -> None:
    """Rules of one traffic statement. With *nodes*, also the type of every
    node it involves (each must be present in *nodes*)."""
    if nodes is not None:
        for end, kinds in _ends(t):
            _lookup(nodes, end, t, kinds)
    if isinstance(t, Flow):
        _positive(t, "rate", t.rate)
        if t.size != "imix" and (not _integer(t.size) or t.size < 1):
            raise _fail(t, f"size must be a positive number of bytes or 'imix', got {t.size!r}")
        _one_of(t, "pattern", t.pattern, PATTERNS)
        _in_range(t, "priority", t.priority, 0, 7)
        if t.pattern == "batch":
            if t.batch is None:
                raise _fail(t, "pattern 'batch' needs a batch size")
            _check_batch(t)
        elif t.batch is not None:
            raise _fail(t, "batch is only meaningful with pattern 'batch'")
    elif isinstance(t, Publish):
        if not isinstance(t.topic, str) or not t.topic:
            raise _fail(t, "topic must be a non-empty string")
        _positive(t, "rate", t.rate)
        if not _integer(t.payload) or t.payload < 0:
            raise _fail(t, f"payload must be zero or more bytes, got {t.payload!r}")
        _one_of(t, "qos", t.qos, MQTT_QOS)
    elif isinstance(t, Route):
        if not isinstance(t.topic, str) or not t.topic:
            raise _fail(t, "topic must be a non-empty string")
        twice = sorted(s for s, n in Counter(t.subscribers).items() if n > 1)
        if twice:
            raise _fail(t, f"subscriber {twice[0]!r} listed twice")
    elif isinstance(t, Poll):
        _in_range(t, "register", t.register, 0, 65_535)
        _in_range(t, "count", t.count, 1, MODBUS_MAX_REGISTERS)
        _positive(t, "rate", t.rate)
    elif isinstance(t, ProfinetAR):
        if t.cycle_ms not in PN_CYCLES_MS or not _integer(t.cycle_ms):
            raise _fail(t, f"cycle {t.cycle_ms!r} ms must be one of {list(PN_CYCLES_MS)} ms")
        _in_range(t, "inputs", t.inputs, 1, PN_MAX_IO_BYTES)
        _in_range(t, "outputs", t.outputs, 1, PN_MAX_IO_BYTES)
        _in_range(t, "hold", t.hold, 1, 255)
    _non_negative(t, "delay", getattr(t, "delay_ms", 0.0))


def _check_batch(t: Flow) -> None:
    b = t.batch
    assert b is not None
    if b.kind == "fixed" and (b.param < 1 or b.param != int(b.param)):
        raise _fail(t, f"batch size must be an integer >= 1, got {b.param:g}")
    if b.kind == "geom" and b.param < 1:
        raise _fail(t, f"geometric batch mean must be >= 1, got {b.param:g}")
    if b.kind == "pareto" and b.param <= 1:
        raise _fail(t, f"pareto batch shape must be > 1 (finite mean), got {b.param:g}")


def check_observation(o: Observation, target: NodeSpec | None = None) -> None:
    """Rules of one observation; with *target*, also its node type."""
    if o.metric not in METRIC_TARGETS:
        raise _fail(o, f"unknown or unsupported metric {o.metric!r}")
    _positive(o, "every", o.every_ms)
    if target is not None:
        _lookup({target.name: target}, o.target, o, METRIC_TARGETS[o.metric])


def check_run(run: RunConfig) -> None:
    _positive(run, "duration", run.duration_ms)
    if not _integer(run.seed):
        raise _fail(run, f"seed must be an integer, got {run.seed!r}")


# --- the whole scenario ---


def validate(scenario: Scenario) -> None:
    """Raise :class:`ScenarioError` for the first rule *scenario* breaks."""
    nodes: dict[str, NodeSpec] = {}
    owner_of_mac: dict[str, str] = {}
    for i, node in enumerate(scenario.nodes):
        with _at("nodes", i):
            check_node(node)
            if node.name in nodes:
                raise ScenarioError(f"node {node.name!r} is declared twice")
            nodes[node.name] = node
            if isinstance(node, ENDPOINTS):
                mac = node.mac.lower()
                if mac in owner_of_mac:
                    raise _fail(node, f"MAC address {mac} is already used by {owner_of_mac[mac]!r}")
                owner_of_mac[mac] = node.name

    degree: Counter[str] = Counter()
    component = _Components(nodes)
    for i, link in enumerate(scenario.links):
        with _at("links", i):
            check_link(link)
            for end in (link.a, link.b):
                _lookup(nodes, end, link)
            for end in (link.a, link.b):
                degree[end] += 1
                node = nodes[end]
                if isinstance(node, ENDPOINTS) and degree[end] > 1:
                    raise _fail(link, f"{end!r} is an end station and already has a link")
                if isinstance(node, BRIDGES) and degree[end] > node.ports:
                    raise _fail(link, f"{end!r} has only {node.ports} ports, all in use")
            if not component.union(link.a, link.b):
                raise _fail(
                    link,
                    "closes a loop in the topology; loops are not supported "
                    "(no spanning tree is modelled)",
                )

    ars: set[tuple[str, str]] = set()
    routes: set[tuple[str, str]] = set()
    for i, t in enumerate(scenario.traffic):
        with _at("traffic", i):
            check_traffic(t, nodes)
            for end, _ in _ends(t):
                if degree[end] == 0:
                    raise _fail(t, f"{end!r} has no link, so it cannot exchange frames")
            ends = [end for end, _ in _ends(t)]
            if len(ends) == 2 and ends[0] == ends[1]:
                raise _fail(t, "source and destination are the same node")
            for end in ends[1:]:
                if not component.same(ends[0], end):
                    raise _fail(t, f"{ends[0]!r} and {end!r} are not connected")
            if isinstance(t, ProfinetAR):
                if (t.controller, t.device) in ars:
                    raise _fail(t, f"{t.controller!r} already controls {t.device!r}")
                ars.add((t.controller, t.device))
            if isinstance(t, Route):
                if (t.broker, t.topic) in routes:
                    raise _fail(t, "topic is routed twice")
                routes.add((t.broker, t.topic))

    observed: set[tuple[str, str]] = set()
    for i, o in enumerate(scenario.observations):
        with _at("observations", i):
            check_observation(o, _lookup(nodes, o.target, o))
            if degree[o.target] == 0:
                raise _fail(o, f"{o.target!r} has no link, so there is nothing to observe")
            if (o.metric, o.target) in observed:
                raise _fail(o, "observed twice")
            observed.add((o.metric, o.target))

    if scenario.run is not None:
        with _at("run", 0):
            check_run(scenario.run)


def _ends(t: TrafficSpec) -> list[tuple[str, tuple[type, ...]]]:
    """The nodes a traffic statement involves, each with its allowed types."""
    if isinstance(t, Flow):
        return [(t.source, (AdapterNode,)), (t.destination, (AdapterNode,))]
    if isinstance(t, Publish):
        return [(t.source, (AdapterNode,)), (t.broker, (MqttBrokerNode,))]
    if isinstance(t, Route):
        return [(t.broker, (MqttBrokerNode,))] + [(s, (AdapterNode,)) for s in t.subscribers]
    if isinstance(t, Poll):
        return [(t.source, (AdapterNode,)), (t.slave, (ModbusSlaveNode,))]
    return [(t.controller, (AdapterNode,)), (t.device, (ProfinetDeviceNode,))]


def _lookup(
    nodes: Mapping[str, NodeSpec], name: str, element: object, kinds: tuple[type, ...] | None = None
) -> NodeSpec:
    node = nodes.get(name)
    if node is None:
        raise _fail(element, f"reference to undeclared node {name!r}")
    if kinds is not None and not isinstance(node, kinds):
        expected = " or ".join(KIND_NAMES[k.kind] for k in kinds)  # type: ignore[attr-defined]
        raise _fail(element, f"{name!r} is {_a(KIND_NAMES[node.kind])}, expected {expected}")
    return node


def _a(noun: str) -> str:
    return f"an {noun}" if noun[0] in "AEIOU" else f"a {noun}"


class _at:
    """Attach the element's position to a ScenarioError raised inside."""

    def __init__(self, section: Section, index: int) -> None:
        self.where: tuple[Section, int] = (section, index)

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        if isinstance(exc, ScenarioError) and exc.where is None:
            exc.where = self.where


class _Components:
    """Union-find over node names: connectivity and loop detection."""

    def __init__(self, nodes: dict[str, NodeSpec]) -> None:
        self.parent = {name: name for name in nodes}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> bool:
        """Join a and b; False if they were already connected (a loop)."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[ra] = rb
        return True

    def same(self, a: str, b: str) -> bool:
        return self.find(a) == self.find(b)


__all__ = [
    "KIND_NAMES",
    "METRIC_TARGETS",
    "ScenarioError",
    "check_link",
    "check_node",
    "check_observation",
    "check_run",
    "check_traffic",
    "describe",
    "validate",
]
