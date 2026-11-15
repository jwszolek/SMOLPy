# ADR-0010: Ethernet Frames on the Wire

**Status:** Accepted  
**Date:** 2026-11-15  
**Amends:** ADR-0007 (the IR becomes version 2), [Model Semantics](../model-semantics.md) §2, §3 and §8  
**Resolves:** Model Semantics open issue 4

---

## Context

Until now the model put fewer bytes on a link than real Ethernet does:

- **No wire overhead.** A frame took `size × 8 / speed` on the link. Every real frame also carries 20 B the model left out: a preamble and SFD (8 B) before it and an inter-frame gap (12 B) after it. So the model's links carried more frames than real ones. At 100 Mb/s it moved about 195 000 frames of 64 B per second, where real Ethernet moves 148 809. That is 31 % more frames, and about 1.3 % more for 1 518 B frames.
- **`size` meant two things.** The IMIX sizes (64, 594 and 1 518 B) follow the usual convention, which includes the FCS. The protocol formulas (54 B for Ethernet, IPv4 and TCP) left the FCS out.
- **Frames could be shorter than Ethernet allows.** Only PROFINET frames were padded, to 60 B without the FCS. An MQTT PUBACK was 58 B, and a flow could be 1 B.
- **Only PROFINET carried an 802.1Q tag.** A flow with a `priority` was queued by its PCP but did not grow by the 4 B tag that carries the PCP.

These differences matter where the model is meant to be used: links near saturation and small industrial frames. They also stand between SMOLPy and ns-3 (ADR-0004), whose CSMA device counts the header and FCS of every frame.

### Options considered

- **A — keep the simplification** and only document that `size` excludes the FCS. This is cheap, but small-frame results stay up to 31 % optimistic.
- **B — model the frame and its overhead on the wire.** **Chosen.**
- **C — a scenario switch between an ideal and an Ethernet wire.** This would mean two models to maintain and two sets of results to compare, and every result would need its switch setting to be read correctly.

---

## Decision

### The frame

A **frame** is an Ethernet frame from the destination MAC to the FCS inclusive, which is what switch counters count:

- **Header:** 14 B.
- **802.1Q tag:** 4 B, on every frame with a priority above 0. That covers PROFINET RT frames (PCP 6) and flows with a `priority`.
- **Payload.**
- **FCS:** 4 B.
- **Minimum:** 64 B. Protocol frames that would be shorter are padded.

The formulas, in `smolpy.model.frames`:

| Frame | Size (B) |
|---|---|
| `Flow` | `size`, + 4 with a priority above 0. `size` is the untagged frame, FCS included, at least 64 B; IMIX is 64, 594 or 1 518 B. |
| MQTT PUBLISH | 58 + 2 + 2 + len(topic) + (2 if qos > 0) + payload, at least 64 |
| MQTT PUBACK | 64 (62, padded) |
| Modbus request (FC 03) | 69 |
| Modbus response | 66 + 2 × count, at least 64 |
| PROFINET RT cyclic frame | max(64, 28 + io_bytes) |

Here 58 B is the Ethernet header, IPv4, TCP and FCS (14 + 20 + 20 + 4). PROFINET's 28 B are the header, tag, FrameID, APDU status and FCS (14 + 4 + 2 + 4 + 4).

- **No silent padding for flows.** A flow `size` below 64 B is a validation error, not padded silently: the user asked for a frame that cannot exist. There is no upper limit, so jumbo frames stay possible.

### On the wire

- **Timing on the link.** Each frame is preceded by the preamble and SFD (8 B) and followed by the inter-frame gap (12 B):
    - the receiver gets the frame when its FCS has arrived: `tx(8 + frame) + propagation` after the transmission starts;
    - the link is free for the next frame `tx(12)` after that, at the end of the gap.
- **Rounding.** Both times are rounded as in Model Semantics §1. They are exact at 10 Mb/s, 100 Mb/s and 1 Gb/s.
- **Saturation.** A saturated link therefore carries `speed / ((frame + 20) × 8)` frames per second, as real Ethernet does.

### Metrics

- **Byte metrics count frames.** `throughput`, `bytes_sent` and `bytes_received` count frame bytes, header to FCS. They do not count the preamble, SFD or gap, the same as switch and NIC counters. A saturated 100 Mb/s link with 64 B frames shows 76.2 Mb/s of throughput.
- **`utilization` counts the wire.** It counts frame + 20 B per frame, the time the link is occupied, so a saturated link shows 100 %. A frame's wire bytes count when its FCS has been sent.

### The IR becomes version 2

The meaning of `Flow.size` and of link timing changes, so by ADR-0007's rule the JSON format version goes from 1 to 2. Version 1 documents are rejected with an explanation, not read with the new meaning. Rebuilding the scenario from its `Network` or `.smol` file gives a version 2 document. The fields themselves do not change.

### ns-3 (ADR-0004)

The mapping onto ns-3's CSMA device is direct:
- CSMA already adds the Ethernet header and FCS to every packet;
- ns-3's CSMA device does not model a preamble, so its `InterframeGap` attribute is set to the time of 20 B (preamble, SFD and gap) at the link's speed. This is to be checked against the pinned ns-3 release in Phase 6;
- flow payloads are sized so that the frame matches `size`.

---

## Consequences

**Positive**
- **Link capacity is real Ethernet's** for every frame size, including at saturation and for small industrial frames.
- **`size` has one meaning everywhere**, the one that IMIX, standards and switch counters use.
- **The model is closer to ns-3**, which makes ADR-0004's low-load comparisons meaningful.
- **Priority flows carry their tag**, so priority traffic uses the bandwidth it uses on a real network.

**Negative / trade-offs**
- **Results change once.**
    - Latencies grow by the preamble and SFD on every hop: 0.64 µs per hop at 100 Mb/s.
    - Utilization grows by the overhead.
    - Links that were just below saturation may now be saturated.
    - B1–B4 get new hashes.
- **Version 1 scenario documents must be regenerated.**
- **Flows with `size` below 64 B, which were accepted before, are now rejected.**
