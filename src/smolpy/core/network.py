from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import networkx as nx

from smolpy.core.adapter import Adapter
from smolpy.core.link import Link
from smolpy.core.node import Node
from smolpy.core.observation import MetricName, Observation
from smolpy.ethernet.hub import Hub
from smolpy.ethernet.switch import MacTableMode, Switch, SwitchMode, SwitchQoS
from smolpy.modbus.slave import ModbusSlave
from smolpy.mqtt.broker import MQTTBroker
from smolpy.profinet.device import ProfinetDevice

if TYPE_CHECKING:
    from smolpy.model import Scenario


@dataclass(frozen=True)
class RunStats:
    """What a backend reports about its own run (ADR-0007).

    *events* is the number of simulation events processed; *wall_s* the
    wall-clock time of the run, without building the model.
    """

    backend: str
    events: int
    wall_s: float


class SimulationResult:
    def __init__(
        self,
        metrics: dict[str, list[tuple[float, float]]],
        scenario: Scenario,
        stats: RunStats | None = None,
    ) -> None:
        self.metrics = metrics
        self.scenario = scenario
        self.stats = stats

    def export(self, path: str, format: str | None = None) -> None:
        """Write all metric time-series to *path*.

        Format is inferred from the file extension when *format* is omitted.
        Supported: ``"csv"`` (long format: time_ms, metric, value)
        and ``"json"`` (dict of lists-of-pairs).
        """
        import csv
        import json
        import pathlib

        p = pathlib.Path(path)
        fmt = (format or p.suffix.lstrip(".")).lower()

        if fmt == "csv":
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["time_ms", "metric", "value"])
                for key in sorted(self.metrics):
                    for t, v in self.metrics[key]:
                        w.writerow([f"{t:.3f}", key, f"{v:.6f}"])
        elif fmt == "json":
            payload = {
                "network": self.scenario.name,
                "metrics": {
                    k: [[round(t, 3), round(v, 6)] for t, v in s] for k, s in self.metrics.items()
                },
            }
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        else:
            raise ValueError(f"Unknown format {fmt!r}. Supported formats: 'csv', 'json'.")

    def plot(self) -> None:
        from smolpy.viz.dashboard import show

        show(self)

    def report(self) -> None:
        if not self.metrics:
            print("No metrics collected. Add net.observe(...) calls before simulating.")
            return
        _UNITS = {
            "throughput": "Mb/s",
            "latency": "µs",
            "jitter": "µs",
            "frame_loss": "%",
            "collision_rate": "/s",
            "queue_depth": "fr",
            "utilization": "%",
            "bytes_sent": "MB",
            "bytes_received": "MB",
        }
        col = max(len(k) for k in self.metrics) + 2
        header = f"{'Metric':<{col}}  {'n':>6}  {'avg':>10}  {'min':>10}  {'max':>10}"
        print(f"\n{header}")
        print("-" * len(header))
        for key in sorted(self.metrics):
            vals = [v for _, v in self.metrics[key]]
            if not vals:
                continue
            unit = next((u for m, u in _UNITS.items() if m in key), "")
            avg = sum(vals) / len(vals)
            print(
                f"{key:<{col}}  {len(vals):>6}  {avg:>9.3f}{unit:>5}"
                f"  {min(vals):>9.3f}{unit:>5}  {max(vals):>9.3f}{unit:>5}"
            )
        print()


class Network:
    """Top-level container — describes topology, traffic, and observations."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._graph: nx.Graph = nx.Graph()
        self._nodes: dict[str, Node] = {}
        self._links: list[Link] = []
        self._observations: list[Observation] = []

    # --- topology builders ---

    def adapter(self, name: str, ip: str, mac: str | None = None) -> Adapter:
        node = Adapter(name, ip=ip, mac=mac)
        self._register(node)
        return node

    def switch(
        self,
        name: str,
        ports: int,
        mode: SwitchMode = "store-and-forward",
        qos: SwitchQoS = "strict",
        mac_table: MacTableMode = "learning",
    ) -> Switch:
        node = Switch(name, ports=ports, mode=mode, qos=qos, mac_table=mac_table)
        self._register(node)
        return node

    def hub(self, name: str, ports: int) -> Hub:
        node = Hub(name, ports=ports)
        self._register(node)
        return node

    def mqtt_broker(self, name: str, ip: str, mac: str | None = None) -> MQTTBroker:
        node = MQTTBroker(name, ip=ip, mac=mac)
        self._register(node)
        return node

    def modbus_slave(self, name: str, ip: str, unit_id: int, mac: str | None = None) -> ModbusSlave:
        node = ModbusSlave(name, ip=ip, unit_id=unit_id, mac=mac)
        self._register(node)
        return node

    def profinet_device(self, name: str, ip: str, mac: str | None = None) -> ProfinetDevice:
        node = ProfinetDevice(name, ip=ip, mac=mac)
        self._register(node)
        return node

    def link(self, a: Node, b: Node, speed: float, length: float, duplex: bool = True) -> Link:
        from smolpy import model
        from smolpy.model.validate import check_link

        check_link(model.Link(a.name, b.name, speed, length, duplex))
        lnk = Link(endpoint_a=a, endpoint_b=b, speed=speed, length=length, duplex=duplex)
        a.links.append(lnk)
        b.links.append(lnk)
        self._links.append(lnk)
        self._graph.add_edge(a.name, b.name, link=lnk)
        return lnk

    # --- observation builder ---

    def observe(self, metric: MetricName, on: Node, every: float) -> None:
        from smolpy import model
        from smolpy.model.validate import check_observation

        check_observation(model.Observation(metric, on.name, every), on.to_spec())
        self._observations.append(Observation(metric=metric, target=on, interval_ms=every))

    # --- simulation control ---

    def simulate(
        self,
        duration: float,
        *,
        live: bool = False,
        text: bool = False,
        seed: int = 42,
    ) -> SimulationResult:
        import os

        if os.environ.get("SMOLPY_TEXT_MODE") == "1":
            live, text = False, True
        scenario = self.to_scenario(duration=duration, seed=seed)
        if live:
            from smolpy.viz.dashboard import show_live

            return show_live(scenario)
        if text:
            from smolpy.viz.text_dashboard import show_text

            return show_text(scenario)
        from smolpy.sim.engine import run_simulation

        return run_simulation(scenario)

    # --- scenario IR ---

    def to_scenario(self, duration: float | None = None, seed: int = 42) -> Scenario:
        """Return the execution-independent description of this network (ADR-0007).

        *duration* (ms) and *seed* form the run configuration, as in
        :meth:`simulate`; without *duration* the scenario has no run section.
        """
        from smolpy import model

        nodes: list[model.NodeSpec] = []
        traffic: list[model.TrafficSpec] = []
        for node in self._nodes.values():
            nodes.append(node.to_spec())
            if isinstance(node, Adapter):
                # Per source, in the order the SimPy engine starts its generators.
                traffic.extend(
                    model.ProfinetAR(
                        node.name,
                        pn.device.name,
                        pn.cycle_ms,
                        pn.input_bytes,
                        pn.output_bytes,
                        pn.hold,
                        pn.delay_ms,
                    )
                    for pn in node.profinet_specs
                )
                traffic.extend(
                    model.Flow(
                        node.name,
                        t.destination.name,
                        t.rate,
                        t.size,
                        t.pattern,
                        t.delay_ms,
                        t.batch,
                        t.priority,
                    )
                    for t in node.traffic_specs
                )
                traffic.extend(
                    model.Publish(
                        node.name,
                        m.broker.name,
                        m.topic,
                        m.rate_hz,
                        m.payload_bytes,
                        m.qos,
                        m.delay_ms,
                    )
                    for m in node.mqtt_specs
                )
                traffic.extend(
                    model.Poll(node.name, p.slave.name, p.register, p.count, p.rate_hz, p.delay_ms)
                    for p in node.modbus_specs
                )
            elif isinstance(node, MQTTBroker):
                traffic.extend(
                    model.Route(node.name, topic, tuple(a.name for a in subscribers))
                    for topic, subscribers in node._routes.items()
                )

        return model.Scenario(
            name=self.name,
            nodes=tuple(nodes),
            links=tuple(
                model.Link(lk.endpoint_a.name, lk.endpoint_b.name, lk.speed, lk.length, lk.duplex)
                for lk in self._links
            ),
            traffic=tuple(traffic),
            observations=tuple(
                model.Observation(o.metric, o.target.name, o.interval_ms)
                for o in self._observations
            ),
            run=None if duration is None else model.RunConfig(duration, seed),
        )

    # --- internals ---

    def _register(self, node: Node) -> None:
        if node.name in self._nodes:
            raise ValueError(f"Node {node.name!r} already exists in network {self.name!r}")
        self._nodes[node.name] = node
        self._graph.add_node(node.name, node=node)
