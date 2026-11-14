"""Wire frame sizes and fixed protocol constants of the model (ADR-0007).

These belong to the model, not to an engine: every backend computes frame
sizes from the scenario with exactly these formulas. Sizes exclude the
Ethernet FCS, consistently for every protocol.
"""

from __future__ import annotations

ETHERNET_IP_TCP_BYTES = 54  # Ethernet (14) + IPv4 (20) + TCP (20)

PN_RT_PRIORITY = 6  # 802.1Q PCP carried by PROFINET RT cyclic frames
PN_CYCLES_MS = tuple(2**n for n in range(10))  # 1, 2, 4, ... 512 ms
PN_MAX_IO_BYTES = 1440

MQTT_PUBACK_BYTES = 58  # Ethernet/IP/TCP + 4 B MQTT PUBACK

MODBUS_REQUEST_BYTES = 65  # Ethernet/IP/TCP + MBAP (6) + FC (1) + addr (2) + quantity (2)


def mqtt_publish_frame_size(topic: str, payload_bytes: int, qos: int) -> int:
    """MQTT PUBLISH: fixed header (2) + topic length (2) + topic + packet id (QoS > 0)."""
    return ETHERNET_IP_TCP_BYTES + 2 + 2 + len(topic) + (2 if qos > 0 else 0) + payload_bytes


def modbus_response_frame_size(count: int) -> int:
    """Read Holding Registers response: MBAP (6) + FC (1) + byte count (1) + 2 B per register."""
    return ETHERNET_IP_TCP_BYTES + 6 + 1 + 1 + 2 * count


def pn_frame_size(io_bytes: int) -> int:
    """PROFINET RT: Ethernet (14) + 802.1Q (4) + FrameID (2) + IO data + APDU status (4),
    padded to the 60 B Ethernet minimum."""
    return max(60, 24 + io_bytes)
