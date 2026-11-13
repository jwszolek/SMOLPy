from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdapterDecl:
    name: str
    ip: str
    mac: str | None
    line: int


@dataclass
class SwitchDecl:
    name: str
    ports: int
    mode: str
    line: int
    qos: str = "strict"
    mac_table: str = "learning"


@dataclass
class HubDecl:
    name: str
    ports: int
    line: int


@dataclass
class MqttBrokerDecl:
    name: str
    ip: str
    line: int


@dataclass
class ModbusSlaveDecl:
    name: str
    ip: str
    unit_id: int
    line: int


@dataclass
class ProfinetDeviceDecl:
    name: str
    ip: str
    line: int


@dataclass
class LinkDecl:
    a: str
    b: str
    speed: float
    length: float
    line: int


@dataclass
class FlowDecl:
    src: str
    dst: str
    rate: float
    size: int | str
    pattern: str
    delay_ms: float
    line: int
    batch: str = "1"
    priority: int = 0


@dataclass
class PublishDecl:
    src: str
    broker: str
    topic: str
    rate: float
    payload: int
    qos: int
    delay_ms: float
    line: int


@dataclass
class RouteDecl:
    broker: str
    topic: str
    targets: list[str]
    line: int


@dataclass
class PollDecl:
    master: str
    slave: str
    register: int
    count: int
    rate: float
    delay_ms: float
    line: int


@dataclass
class ControlDecl:
    controller: str
    device: str
    cycle: int
    inputs: int
    outputs: int
    hold: int
    delay_ms: float
    line: int


@dataclass
class ObserveDecl:
    metric: str
    target: str
    every: float
    line: int


@dataclass
class SimulateDecl:
    duration: float
    mode: str
    line: int
    seed: int = 42


Decl = (
    AdapterDecl
    | SwitchDecl
    | HubDecl
    | MqttBrokerDecl
    | ModbusSlaveDecl
    | ProfinetDeviceDecl
    | LinkDecl
    | FlowDecl
    | PublishDecl
    | RouteDecl
    | PollDecl
    | ControlDecl
    | ObserveDecl
    | SimulateDecl
)


@dataclass
class NetworkDecl:
    name: str
    declarations: list[Decl]
    line: int
