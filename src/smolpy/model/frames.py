"""Ethernet frames on the wire and protocol frame sizes (ADR-0010).

These belong to the model, not to an engine: every backend computes frame
sizes from the scenario with exactly these formulas.

A **frame** is an Ethernet frame from the destination MAC to the FCS
inclusive, as switches count it: header (14 B), an 802.1Q tag (4 B) when the
frame has a priority above 0, payload, FCS (4 B); at least 64 B. On the
wire each frame is preceded by a preamble and SFD (8 B) and followed by an
inter-frame gap (12 B).
"""

from __future__ import annotations

ETHERNET_HEADER_BYTES = 14
FCS_BYTES = 4
VLAN_TAG_BYTES = 4  # 802.1Q, on frames with a priority above 0
MIN_FRAME_BYTES = 64
MAX_STANDARD_FRAME_BYTES = 1522  # 1 518 B and an 802.1Q tag: the least a bounded buffer holds
PREAMBLE_SFD_BYTES = 8
IFG_BYTES = 12  # inter-frame gap
WIRE_OVERHEAD_BYTES = PREAMBLE_SFD_BYTES + IFG_BYTES  # 20 B per frame on the wire

IMIX_SIZES = ((64, 0.40), (594, 0.57), (1518, 0.03))  # frame sizes, FCS included

ETHERNET_IP_TCP_BYTES = ETHERNET_HEADER_BYTES + 20 + 20 + FCS_BYTES  # 58: + IPv4 + TCP

PN_RT_PRIORITY = 6  # 802.1Q PCP carried by PROFINET RT cyclic frames
PN_CYCLES_MS = tuple(2**n for n in range(10))  # 1, 2, 4, ... 512 ms
PN_MAX_IO_BYTES = 1440

# The protocol messages below are the TCP payload of one frame each; their
# frames add ETHERNET_IP_TCP_BYTES. The ns-3 backend sends the messages
# themselves over TCP.

MQTT_PUBACK_MESSAGE_BYTES = 4
MQTT_PUBACK_BYTES = max(MIN_FRAME_BYTES, ETHERNET_IP_TCP_BYTES + MQTT_PUBACK_MESSAGE_BYTES)  # 64

MODBUS_REQUEST_MESSAGE_BYTES = 6 + 1 + 2 + 2  # MBAP, FC, address, quantity
MODBUS_REQUEST_BYTES = ETHERNET_IP_TCP_BYTES + MODBUS_REQUEST_MESSAGE_BYTES  # 69


def padded(frame_bytes: int) -> int:
    """A frame padded to the 64 B Ethernet minimum."""
    return max(MIN_FRAME_BYTES, frame_bytes)


def flow_frame_size(size: int, priority: int) -> int:
    """A flow frame of *size* bytes (untagged, FCS included), with the 802.1Q
    tag a priority above 0 needs."""
    return size + (VLAN_TAG_BYTES if priority > 0 else 0)


def mqtt_publish_message_bytes(topic: str, payload_bytes: int, qos: int) -> int:
    """MQTT PUBLISH: fixed header (2) + topic length (2) + topic + packet id (QoS > 0)
    + payload. The topic's length counts characters."""
    return 2 + 2 + len(topic) + (2 if qos > 0 else 0) + payload_bytes


def mqtt_publish_frame_size(topic: str, payload_bytes: int, qos: int) -> int:
    return padded(ETHERNET_IP_TCP_BYTES + mqtt_publish_message_bytes(topic, payload_bytes, qos))


def modbus_response_message_bytes(count: int) -> int:
    """Read Holding Registers response: MBAP (6) + FC (1) + byte count (1) + 2 B per register."""
    return 6 + 1 + 1 + 2 * count


def modbus_response_frame_size(count: int) -> int:
    return padded(ETHERNET_IP_TCP_BYTES + modbus_response_message_bytes(count))


def pn_frame_size(io_bytes: int) -> int:
    """PROFINET RT: Ethernet (14) + 802.1Q (4) + FrameID (2) + IO data + APDU status (4)
    + FCS (4), padded to the 64 B minimum."""
    return padded(ETHERNET_HEADER_BYTES + VLAN_TAG_BYTES + 2 + io_bytes + 4 + FCS_BYTES)
