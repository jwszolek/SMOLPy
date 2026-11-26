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

import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import Literal

from smolpy.model import timebase
from smolpy.model.frames import (
    IMIX_SIZES,
    MAX_STANDARD_FRAME_BYTES,
    MIN_FRAME_BYTES,
    PN_CYCLES_MS,
    PN_MAX_IO_BYTES,
    flow_frame_size,
    frame_cells,
    modbus_response_frame_size,
    mqtt_publish_frame_size,
    pn_frame_size,
)
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
    "bytes_sent": ENDPOINTS,  # any end station since ADR-0013
    "bytes_received": (AdapterNode,),
    # Modbus metrics on a master, or on a slave over its polls (ADR-0013)
    "modbus_latency": (AdapterNode, ModbusSlaveNode),
    "queue_depth": (SwitchNode,),
    "broker_queue": (MqttBrokerNode,),
    "pn_latency": (AdapterNode, ProfinetDeviceNode),
    "pn_jitter": (AdapterNode, ProfinetDeviceNode),
    "pn_watchdog_expiries": (AdapterNode, ProfinetDeviceNode),
    "utilization": None,
    # ADR-0011: loss
    "drops": (SwitchNode,),
    "drops_total": (SwitchNode,),
    "frames_sent": ENDPOINTS,
    "frames_received": (AdapterNode,),
    "modbus_unanswered": (AdapterNode, ModbusSlaveNode),
    # ADR-0012: one Modbus request in flight
    "modbus_timeouts": (AdapterNode, ModbusSlaveNode),
    "modbus_skipped": (AdapterNode, ModbusSlaveNode),
    # ADR-0013: drops by account
    "ingress_drops_total": (SwitchNode,),
    "egress_drops_total": (SwitchNode,),
    # ADR-0014: drop episodes and the time in the drop state
    "ingress_drop_episodes_total": (SwitchNode,),
    "egress_drop_episodes_total": (SwitchNode,),
    "ingress_drop_time_total": (SwitchNode,),
    "egress_drop_time_total": (SwitchNode,),
    # ADR-0015: the shared pool
    "pool_occupancy": (SwitchNode,),
}

# Metrics that a station other than an adapter carries only since ADR-0013,
# by the capability feature an engine needs for them there.
STATION_METRIC_FEATURES = {
    "frames_sent": "station_flows",
    "bytes_sent": "station_flows",
    "modbus_latency": "slave_metrics",
    "modbus_unanswered": "slave_metrics",
    "modbus_timeouts": "slave_metrics",
    "modbus_skipped": "slave_metrics",
}

SWITCH_MODES = ("store-and-forward", "cut-through")
SWITCH_QOS = ("strict", "fifo")
MAC_TABLES = ("learning", "topology")
DROP_SCOPES = ("port", "switch")
DRAIN_COUNTS = ("freed", "net")
POOL_COUNTS = ("excess", "total")
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
        _check_switch_buffers(node)
    if isinstance(node, ModbusSlaveNode):
        _in_range(node, "unit_id", node.unit_id, 0, 255)


def _check_switch_buffers(node: SwitchNode) -> None:
    """A switch's limits, cell size and resume levels (ADR-0011, ADR-0013,
    ADR-0014)."""
    scopes = (
        ("buffer", "resume", node.buffer_bytes, node.buffer_frames, node.buffer_cells,
         node.buffer_resume),
        ("ingress_buffer", "ingress_resume", node.ingress_buffer_bytes,
         node.ingress_buffer_frames, node.ingress_buffer_cells, node.ingress_buffer_resume),
    )  # fmt: skip
    for label, resume_label, in_bytes, in_frames, in_cells, resume in scopes:
        _check_limit(node, label, in_bytes, in_frames, in_cells)
        limit = next((v for v in (in_bytes, in_frames, in_cells) if v is not None), None)
        if resume is None:
            continue
        if limit is None:
            raise _fail(node, f"{resume_label} needs {label}: it is a level of that limit")
        if not _integer(resume) or not 0 <= resume < limit:
            unit = (
                "bytes" if in_bytes is not None else "frames" if in_frames is not None else "cells"
            )
            raise _fail(
                node,
                f"{resume_label} must be a whole number of {unit} from 0 to less than"
                f" {label} ({limit}), got {resume!r}",
            )
    _check_pool(node)
    in_cells = (
        node.buffer_cells is not None
        or node.ingress_buffer_cells is not None
        or node.pool_cells is not None
    )
    cell = node.cell_bytes
    if cell is not None and (not _integer(cell) or cell < 1):
        raise _fail(node, f"cell must be a whole number of bytes, at least 1, got {cell!r}")
    if in_cells and cell is None:
        raise _fail(node, "a limit in cells needs cell, the cell size in bytes")
    if cell is not None and not in_cells:
        raise _fail(node, "cell is the size of the cells of a limit in cells; no limit is in cells")


def _check_pool(node: SwitchNode) -> None:
    """A switch's pool, reserve and drain (ADR-0015)."""
    pools = [v for v in (node.pool_bytes, node.pool_frames, node.pool_cells) if v is not None]
    if len(pools) > 1:
        raise _fail(node, "pool is given in more than one unit (bytes, frames, cells); give one")
    for unit, value in (("bytes", node.pool_bytes), ("frames", node.pool_frames),
                        ("cells", node.pool_cells)):  # fmt: skip
        if value is not None and (not _integer(value) or value < 1):
            raise _fail(node, f"pool in {unit} must be a whole number of at least 1, got {value!r}")
    pool, limit = node.pool_limit, node.ingress_limit
    if pool is not None and limit is not None and pool[1] != limit[1]:
        raise _fail(
            node,
            f"pool is in {pool[1]} and ingress_buffer in {limit[1]}: the ingress scope has"
            " one unit",
        )
    scope_unit = node.ingress_unit
    for label, value in (("ingress_reserve", node.ingress_reserve),
                         ("ingress_drain", node.ingress_drain)):  # fmt: skip
        if value is not None and (not _integer(value) or value < 1):
            raise _fail(
                node,
                f"{label} must be a whole number of {scope_unit or 'units'}, at least 1,"
                f" got {value!r}",
            )
    if node.ingress_reserve is not None:
        if pool is None:
            raise _fail(node, "ingress_reserve needs pool: it is room beside the shared pool")
        if limit is not None and node.ingress_reserve > limit[0]:
            raise _fail(
                node,
                f"ingress_reserve ({node.ingress_reserve}) is more than ingress_buffer"
                f" ({limit[0]}), which caps what a port holds",
            )
    if node.ingress_drain is not None:
        if pool is None and limit is None:
            raise _fail(node, "ingress_drain needs pool or ingress_buffer: a refusal to end")
        if node.ingress_buffer_resume is not None:
            raise _fail(
                node, "ingress_drain and ingress_resume both end a drop state; give one of them"
            )
    if node.ingress_drop is not None:
        _one_of(node, "ingress_drop", node.ingress_drop, DROP_SCOPES)
        if node.ingress_drop == "switch" and node.ingress_drain is None:
            raise _fail(
                node,
                "ingress_drop=switch needs ingress_drain: a switch-wide drop state ends"
                " once the switch has freed that much",
            )
    if node.ingress_drain_count is not None:
        _one_of(node, "ingress_drain_count", node.ingress_drain_count, DRAIN_COUNTS)
        if node.ingress_drop != "switch":
            raise _fail(
                node,
                "ingress_drain_count needs ingress_drop=switch: it counts the drain of the"
                " switch's drop state",
            )
    _check_light_port(node)


def _check_light_port(node: SwitchNode) -> None:
    """The protection of a light port (ADR-0017 Part 2): the pool's count,
    the dynamic threshold and the mark, each with the switch's drop state
    and its drain counted net."""
    given = [
        label
        for label, value in (
            ("ingress_pool_count", node.ingress_pool_count),
            ("ingress_alpha", node.ingress_alpha),
            ("ingress_mark", node.ingress_mark),
        )  # fmt: skip
        if value is not None
    ]
    if not given:
        return
    if node.ingress_pool_count is not None:
        _one_of(node, "ingress_pool_count", node.ingress_pool_count, POOL_COUNTS)
    alpha = node.ingress_alpha
    if alpha is not None and (not _number(alpha) or not math.isfinite(alpha) or not alpha > 0):
        raise _fail(node, f"ingress_alpha must be a positive number, got {alpha!r}")
    mark = node.ingress_mark
    if mark is not None and (not _integer(mark) or mark < 1):
        raise _fail(
            node,
            f"ingress_mark must be a whole number of {node.ingress_unit or 'units'}, at least 1,"
            f" got {mark!r}",
        )
    if node.ingress_drop != "switch" or node.ingress_drain_count != "net":
        raise _fail(
            node,
            f"{given[0]} needs ingress_drop=switch and ingress_drain_count=net: it protects a"
            " light port in the switch's drop state",
        )
    for label in ("ingress_pool_count", "ingress_alpha"):
        if label in given and node.pool_limit is None:
            raise _fail(node, f"{label} needs pool: it is about the shared pool's use")


def _check_limit(
    node: SwitchNode, label: str, in_bytes: object, in_frames: object, in_cells: object = None
) -> None:
    """A switch's limit of one account: in bytes, frames or cells, one of
    them (ADR-0011, ADR-0013, ADR-0014)."""
    if sum(v is not None for v in (in_bytes, in_frames, in_cells)) > 1:
        raise _fail(
            node, f"{label} is given in more than one unit (bytes, frames, cells); give one"
        )
    if in_bytes is not None and (not _integer(in_bytes) or in_bytes < MAX_STANDARD_FRAME_BYTES):  # type: ignore[operator]
        raise _fail(
            node,
            f"{label} must be a whole number of bytes, at least one maximum frame"
            f" ({MAX_STANDARD_FRAME_BYTES} B), got {in_bytes!r}",
        )
    for unit, value in (("frames", in_frames), ("cells", in_cells)):
        if value is not None and (not _integer(value) or value < 1):  # type: ignore[operator]
            raise _fail(
                node, f"{label} in {unit} must be a whole number of at least 1, got {value!r}"
            )


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
        if t.size != "imix" and (not _integer(t.size) or t.size < MIN_FRAME_BYTES):
            raise _fail(
                t,
                f"size must be a frame of at least {MIN_FRAME_BYTES} bytes (FCS included)"
                f" or 'imix', got {t.size!r}",
            )
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
        _check_outstanding(t)
    elif isinstance(t, ProfinetAR):
        if t.cycle_ms not in PN_CYCLES_MS or not _integer(t.cycle_ms):
            raise _fail(t, f"cycle {t.cycle_ms!r} ms must be one of {list(PN_CYCLES_MS)} ms")
        _in_range(t, "inputs", t.inputs, 1, PN_MAX_IO_BYTES)
        _in_range(t, "outputs", t.outputs, 1, PN_MAX_IO_BYTES)
        _in_range(t, "hold", t.hold, 1, 255)
    _non_negative(t, "delay", getattr(t, "delay_ms", 0.0))


def _check_outstanding(t: Poll) -> None:
    """ADR-0012: *outstanding* is 1 or unset; a timeout needs it."""
    if t.outstanding is not None and (not _integer(t.outstanding) or t.outstanding != 1):
        raise _fail(t, f"outstanding must be 1 or unlimited, got {t.outstanding!r}")
    if t.timeout_ms is None:
        return
    if t.outstanding is None:
        raise _fail(t, "timeout needs outstanding=1")
    _positive(t, "timeout", t.timeout_ms)
    if not math.isfinite(t.timeout_ms) or timebase.ms_to_ns(t.timeout_ms) < 1:
        raise _fail(
            t, f"timeout must be at least 1 ns (0.000001 ms) and finite, got {t.timeout_ms!r}"
        )
    if timebase.ms_to_ns(t.timeout_ms) > timebase.MAX_NS:
        raise _fail(t, f"timeout must be at most 2^63 - 1 ns (292 years), got {t.timeout_ms!r} ms")


def _largest_frame(t: TrafficSpec) -> int:
    """The largest frame *t* sends, in bytes (Model Semantics §2)."""
    if isinstance(t, Flow):
        size = max(s for s, _ in IMIX_SIZES) if t.size == "imix" else t.size
        return flow_frame_size(size, t.priority)
    if isinstance(t, Publish):
        return mqtt_publish_frame_size(t.topic, t.payload, t.qos)
    if isinstance(t, Poll):
        return modbus_response_frame_size(t.count)
    if isinstance(t, ProfinetAR):
        return pn_frame_size(max(t.inputs, t.outputs))
    return 0  # a route forwards PUBLISH frames, sized by their publish


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
    if not math.isfinite(o.every_ms) or timebase.period_ns(o.every_ms) < 1:
        raise _fail(o, f"every must be at least 1 ns (0.000001 ms) and finite, got {o.every_ms!r}")
    if target is not None:
        _lookup({target.name: target}, o.target, o, METRIC_TARGETS[o.metric])


def check_run(run: RunConfig) -> None:
    _positive(run, "duration", run.duration_ms)
    if not math.isfinite(run.duration_ms) or timebase.ms_to_ns(run.duration_ms) > timebase.MAX_NS:
        raise _fail(
            run, f"duration must be at most 2^63 - 1 ns (292 years), got {run.duration_ms!r} ms"
        )
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
    polled = Counter((t.source, t.slave) for t in scenario.traffic if isinstance(t, Poll))
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
            if isinstance(t, Poll) and t.outstanding is not None and polled[t.source, t.slave] > 1:
                raise _fail(
                    t,
                    f"{t.source!r} polls {t.slave!r} more than once; with outstanding=1"
                    " a master-slave pair has one poll",
                )
            if isinstance(t, Route):
                if (t.broker, t.topic) in routes:
                    raise _fail(t, "topic is routed twice")
                routes.add((t.broker, t.topic))

    largest = max((_largest_frame(t) for t in scenario.traffic), default=0)
    for i, node in enumerate(scenario.nodes):
        if not isinstance(node, SwitchNode):
            continue
        for label, in_bytes in (
            ("buffer", node.buffer_bytes),
            ("ingress_buffer", node.ingress_buffer_bytes),
        ):
            if in_bytes is not None and in_bytes < largest:
                raise ScenarioError(
                    f"{describe(node)}: {label} {in_bytes} B is smaller than the"
                    f" largest frame of the scenario, {largest} B, which it would always drop",
                    ("nodes", i),
                )
        pool = node.pool_limit
        if pool is not None and largest:
            room = pool[0] + (node.ingress_reserve or 0)
            need = (
                largest
                if pool[1] == "bytes"
                else 1
                if pool[1] == "frames"
                else frame_cells(largest, node.cell_bytes or 1)
            )
            if room < need:
                raise ScenarioError(
                    f"{describe(node)}: pool and ingress_reserve hold {room} {pool[1]}, less than"
                    f" the largest frame of the scenario ({largest} B, {need} {pool[1]}),"
                    " which they would always drop",
                    ("nodes", i),
                )
        for label, in_cells in (
            ("buffer", node.buffer_cells),
            ("ingress_buffer", node.ingress_buffer_cells),
        ):
            if in_cells is None or node.cell_bytes is None or largest == 0:
                continue
            need = frame_cells(largest, node.cell_bytes)
            if in_cells < need:
                raise ScenarioError(
                    f"{describe(node)}: {label} {in_cells} cells is smaller than the largest"
                    f" frame of the scenario, {largest} B in {need} cells of"
                    f" {node.cell_bytes} B, which it would always drop",
                    ("nodes", i),
                )

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
    if isinstance(t, Flow):  # between any two end stations (ADR-0013)
        return [(t.source, ENDPOINTS), (t.destination, ENDPOINTS)]
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
        expected = (
            "an end station"
            if kinds == ENDPOINTS
            else " or ".join(KIND_NAMES[k.kind] for k in kinds)  # type: ignore[attr-defined]
        )
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
    "STATION_METRIC_FEATURES",
    "ScenarioError",
    "check_link",
    "check_node",
    "check_observation",
    "check_run",
    "check_traffic",
    "describe",
    "validate",
]
