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
    | LinkDecl
    | FlowDecl
    | PublishDecl
    | RouteDecl
    | PollDecl
    | ObserveDecl
    | SimulateDecl
)


@dataclass
class NetworkDecl:
    name: str
    declarations: list[Decl]
    line: int
