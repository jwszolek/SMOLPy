from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from smolpy.core.adapter import Adapter
from smolpy.core.network import Network
from smolpy.core.node import Node
from smolpy.lang import ast as smol_ast
from smolpy.lang.errors import SMOLSemanticError
from smolpy.lang.transformer import parse_smol
from smolpy.model import Route, Scenario, traffic_owner
from smolpy.model.validate import ScenarioError, validate
from smolpy.mqtt.broker import MQTTBroker

if TYPE_CHECKING:
    from smolpy.core.network import SimulationResult

_SIMULATE_MODES = ("headless", "text", "live")

# Every semantic rule lives in smolpy.model.validate. The interpreter only
# resolves names, checks the type of the node whose method a statement calls,
# and maps errors back to source lines.


class _Lines:
    """Source line of every Scenario IR element the file declared."""

    def __init__(self) -> None:
        self.nodes: list[int] = []
        self.links: list[int] = []
        self.observations: list[int] = []
        self.traffic: dict[tuple[str, ...], int] = {}
        self._seen: dict[tuple[str, str], int] = {}
        self.run = 0

    def statement(self, kind: str, owner: str, line: int, topic: str | None = None) -> None:
        if topic is not None:  # a route is identified by its topic, later ones replace it
            self.traffic[(kind, owner, topic)] = line
            return
        n = self._seen.get((kind, owner), 0)
        self._seen[(kind, owner)] = n + 1
        self.traffic[(kind, owner, str(n))] = line

    def of(self, scenario: Scenario, where: tuple[str, int] | None) -> int | None:
        if where is None:
            return None
        section, index = where
        if section == "run":
            return self.run
        if section != "traffic":
            return getattr(self, section)[index]  # type: ignore[no-any-return]
        counts: dict[tuple[str, str], int] = {}
        for i, t in enumerate(scenario.traffic):
            owner = traffic_owner(t)
            if isinstance(t, Route):
                key: tuple[str, ...] = (t.kind, owner, t.topic)
            else:
                n = counts.get((t.kind, owner), 0)
                counts[(t.kind, owner)] = n + 1
                key = (t.kind, owner, str(n))
            if i == index:
                return self.traffic.get(key)
        return None


class _Interpreter:
    def __init__(self, file: str) -> None:
        self.file = file

    def _resolve(
        self,
        net: Network,
        name: str,
        line: int,
        *,
        expect: type[Node] | None = None,
    ) -> Node:
        """Find a declared node. *expect* is only for the node whose builder
        method the statement calls; every other type rule is the model's."""
        node = net._nodes.get(name)
        if node is None:
            raise SMOLSemanticError(self.file, line, f"reference to undeclared node '{name}'")
        if expect is not None and not isinstance(node, expect):
            kind = type(node).__name__
            article = "an" if kind[0] in "AEIOU" else "a"
            raise SMOLSemanticError(
                self.file, line, f"'{name}' is {article} {kind}, expected {expect.__name__}"
            )
        return node

    def build_and_run(self, decl: smol_ast.NetworkDecl) -> SimulationResult:
        net, simulate_decl = self.build(decl)
        return self._simulate(net, simulate_decl)

    def build(self, decl: smol_ast.NetworkDecl) -> tuple[Network, smol_ast.SimulateDecl]:
        """Build the network a .smol file describes, without running it."""
        net = Network(decl.name)
        simulate_decl: smol_ast.SimulateDecl | None = None
        lines = _Lines()
        node_decls = (
            smol_ast.AdapterDecl,
            smol_ast.SwitchDecl,
            smol_ast.HubDecl,
            smol_ast.MqttBrokerDecl,
            smol_ast.ModbusSlaveDecl,
            smol_ast.ProfinetDeviceDecl,
        )

        for d in decl.declarations:
            try:
                if isinstance(d, node_decls):
                    lines.nodes.append(d.line)
                if isinstance(d, smol_ast.AdapterDecl):
                    net.adapter(d.name, ip=d.ip, mac=d.mac)
                elif isinstance(d, smol_ast.SwitchDecl):
                    net.switch(
                        d.name,
                        ports=d.ports,
                        mode=d.mode,  # type: ignore[arg-type]
                        qos=d.qos,  # type: ignore[arg-type]
                        mac_table=d.mac_table,  # type: ignore[arg-type]
                    )
                elif isinstance(d, smol_ast.HubDecl):
                    net.hub(d.name, ports=d.ports)
                elif isinstance(d, smol_ast.MqttBrokerDecl):
                    net.mqtt_broker(d.name, ip=d.ip)
                elif isinstance(d, smol_ast.ModbusSlaveDecl):
                    net.modbus_slave(d.name, ip=d.ip, unit_id=d.unit_id)
                elif isinstance(d, smol_ast.ProfinetDeviceDecl):
                    net.profinet_device(d.name, ip=d.ip)
                elif isinstance(d, smol_ast.LinkDecl):
                    a = self._resolve(net, d.a, d.line)
                    b = self._resolve(net, d.b, d.line)
                    net.link(a, b, speed=d.speed, length=d.length)
                    lines.links.append(d.line)
                elif isinstance(d, smol_ast.FlowDecl):
                    src = self._resolve(net, d.src, d.line, expect=Adapter)
                    dst = self._resolve(net, d.dst, d.line)
                    assert isinstance(src, Adapter)
                    src.sends(
                        to=dst,  # type: ignore[arg-type]
                        rate=d.rate,
                        size=d.size,  # type: ignore[arg-type]
                        pattern=d.pattern,  # type: ignore[arg-type]
                        delay_ms=d.delay_ms,
                        batch=d.batch,
                        priority=d.priority,
                    )
                    lines.statement("flow", src.name, d.line)
                elif isinstance(d, smol_ast.PublishDecl):
                    src = self._resolve(net, d.src, d.line, expect=Adapter)
                    broker = self._resolve(net, d.broker, d.line)
                    assert isinstance(src, Adapter)
                    src.publishes(
                        to=broker,
                        topic=d.topic,
                        rate=d.rate,
                        payload=d.payload,
                        qos=d.qos,
                        delay_ms=d.delay_ms,
                    )
                    lines.statement("publish", src.name, d.line)
                elif isinstance(d, smol_ast.RouteDecl):
                    broker = self._resolve(net, d.broker, d.line, expect=MQTTBroker)
                    targets = [self._resolve(net, t, d.line) for t in d.targets]
                    assert isinstance(broker, MQTTBroker)
                    broker.routes(d.topic, to=targets)
                    lines.statement("route", broker.name, d.line, topic=d.topic)
                elif isinstance(d, smol_ast.PollDecl):
                    master = self._resolve(net, d.master, d.line, expect=Adapter)
                    slave = self._resolve(net, d.slave, d.line)
                    assert isinstance(master, Adapter)
                    master.polls(
                        slave, register=d.register, count=d.count, rate=d.rate, delay_ms=d.delay_ms
                    )
                    lines.statement("poll", master.name, d.line)
                elif isinstance(d, smol_ast.ControlDecl):
                    controller = self._resolve(net, d.controller, d.line, expect=Adapter)
                    device = self._resolve(net, d.device, d.line)
                    assert isinstance(controller, Adapter)
                    controller.controls(
                        device,  # type: ignore[arg-type]
                        cycle=d.cycle,
                        inputs=d.inputs,
                        outputs=d.outputs,
                        hold=d.hold,
                        delay_ms=d.delay_ms,
                    )
                    lines.statement("profinet_ar", controller.name, d.line)
                elif isinstance(d, smol_ast.ObserveDecl):
                    target = self._resolve(net, d.target, d.line)
                    net.observe(d.metric, on=target, every=d.every)  # type: ignore[arg-type]
                    lines.observations.append(d.line)
                elif isinstance(d, smol_ast.SimulateDecl):
                    if simulate_decl is not None:
                        raise SMOLSemanticError(
                            self.file, d.line, "only one 'simulate' statement is allowed"
                        )
                    simulate_decl = d
                    lines.run = d.line
            except ValueError as exc:
                raise SMOLSemanticError(self.file, d.line, str(exc)) from exc

        if simulate_decl is None:
            raise SMOLSemanticError(
                self.file, decl.line, "network block must include a 'simulate' statement"
            )
        self._check_mode(simulate_decl)

        # Rules about the scenario as a whole: topology, reachability, duplicates.
        scenario = net.to_scenario(duration=simulate_decl.duration, seed=simulate_decl.seed)
        try:
            validate(scenario)
        except ScenarioError as exc:
            line = lines.of(scenario, exc.where) or decl.line
            raise SMOLSemanticError(self.file, line, str(exc)) from exc
        return net, simulate_decl

    def _check_mode(self, d: smol_ast.SimulateDecl) -> None:
        if d.mode not in _SIMULATE_MODES:
            raise SMOLSemanticError(
                self.file,
                d.line,
                f"invalid simulate mode '{d.mode}' — expected one of {_SIMULATE_MODES}",
            )

    def _simulate(self, net: Network, d: smol_ast.SimulateDecl) -> SimulationResult:
        return net.simulate(
            duration=d.duration,
            live=(d.mode == "live"),
            text=(d.mode == "text"),
            seed=d.seed,
        )


def interpret_and_run(path: Path) -> SimulationResult:
    """Parse and run a .smol file end-to-end, returning the SimulationResult."""
    file = str(path)
    text = path.read_text(encoding="utf-8")
    network_decl = parse_smol(text, file)
    return _Interpreter(file).build_and_run(network_decl)


def interpret_to_scenario(path: Path) -> Scenario:
    """Parse a .smol file into its Scenario IR (ADR-0007) without running it.

    The run configuration comes from the file's ``simulate`` statement; its
    display mode is not part of the scenario.
    """
    file = str(path)
    network_decl = parse_smol(path.read_text(encoding="utf-8"), file)
    net, simulate_decl = _Interpreter(file).build(network_decl)
    return net.to_scenario(duration=simulate_decl.duration, seed=simulate_decl.seed)
