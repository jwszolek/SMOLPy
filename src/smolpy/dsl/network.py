from __future__ import annotations

import networkx as nx

from smolpy.dsl.adapter import Adapter
from smolpy.dsl.hub import Hub
from smolpy.dsl.link import Link
from smolpy.dsl.mqtt_broker import MQTTBroker
from smolpy.dsl.node import Node
from smolpy.dsl.observation import MetricName, Observation
from smolpy.dsl.switch import Switch, SwitchMode


class SimulationResult:
    def __init__(
        self,
        metrics: dict[str, list[tuple[float, float]]],
        network: Network,
    ) -> None:
        self.metrics = metrics
        self._network = network

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
                "network": self._network.name,
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

    def switch(self, name: str, ports: int, mode: SwitchMode = "store-and-forward") -> Switch:
        node = Switch(name, ports=ports, mode=mode)
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

    def link(self, a: Node, b: Node, speed: float, length: float, duplex: bool = True) -> Link:
        lnk = Link(endpoint_a=a, endpoint_b=b, speed=speed, length=length, duplex=duplex)
        a.links.append(lnk)
        b.links.append(lnk)
        self._links.append(lnk)
        self._graph.add_edge(a.name, b.name, link=lnk)
        return lnk

    # --- observation builder ---

    def observe(self, metric: MetricName, on: Node, every: float) -> None:
        self._observations.append(Observation(metric=metric, target=on, interval_ms=every))

    # --- simulation control ---

    def simulate(
        self,
        duration: float,
        *,
        live: bool = False,
        text: bool = False,
    ) -> SimulationResult:
        import os

        if os.environ.get("SMOLPY_TEXT_MODE") == "1":
            live, text = False, True
        if live:
            from smolpy.viz.dashboard import show_live

            return show_live(self, duration)
        if text:
            from smolpy.viz.text_dashboard import show_text

            return show_text(self, duration)
        from smolpy.sim.engine import run_simulation

        return run_simulation(self, duration_ms=duration)

    # --- internals ---

    def _register(self, node: Node) -> None:
        if node.name in self._nodes:
            raise ValueError(f"Node {node.name!r} already exists in network {self.name!r}")
        self._nodes[node.name] = node
        self._graph.add_node(node.name, node=node)
