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

/// Read Holding Registers response: MBAP (6), FC (1), byte count (1), 2 B per register.
pub fn modbus_response_frame_size(count: u16) -> u64 {
    padded(ETHERNET_IP_TCP_BYTES + 6 + 1 + 1 + 2 * count as u64)
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
    }
}
