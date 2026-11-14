from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from smolpy import model
from smolpy.core.batch import BatchSpec, parse_batch
from smolpy.core.node import Node
from smolpy.model.frames import (
    MODBUS_REQUEST_BYTES,
    PN_RT_PRIORITY,  # noqa: F401  (re-exported for existing imports)
    modbus_response_frame_size,
    mqtt_publish_frame_size,
    pn_frame_size,
)
from smolpy.model.validate import ScenarioError, check_node, check_traffic, describe

if TYPE_CHECKING:
    from smolpy.profinet.device import ProfinetDevice

TrafficPattern = Literal["constant", "poisson", "bursty", "batch"]
FrameSize = int | Literal["imix"]


class TrafficSpec:
    def __init__(
        self,
        destination: Adapter,
        rate: float,
        size: FrameSize,
        pattern: TrafficPattern,
        delay_ms: float = 0.0,
        batch: BatchSpec | None = None,
        priority: int = 0,
    ) -> None:
        self.destination = destination
        self.rate = rate  # frames/s (long-run average, also for "batch")
        self.size = size  # bytes or "imix"
        self.pattern = pattern
        self.delay_ms = delay_ms
        self.batch = batch  # group-size distribution, only for pattern="batch"
        self.priority = priority  # 802.1Q PCP 0-7


class MQTTSpec:
    def __init__(
        self,
        broker,  # MQTTBroker
        topic: str,
        rate_hz: float,  # messages per second
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
        self.frame_size: int = mqtt_publish_frame_size(topic, payload_bytes, qos)


class ModbusSpec:
    def __init__(
        self,
        slave,  # ModbusSlave
        register: int,
        count: int,
        rate_hz: float,
        delay_ms: float = 0.0,
    ) -> None:
        self.slave = slave
        self.register = register
        self.count = count
        self.rate_hz = rate_hz
        self.delay_ms = delay_ms
        # Frame sizes are part of the model: smolpy.model.frames.
        self.request_frame_size: int = MODBUS_REQUEST_BYTES
        self.response_frame_size: int = modbus_response_frame_size(count)


class ProfinetSpec:
    """One PROFINET RT Application Relationship (AR) between controller and device."""

    def __init__(
        self,
        device: ProfinetDevice,
        cycle_ms: int,
        input_bytes: int,
        output_bytes: int,
        hold: int = 3,
        delay_ms: float = 0.0,
    ) -> None:
        self.device = device
        self.cycle_ms = cycle_ms
        self.input_bytes = input_bytes  # device -> controller
        self.output_bytes = output_bytes  # controller -> device
        self.hold = hold
        self.delay_ms = delay_ms
        self.input_frame_size: int = pn_frame_size(input_bytes)
        self.output_frame_size: int = pn_frame_size(output_bytes)


class Adapter(Node):
    """NIC — generates and receives Ethernet frames."""

    def __init__(self, name: str, ip: str, mac: str | None = None) -> None:
        super().__init__(name)
        self.ip = ip
        self.mac = mac or _derive_mac(ip)
        self.traffic_specs: list[TrafficSpec] = []
        self.mqtt_specs: list[MQTTSpec] = []
        self.modbus_specs: list[ModbusSpec] = []
        self.profinet_specs: list[ProfinetSpec] = []
        check_node(self.to_spec())

    def to_spec(self) -> model.AdapterNode:
        return model.AdapterNode(self.name, self.ip, self.mac)

    def _check(self, statement: model.TrafficSpec, *others: Node) -> None:
        """Rules of *statement* (smolpy.model.validate), at the call that made it."""
        check_traffic(statement, {n.name: n.to_spec() for n in (self, *others)})

    def sends(
        self,
        to: Adapter,
        rate: float,
        size: FrameSize = 512,
        pattern: TrafficPattern = "constant",
        delay_ms: float = 0.0,
        batch: int | str = 1,
        priority: int = 0,
    ) -> None:
        # batch: group size for pattern="batch" -- an int k, "geom:<mean>" or
        # "pareto:<shape>" (see smolpy.core.batch). rate stays the average
        # number of frames per second.
        spec = parse_batch(batch)
        # The default batch=1 means "not batched" for the other patterns.
        group = spec if pattern == "batch" or spec != parse_batch(1) else None
        self._check(
            model.Flow(self.name, to.name, rate, size, pattern, delay_ms, group, priority), to
        )
        self.traffic_specs.append(
            TrafficSpec(to, rate, size, pattern, delay_ms, batch=group, priority=priority)
        )

    def publishes(
        self,
        to,  # MQTTBroker
        topic: str,
        rate: float = 1.0,  # messages / second
        payload: int = 20,  # payload bytes
        qos: int = 0,
        delay_ms: float = 0.0,
    ) -> None:
        self._check(model.Publish(self.name, to.name, topic, rate, payload, qos, delay_ms), to)
        self.mqtt_specs.append(MQTTSpec(to, topic, rate, payload, qos, delay_ms))

    def polls(
        self,
        slave,  # ModbusSlave
        register: int,
        count: int,
        rate: float = 1.0,  # polls / second
        delay_ms: float = 0.0,
    ) -> None:
        self._check(model.Poll(self.name, slave.name, register, count, rate, delay_ms), slave)
        self.modbus_specs.append(ModbusSpec(slave, register, count, rate, delay_ms))

    def controls(
        self,
        device: ProfinetDevice,
        cycle: int,  # update time, ms
        inputs: int,  # input data bytes per frame (device -> controller)
        outputs: int,  # output data bytes per frame (controller -> device)
        hold: int = 3,  # data hold factor: watchdog = hold x cycle
        delay_ms: float = 0.0,
    ) -> None:
        ar = model.ProfinetAR(self.name, device.name, cycle, inputs, outputs, hold, delay_ms)
        self._check(ar, device)
        if any(s.device is device for s in self.profinet_specs):
            raise ScenarioError(f"{describe(ar)}: {self.name!r} already controls {device.name!r}")
        self.profinet_specs.append(ProfinetSpec(device, cycle, inputs, outputs, hold, delay_ms))


def _derive_mac(ip: str) -> str:
    """Locally administered unicast MAC 02:00:<the four IPv4 octets>, e.g.
    10.0.4.1 -> 02:00:0a:00:04:01: six bytes, unique per IPv4 address."""
    octets = ip.split(".")
    if len(octets) != 4 or not all(o.isdigit() and int(o) <= 255 for o in octets):
        raise ValueError(f"cannot derive a MAC address from {ip!r}: not an IPv4 address")
    return "02:00:" + ":".join(f"{int(o):02x}" for o in octets)
