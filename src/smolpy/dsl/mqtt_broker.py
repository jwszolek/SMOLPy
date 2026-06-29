from __future__ import annotations

from smolpy.dsl.adapter import _derive_mac
from smolpy.dsl.node import Node


class MQTTBroker(Node):
    """Application-layer MQTT message broker.

    Receives PUBLISH frames from client adapters and forwards copies
    to all registered subscribers for each topic.
    Supports QoS 0 (fire-and-forget) and QoS 1 (PUBACK acknowledgement).
    """
    def __init__(self, name: str, ip: str, mac: str | None = None) -> None:
        super().__init__(name)
        self.ip = ip
        self.mac = mac or _derive_mac(ip)
        self._routes: dict[str, list] = {}   # topic → [Adapter, ...]

    def routes(self, topic: str, to: list) -> None:
        """Register subscriber adapters for *topic*."""
        self._routes[topic] = list(to)
