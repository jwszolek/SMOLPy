from __future__ import annotations

from smolpy.core.adapter import _derive_mac
from smolpy.core.node import Node
from smolpy.model import MqttBrokerNode
from smolpy.model.validate import check_node


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
        self._routes: dict[str, list] = {}  # topic → [Adapter, ...]
        check_node(self.to_spec())

    def to_spec(self) -> MqttBrokerNode:
        return MqttBrokerNode(self.name, self.ip, self.mac)

    def routes(self, topic: str, to: list) -> None:
        """Register subscriber adapters for *topic*; replaces earlier ones."""
        from smolpy.model import Route
        from smolpy.model.validate import check_traffic

        subscribers = list(to)
        route = Route(self.name, topic, tuple(a.name for a in subscribers))
        check_traffic(route, {n.name: n.to_spec() for n in [self, *subscribers]})
        self._routes[topic] = subscribers
