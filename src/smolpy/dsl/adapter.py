from __future__ import annotations
from typing import Literal, TYPE_CHECKING
from smolpy.dsl.node import Node

if TYPE_CHECKING:
    pass

TrafficPattern = Literal["constant", "poisson", "bursty"]
FrameSize = int | Literal["imix"]


class TrafficSpec:
    def __init__(
        self,
        destination: Adapter,
        rate: float,
        size: FrameSize,
        pattern: TrafficPattern,
        delay_ms: float = 0.0,
    ) -> None:
        self.destination = destination
        self.rate = rate        # frames/s
        self.size = size        # bytes or "imix"
        self.pattern = pattern
        self.delay_ms = delay_ms


class MQTTSpec:
    def __init__(
        self,
        broker,           # MQTTBroker
        topic: str,
        rate_hz: float,   # messages per second
        payload_bytes: int,
        qos: int,
        delay_ms: float = 0.0,
    ) -> None:
        self.broker = broker
        self.topic = topic
        self.rate_hz = rate_hz
        self.payload_bytes = payload_bytes
        self.qos = qos
        self.delay_ms = delay_ms
        # Wire-level frame size:
        # Ethernet+IPv4+TCP overhead = 54 B
        # MQTT fixed header = 2 B, topic-length field = 2 B
        # topic name = len(topic) B, packet-ID (QoS>0) = 2 B
        ethernet_ip_tcp = 54
        mqtt_overhead = 2 + 2 + len(topic) + (2 if qos > 0 else 0)
        self.frame_size: int = ethernet_ip_tcp + mqtt_overhead + payload_bytes


class Adapter(Node):
    """NIC — generates and receives Ethernet frames."""

    def __init__(self, name: str, ip: str, mac: str | None = None) -> None:
        super().__init__(name)
        self.ip = ip
        self.mac = mac or _derive_mac(ip)
        self.traffic_specs: list[TrafficSpec] = []
        self.mqtt_specs: list[MQTTSpec] = []

    def sends(
        self,
        to: Adapter,
        rate: float,
        size: FrameSize = 512,
        pattern: TrafficPattern = "constant",
        delay_ms: float = 0.0,
    ) -> None:
        self.traffic_specs.append(TrafficSpec(to, rate, size, pattern, delay_ms))

    def publishes(
        self,
        to,                        # MQTTBroker
        topic: str,
        rate: float = 1.0,         # messages / second
        payload: int = 20,         # payload bytes
        qos: int = 0,
        delay_ms: float = 0.0,
    ) -> None:
        self.mqtt_specs.append(MQTTSpec(to, topic, rate, payload, qos, delay_ms))


def _derive_mac(ip: str) -> str:
    octets = ip.split(".")[-3:]
    return f"02:00:{':'.join(f'{int(o):02x}' for o in octets)}"
