from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from smolpy.core.batch import BatchSpec, parse_batch
from smolpy.core.node import Node

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
        # Wire-level frame size:
        # Ethernet+IPv4+TCP overhead = 54 B
        # MQTT fixed header = 2 B, topic-length field = 2 B
        # topic name = len(topic) B, packet-ID (QoS>0) = 2 B
        ethernet_ip_tcp = 54
        mqtt_overhead = 2 + 2 + len(topic) + (2 if qos > 0 else 0)
        self.frame_size: int = ethernet_ip_tcp + mqtt_overhead + payload_bytes


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
        # Modbus TCP framing:
        # Request  = Ethernet/IP/TCP (54 B) + MBAP (6 B) + FC (1 B)
        #            + Start addr (2 B) + Quantity (2 B) = 65 B, constant.
        # Response = Ethernet/IP/TCP (54 B) + MBAP (6 B) + FC (1 B)
        #            + Byte count (1 B) + Data (2 B × count).
        self.request_frame_size: int = 65
        self.response_frame_size: int = 62 + 2 * count


PN_RT_PRIORITY = 6  # 802.1Q PCP carried by PROFINET RT cyclic frames
_PN_CYCLES_MS = tuple(2**n for n in range(10))  # 1, 2, 4, ... 512 ms
_PN_MAX_IO_BYTES = 1440


def pn_frame_size(io_bytes: int) -> int:
    # Ethernet header (14) + 802.1Q tag (4) + FrameID (2) + IO data + APDU status (4),
    # padded to the 60 B Ethernet minimum (FCS not counted, as for Modbus/MQTT).
    return max(60, 24 + io_bytes)


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
        if cycle_ms not in _PN_CYCLES_MS:
            raise ValueError(f"PROFINET cycle must be one of {list(_PN_CYCLES_MS)} ms")
        for label, n in (("inputs", input_bytes), ("outputs", output_bytes)):
            if not 1 <= n <= _PN_MAX_IO_BYTES:
                raise ValueError(f"PROFINET {label} must be 1-{_PN_MAX_IO_BYTES} bytes")
        if not 1 <= hold <= 255:
            raise ValueError("PROFINET data hold factor must be 1-255")
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
        if pattern != "batch" and spec != parse_batch(1):
            raise ValueError("batch= is only meaningful with pattern='batch'")
        if not 0 <= priority <= 7:
            raise ValueError("priority must be an 802.1Q PCP value 0-7")
        self.traffic_specs.append(
            TrafficSpec(
                to,
                rate,
                size,
                pattern,
                delay_ms,
                batch=spec if pattern == "batch" else None,
                priority=priority,
            )
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
        self.mqtt_specs.append(MQTTSpec(to, topic, rate, payload, qos, delay_ms))

    def polls(
        self,
        slave,  # ModbusSlave
        register: int,
        count: int,
        rate: float = 1.0,  # polls / second
        delay_ms: float = 0.0,
    ) -> None:
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
        from smolpy.profinet.device import ProfinetDevice

        if not isinstance(device, ProfinetDevice):
            raise ValueError(f"{device!r} is not a ProfinetDevice")
        if any(s.device is device for s in self.profinet_specs):
            raise ValueError(f"{self.name!r} already controls {device.name!r}")
        self.profinet_specs.append(ProfinetSpec(device, cycle, inputs, outputs, hold, delay_ms))


def _derive_mac(ip: str) -> str:
    octets = ip.split(".")[-3:]
    return f"02:00:{':'.join(f'{int(o):02x}' for o in octets)}"
