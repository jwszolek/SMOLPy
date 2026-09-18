from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from smolpy.core.adapter import Adapter
from smolpy.core.network import Network
from smolpy.core.node import Node
from smolpy.ethernet.hub import Hub
from smolpy.ethernet.switch import Switch
from smolpy.lang import ast as smol_ast
from smolpy.lang.errors import SMOLSemanticError
from smolpy.lang.transformer import parse_smol
from smolpy.modbus.slave import ModbusSlave
from smolpy.mqtt.broker import MQTTBroker

if TYPE_CHECKING:
    from smolpy.core.network import SimulationResult

# Mirrors the observe() dispatch chain in sim/engine.py — the actual runtime
# behaviour, not just the MetricName type (which also lists "frame_loss",
# an unimplemented metric with no engine.py branch — deliberately absent here
# so .smol files can't observe it and expect it to do anything).
_METRIC_NODE_TYPES: dict[str, tuple[type[Node], ...] | None] = {
    "throughput": (Adapter,),
    "latency": (Adapter,),
    "bytes_sent": (Adapter,),
    "bytes_received": (Adapter,),
    "modbus_latency": (Adapter,),
    "queue_depth": (Switch,),
    "collision_rate": (Hub,),
    "broker_queue": (MQTTBroker,),
    "utilization": None,  # any node type
}

_SIMULATE_MODES = ("headless", "text", "live")


class _Interpreter:
    def __init__(self, file: str) -> None:
        self.file = file

    def _resolve(
        self,
        net: Network,
        name: str,
        line: int,
        *,
        expect: type[Node] | tuple[type[Node], ...] | None = None,
    ) -> Node:
        node = net._nodes.get(name)
        if node is None:
            raise SMOLSemanticError(self.file, line, f"reference to undeclared node '{name}'")
        if expect is not None and not isinstance(node, expect):
            names = (
                " or ".join(t.__name__ for t in expect)
                if isinstance(expect, tuple)
                else expect.__name__
            )
            raise SMOLSemanticError(
                self.file, line, f"'{name}' is a {type(node).__name__}, expected {names}"
            )
        return node

    def build_and_run(self, decl: smol_ast.NetworkDecl) -> SimulationResult:
        net = Network(decl.name)
        simulate_decl: smol_ast.SimulateDecl | None = None

        for d in decl.declarations:
            try:
                if isinstance(d, smol_ast.AdapterDecl):
                    net.adapter(d.name, ip=d.ip, mac=d.mac)
                elif isinstance(d, smol_ast.SwitchDecl):
                    net.switch(d.name, ports=d.ports, mode=d.mode)  # type: ignore[arg-type]
                elif isinstance(d, smol_ast.HubDecl):
                    net.hub(d.name, ports=d.ports)
                elif isinstance(d, smol_ast.MqttBrokerDecl):
                    net.mqtt_broker(d.name, ip=d.ip)
                elif isinstance(d, smol_ast.ModbusSlaveDecl):
                    net.modbus_slave(d.name, ip=d.ip, unit_id=d.unit_id)
                elif isinstance(d, smol_ast.LinkDecl):
                    a = self._resolve(net, d.a, d.line)
                    b = self._resolve(net, d.b, d.line)
                    net.link(a, b, speed=d.speed, length=d.length)
                elif isinstance(d, smol_ast.FlowDecl):
                    src = self._resolve(net, d.src, d.line, expect=Adapter)
                    dst = self._resolve(net, d.dst, d.line, expect=Adapter)
                    assert isinstance(src, Adapter)
                    assert isinstance(dst, Adapter)
                    src.sends(
                        to=dst,
                        rate=d.rate,
                        size=d.size,  # type: ignore[arg-type]
                        pattern=d.pattern,  # type: ignore[arg-type]
                        delay_ms=d.delay_ms,
                    )
                elif isinstance(d, smol_ast.PublishDecl):
                    src = self._resolve(net, d.src, d.line, expect=Adapter)
                    broker = self._resolve(net, d.broker, d.line, expect=MQTTBroker)
                    assert isinstance(src, Adapter)
                    src.publishes(
                        to=broker,
                        topic=d.topic,
                        rate=d.rate,
                        payload=d.payload,
                        qos=d.qos,
                        delay_ms=d.delay_ms,
                    )
                elif isinstance(d, smol_ast.RouteDecl):
                    broker = self._resolve(net, d.broker, d.line, expect=MQTTBroker)
                    targets = [self._resolve(net, t, d.line, expect=Adapter) for t in d.targets]
                    assert isinstance(broker, MQTTBroker)
                    broker.routes(d.topic, to=targets)
                elif isinstance(d, smol_ast.PollDecl):
                    master = self._resolve(net, d.master, d.line, expect=Adapter)
                    slave = self._resolve(net, d.slave, d.line, expect=ModbusSlave)
                    assert isinstance(master, Adapter)
                    master.polls(
                        slave, register=d.register, count=d.count, rate=d.rate, delay_ms=d.delay_ms
                    )
                elif isinstance(d, smol_ast.ObserveDecl):
                    self._observe(net, d)
                elif isinstance(d, smol_ast.SimulateDecl):
                    if simulate_decl is not None:
                        raise SMOLSemanticError(
                            self.file, d.line, "only one 'simulate' statement is allowed"
                        )
                    simulate_decl = d
            except ValueError as exc:
                raise SMOLSemanticError(self.file, d.line, str(exc)) from exc

        if simulate_decl is None:
            raise SMOLSemanticError(
                self.file, decl.line, "network block must include a 'simulate' statement"
            )

        return self._simulate(net, simulate_decl)

    def _observe(self, net: Network, d: smol_ast.ObserveDecl) -> None:
        if d.metric not in _METRIC_NODE_TYPES:
            raise SMOLSemanticError(
                self.file, d.line, f"unknown or unsupported metric '{d.metric}'"
            )
        expect = _METRIC_NODE_TYPES[d.metric]
        target = self._resolve(net, d.target, d.line, expect=expect)
        net.observe(d.metric, on=target, every=d.every)  # type: ignore[arg-type]

    def _simulate(self, net: Network, d: smol_ast.SimulateDecl) -> SimulationResult:
        if d.mode not in _SIMULATE_MODES:
            raise SMOLSemanticError(
                self.file,
                d.line,
                f"invalid simulate mode '{d.mode}' — expected one of {_SIMULATE_MODES}",
            )
        return net.simulate(duration=d.duration, live=(d.mode == "live"), text=(d.mode == "text"))


def interpret_and_run(path: Path) -> SimulationResult:
    """Parse and run a .smol file end-to-end, returning the SimulationResult."""
    file = str(path)
    text = path.read_text(encoding="utf-8")
    network_decl = parse_smol(text, file)
    return _Interpreter(file).build_and_run(network_decl)
