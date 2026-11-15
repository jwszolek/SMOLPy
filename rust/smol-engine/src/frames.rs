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
    }
}
