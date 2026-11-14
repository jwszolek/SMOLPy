"""Execution-independent Scenario IR (ADR-0007).

A :class:`Scenario` is a complete, declarative description of one simulation:
nodes, links, traffic, observations and the run configuration. It is a plain
tree of frozen dataclasses: nodes are referenced by name, never by object, so
it has no cycles and serialises directly to versioned JSON.

Values keep the units users give: milliseconds, metres, Mb/s, bytes, frames
or messages per second. How they map to simulated time is defined by the
model semantics, not by any engine. Defaults are resolved when the scenario
is built (e.g. a MAC address derived from the IP is stored explicitly), but
quantities that follow from the model, such as wire frame sizes, are not
stored: every engine computes them from the same formulas.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from smolpy.core.batch import BatchSpec

FORMAT = "smol-ir"
VERSION = 1

SwitchMode = Literal["store-and-forward", "cut-through"]
SwitchQoS = Literal["strict", "fifo"]
MacTableMode = Literal["learning", "topology"]
TrafficPattern = Literal["constant", "poisson", "bursty", "batch"]
FrameSize = int | Literal["imix"]


# --- nodes ---


@dataclass(frozen=True)
class AdapterNode:
    """End station (NIC) that sources and sinks traffic."""

    kind: ClassVar[str] = "adapter"
    name: str
    ip: str
    mac: str


@dataclass(frozen=True)
class SwitchNode:
    """Layer-2 switch: per-port egress queues and a MAC table."""

    kind: ClassVar[str] = "switch"
    name: str
    ports: int
    mode: SwitchMode = "store-and-forward"
    qos: SwitchQoS = "strict"
    mac_table: MacTableMode = "learning"


@dataclass(frozen=True)
class MqttBrokerNode:
    kind: ClassVar[str] = "mqtt_broker"
    name: str
    ip: str
    mac: str


@dataclass(frozen=True)
class ModbusSlaveNode:
    kind: ClassVar[str] = "modbus_slave"
    name: str
    ip: str
    mac: str
    unit_id: int


@dataclass(frozen=True)
class ProfinetDeviceNode:
    kind: ClassVar[str] = "profinet_device"
    name: str
    ip: str
    mac: str


NodeSpec = AdapterNode | SwitchNode | MqttBrokerNode | ModbusSlaveNode | ProfinetDeviceNode


# --- links ---


@dataclass(frozen=True)
class Link:
    """Cable between nodes *a* and *b*. Port numbers follow link order."""

    a: str
    b: str
    speed_mbps: float
    length_m: float
    duplex: bool = True


# --- traffic ---


@dataclass(frozen=True)
class Flow:
    """Raw Ethernet frames from *source* to *destination* (``Adapter.sends``).

    *rate* is the long-run average in frames/s, *size* bytes or ``"imix"``;
    *batch* is set only for ``pattern="batch"``.
    """

    kind: ClassVar[str] = "flow"
    source: str
    destination: str
    rate: float
    size: FrameSize = 512
    pattern: TrafficPattern = "constant"
    delay_ms: float = 0.0
    batch: BatchSpec | None = None
    priority: int = 0


@dataclass(frozen=True)
class Publish:
    """MQTT PUBLISH messages to *broker* (``Adapter.publishes``); *rate* in msg/s."""

    kind: ClassVar[str] = "publish"
    source: str
    broker: str
    topic: str
    rate: float = 1.0
    payload: int = 20
    qos: int = 0
    delay_ms: float = 0.0


@dataclass(frozen=True)
class Route:
    """Broker fan-out of *topic* to *subscribers* (``MQTTBroker.routes``)."""

    kind: ClassVar[str] = "route"
    broker: str
    topic: str
    subscribers: tuple[str, ...]


@dataclass(frozen=True)
class Poll:
    """Modbus TCP register reads of *slave* (``Adapter.polls``); *rate* in polls/s."""

    kind: ClassVar[str] = "poll"
    source: str
    slave: str
    register: int
    count: int
    rate: float = 1.0
    delay_ms: float = 0.0


@dataclass(frozen=True)
class ProfinetAR:
    """PROFINET RT application relationship (``Adapter.controls``).

    *inputs* flow device -> controller, *outputs* controller -> device, both in
    bytes per frame; the watchdog is ``hold`` x ``cycle_ms``.
    """

    kind: ClassVar[str] = "profinet_ar"
    controller: str
    device: str
    cycle_ms: int
    inputs: int
    outputs: int
    hold: int = 3
    delay_ms: float = 0.0


TrafficSpec = Flow | Publish | Route | Poll | ProfinetAR


def traffic_owner(t: TrafficSpec) -> str:
    """The node that runs a traffic statement: the source, the PROFINET
    controller, or the broker of a route."""
    if isinstance(t, ProfinetAR):
        return t.controller
    if isinstance(t, Route):
        return t.broker
    return t.source


# --- observations and run ---


@dataclass(frozen=True)
class Observation:
    metric: str
    target: str
    every_ms: float


@dataclass(frozen=True)
class RunConfig:
    duration_ms: float
    seed: int = 42


@dataclass(frozen=True)
class Scenario:
    """A complete simulation scenario.

    Order is significant: *links* define port numbering, and *traffic* lists
    every source's statements in the order the source declared them.
    *run* is ``None`` for a scenario that only describes a network.
    """

    name: str
    nodes: tuple[NodeSpec, ...] = ()
    links: tuple[Link, ...] = ()
    traffic: tuple[TrafficSpec, ...] = ()
    observations: tuple[Observation, ...] = ()
    run: RunConfig | None = None

    def node(self, name: str) -> NodeSpec:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(name)

    # --- serialisation ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "name": self.name,
            "nodes": [_encode(n) for n in self.nodes],
            "links": [_encode(lk) for lk in self.links],
            "traffic": [_encode(t) for t in self.traffic],
            "observations": [_encode(o) for o in self.observations],
            "run": None if self.run is None else _encode(self.run),
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scenario:
        if not isinstance(data, dict):
            raise ValueError("scenario must be a JSON object")
        if data.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} document (format={data.get('format')!r})")
        if data.get("version") != VERSION:
            raise ValueError(
                f"unsupported {FORMAT} version {data.get('version')!r}, expected {VERSION}"
            )
        body = {k: v for k, v in data.items() if k not in ("format", "version")}
        _check_keys("scenario", body, {f.name for f in dataclasses.fields(cls)})
        if "name" not in body:
            raise ValueError("scenario: missing field 'name'")
        run = body.get("run")
        return cls(
            name=body["name"],
            nodes=tuple(_decode_kind(n, _NODE_KINDS, "node") for n in body.get("nodes", [])),
            links=tuple(_decode(Link, lk, "link") for lk in body.get("links", [])),
            traffic=tuple(
                _decode_kind(t, _TRAFFIC_KINDS, "traffic") for t in body.get("traffic", [])
            ),
            observations=tuple(
                _decode(Observation, o, "observation") for o in body.get("observations", [])
            ),
            run=None if run is None else _decode(RunConfig, run, "run"),
        )

    @classmethod
    def from_json(cls, text: str) -> Scenario:
        return cls.from_dict(json.loads(text))


_NODE_KINDS: dict[str, type] = {
    c.kind: c
    for c in (
        AdapterNode,
        SwitchNode,
        MqttBrokerNode,
        ModbusSlaveNode,
        ProfinetDeviceNode,
    )
}
_TRAFFIC_KINDS: dict[str, type] = {c.kind: c for c in (Flow, Publish, Route, Poll, ProfinetAR)}


def _encode(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    kind = getattr(type(obj), "kind", None)
    if kind is not None:
        out["kind"] = kind
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if isinstance(value, BatchSpec):
            value = {"kind": value.kind, "param": value.param}
        elif isinstance(value, tuple):
            value = list(value)
        out[f.name] = value
    return out


def _decode_kind(data: Any, kinds: dict[str, type], what: str) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"{what}: expected an object, got {data!r}")
    kind = data.get("kind")
    cls = kinds.get(kind)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"{what}: unknown kind {kind!r}, expected one of {sorted(kinds)}")
    return _decode(cls, {k: v for k, v in data.items() if k != "kind"}, f"{what} {kind!r}")


def _decode(cls: type, data: Any, what: str) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"{what}: expected an object, got {data!r}")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    _check_keys(what, data, set(fields))
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        if name == "batch" and value is not None:
            value = _decode_batch(value, what)
        elif isinstance(value, list):
            value = tuple(value)
        kwargs[name] = value
    missing = [
        n
        for n, f in fields.items()
        if n not in kwargs
        and f.default is dataclasses.MISSING
        and f.default_factory is dataclasses.MISSING
    ]
    if missing:
        raise ValueError(f"{what}: missing field(s) {', '.join(missing)}")
    return cls(**kwargs)


def _decode_batch(value: Any, what: str) -> BatchSpec:
    if not isinstance(value, dict) or set(value) != {"kind", "param"}:
        raise ValueError(f"{what}: batch must be an object with 'kind' and 'param'")
    if value["kind"] not in ("fixed", "geom", "pareto"):
        raise ValueError(f"{what}: unknown batch kind {value['kind']!r}")
    return BatchSpec(value["kind"], float(value["param"]))


def _check_keys(what: str, data: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{what}: unknown field(s) {', '.join(unknown)}")
