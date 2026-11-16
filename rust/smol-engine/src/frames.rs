//! Ethernet frames on the wire (ADR-0010), as `smolpy.model.frames`.
//!
//! A frame runs from the destination MAC to the FCS, is at least 64 B and
//! carries an 802.1Q tag with a priority above 0. On the wire every frame is
//! preceded by a preamble and SFD (8 B) and followed by an inter-frame gap
//! (12 B).

pub const VLAN_TAG_BYTES: u64 = 4;
pub const PREAMBLE_SFD_BYTES: u64 = 8;
pub const IFG_BYTES: u64 = 12;
pub const WIRE_OVERHEAD_BYTES: u64 = PREAMBLE_SFD_BYTES + IFG_BYTES;

pub const MIN_FRAME_BYTES: u64 = 64;
/// Ethernet header (14), IPv4 (20), TCP (20) and FCS (4).
pub const ETHERNET_IP_TCP_BYTES: u64 = 14 + 20 + 20 + 4;
/// Modbus TCP Read Holding Registers request: MBAP (6), FC, address, quantity.
pub const MODBUS_REQUEST_BYTES: u64 = ETHERNET_IP_TCP_BYTES + 6 + 1 + 2 + 2;

/// A frame padded to the 64 B Ethernet minimum.
pub fn padded(bytes: u64) -> u64 {
    bytes.max(MIN_FRAME_BYTES)
}

/// MQTT PUBACK: fixed header (2) and packet id (2).
pub const MQTT_PUBACK_BYTES: u64 = 64; // 62 B, padded to the minimum

/// MQTT PUBLISH: fixed header (2), topic length (2), the topic, a packet id
/// with a QoS above 0 (2) and the payload. `topic_len` counts characters, as
/// the reference engine's `len(topic)`.
pub fn mqtt_publish_frame_size(topic_len: u64, payload: u64, qos: u8) -> u64 {
    padded(ETHERNET_IP_TCP_BYTES + 2 + 2 + topic_len + if qos > 0 { 2 } else { 0 } + payload)
}

/// Read Holding Registers response: MBAP (6), FC (1), byte count (1), 2 B per register.
pub fn modbus_response_frame_size(count: u16) -> u64 {
    padded(ETHERNET_IP_TCP_BYTES + 6 + 1 + 1 + 2 * count as u64)
}

/// The 802.1Q priority (PCP) of PROFINET RT cyclic frames.
pub const PN_RT_PRIORITY: u8 = 6;

/// PROFINET RT: Ethernet (14), 802.1Q (4), FrameID (2), IO data, APDU
/// status (4) and FCS (4), padded to the minimum.
pub fn pn_frame_size(io_bytes: u64) -> u64 {
    padded(14 + VLAN_TAG_BYTES + 2 + io_bytes + 4 + 4)
}

/// A flow frame of `size` bytes (untagged, FCS included), with the tag a
/// priority above 0 needs.
pub fn flow_frame_size(size: u64, priority: u8) -> u64 {
    size + if priority > 0 { VLAN_TAG_BYTES } else { 0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flow_frames() {
        assert_eq!(flow_frame_size(100, 0), 100);
        assert_eq!(flow_frame_size(100, 3), 104);
        assert_eq!(WIRE_OVERHEAD_BYTES, 20);
        // the values of tests/test_semantics.py::test_frame_sizes
        assert_eq!(MODBUS_REQUEST_BYTES, 69);
        assert_eq!(modbus_response_frame_size(10), 86);
        assert_eq!(modbus_response_frame_size(1), 68);
        assert_eq!(pn_frame_size(20), 64);
        assert_eq!(pn_frame_size(40), 68);
        assert_eq!(pn_frame_size(1400), 1428);
        assert_eq!(MQTT_PUBACK_BYTES, padded(ETHERNET_IP_TCP_BYTES + 4));
        assert_eq!(mqtt_publish_frame_size(7, 20, 0), 58 + 2 + 2 + 7 + 20);
        assert_eq!(mqtt_publish_frame_size(7, 20, 1), 58 + 2 + 2 + 7 + 2 + 20);
        assert_eq!(mqtt_publish_frame_size(1, 0, 0), 64);
    }
}
