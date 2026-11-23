# ADR-0011: Bounded Switch Buffers with Frame Loss

**Status:** Proposed  
**Date:** 2026-11-23  
**Amends:** ADR-0007 (a new optional field of the IR, which stays version 2), ADR-0004 (the `smol-ns3` line format becomes version 2), [Model Semantics](../model-semantics.md) §3, §5, §8 and §9  
**Roadmap:** the candidate "Bounded switch buffers with frame loss"

---

## Context

Model Semantics §3 says: *"Queues are unbounded, so no frame is ever dropped for lack of buffer."* Below saturation that costs nothing. Near saturation and above it, it is where the model and a real network part ways.

**A hardware testbed shows where.** The setup:
- **Switch:** a TP-Link LS108GP, an unmanaged 8 × 1 Gb/s store-and-forward switch.
- **Traffic:** a Raspberry Pi on a 1 Gb/s port sends Poisson UDP traffic, 1 518 B frames, to a laptop whose port runs at 100 Mb/s.
- **Polling:** the laptop polls a Modbus TCP server on the Pi every 10 ms. The responses share the laptop's egress port with the UDP stream.

| Offered load ρ of the 100 Mb/s port | Measured | SMOLPy (unbounded queues) |
|---|---|---|
| up to 0.93 | Modbus RTT: mean wait, median, p90, p99 | the same, within a few percent |
| 0.996 | RTT capped at about 9.8 ms; 0.09 % of UDP frames lost | median 11 ms, p99 79 ms, nothing lost |

The cap and the loss point to a bounded egress buffer of about 115 kB, or about 75 frames of 1 538 B on the wire: 75 × 1 538 × 8 / 100 Mb/s = 9.2 ms. An unbounded queue at ρ ≈ 1 grows without limit, and so does the delay it predicts.

The same limitation appears in the ns-3 load study ([Execution backends](../backends.md#modbus-and-mqtt-over-tcp)). There nothing is dropped, so ns-3's TCP never retransmits, and its overload behaviour comes only from its windows.

### Options considered

- **A — keep unbounded queues** and document the limit of validity. Cheap, but the model then cannot be used at and above saturation, which is where industrial networks are dimensioned.
- **B — a bounded egress buffer per switch port, with tail drop.** **Chosen.** It is what the unmanaged switches of the testbed do. It needs one new scenario parameter, and every engine can reproduce it exactly.
- **C — a shared buffer pool per switch with dynamic thresholds**, as managed switches have. Closer to some hardware, but the sharing policy is vendor-specific and has no single correct model. It stays a possible later extension of B (below).
- **D — active queue management (RED, CoDel).** Not what industrial unmanaged switches do. Out of scope.

---

## Decision

### The parameter

A switch takes an optional **egress buffer limit in bytes**, the same for each of its ports:

```
switch sw ports=8 buffer=115000
switch sw ports=8 buffer=115kB        # 115 000 B
switch sw ports=8 buffer=112KiB       # 114 688 B
```

```python
sw = net.switch("sw", ports=8, buffer=115_000)
```

- **Unit: bytes.** Real switches allocate buffer memory by size, not by frame count. A limit in frames would let 64 B and 1 518 B frames take the same space.
- **Suffixes** in `.smol`: `kB` (10³), `MB` (10⁶), `KiB` (2¹⁰), `MiB` (2²⁰). The value must come out as a whole number of bytes. The Python API takes an `int`.
- **Default: unbounded,** as now. A scenario without `buffer` means exactly what it meant before: same results, same event counts, same benchmark hashes (B1–B4) and the same equivalence suite.
- **Validation.** `buffer` must be a positive integer, not a bool. It must be at least one maximum frame: 1 522 B, the largest standard frame with an 802.1Q tag, and at least the largest frame that any traffic statement of the scenario sends. A jumbo flow would otherwise be dropped in full at every bounded switch it crosses.
- **IR.** `SwitchNode.buffer_bytes: int | None = None`, `None` meaning unbounded.

### What occupies the buffer

- **Frame bytes.** A frame occupies its size as defined in Model Semantics §2: header to FCS, its 802.1Q tag included, without the preamble, SFD or gap. That is how switches account buffer memory, and how `bytes_sent` and `bytes_received` already count.
- **Only waiting frames.** The buffer holds exactly the frames that `queue_depth` counts: those queued on the port and not yet taken by its transmitter. **The frame in transmission does not occupy the buffer.**
    - It has left the queue: one notion of "queued" serves both `queue_depth` and the limit.
    - The delay a frame can see is then bounded simply: what is queued ahead of it, plus at most one frame in transmission (below).
    - A real switch also holds the frame it is sending, so its memory is `buffer` plus one frame per port. For the testbed that is one frame in 76, or 1.3 %: `buffer` is the switch's memory less one maximum frame.
- **When the transmitter takes a frame.** This is today's behaviour, now stated, because the limit makes it observable:
    - when the transmitter has sent a frame's FCS, it takes the next frame at once, if there is one. That frame then waits out the inter-frame gap as part of its own transmission (§3);
    - when the transmitter is idle, it takes a frame queued at instant `t` at the same instant, **after the events already scheduled for `t`** (§9). Frames queued in between at `t` are in the buffer together, and the transmitter takes the highest-priority one of them.

### Admission: tail drop

A frame of size `s` that arrives at a port with `q` bytes queued:
- is **queued** if `q + s ≤ buffer`;
- is **dropped** otherwise. It is never queued, and nothing else changes: no queued frame is displaced, and the transmitter does not notice.

Further rules:
- **Priorities.** One limit per port is shared by all 8 priority classes. The arriving frame is dropped whatever its priority. A PROFINET frame (PCP 6) that arrives at a port full of best-effort frames is lost. That is what a single-buffer unmanaged switch does. With `qos="fifo"` there is one class, and nothing else changes.
- **Flooding.** Each copy of a flooded frame is admitted or dropped on its own port.
- **Simultaneous arrivals.** Admissions at the same instant are decided in the order the frames are queued (§9). That is the order in which the switch forwards them, and within one forward, port order for flooded copies. Together with the rule on when the transmitter takes a frame, every engine decides every drop the same way.
- **Bound on delay.** A frame admitted with `n` frames of `q ≤ buffer − s` bytes ahead of it waits at most `tx(q + 20·n) + tx(8 + m) + tx(12)` before its own preamble starts. Here `m` is the largest frame in the scenario, for the frame in transmission and its gap. Its queueing delay is therefore at most about `buffer / speed` plus one frame time.

### Scope

- **Per egress port of a switch.** Ingress buffering is not modelled. A switch forwards in zero time (§4).
- **End stations stay unbounded.** The egress queues of adapters, MQTT brokers, Modbus slaves and PROFINET devices have no limit. A station never drops a frame it generates, as a host's socket buffer and qdisc would push back on the application instead. The scenario offers its traffic, and only the network loses it.
- **Possible later extensions, not part of this decision:**
    - a buffer pool shared by the ports of a switch, with per-port thresholds;
    - limits per priority class;
    - a limit in frames, for switches that count descriptors.

### Protocols when a frame is lost

**On SimPy and Rust there is no transport layer.** SMOL sizes Modbus TCP and MQTT frames as TCP segments but models no TCP behaviour, so nothing is retransmitted:
- **Modbus TCP:** a lost request or response leaves its poll without a response. The master does not retry, and its next polls go out on schedule.
- **MQTT:**
    - a lost PUBLISH is lost: the broker forwards nothing and sends no PUBACK;
    - a lost forwarded copy is lost to that subscriber;
    - a lost PUBACK changes nothing, since publishers do not wait for it.
- **PROFINET RT:** there is no retransmission in PROFINET RT either. A lost cyclic frame leaves a gap. If no frame arrives within `hold × cycle`, the watchdog expires (§7), as on a real device.
- **Flows (UDP-like):** lost.

**This is a limitation, and it is stated as one.** A real Modbus TCP or MQTT stack retransmits a lost segment, after a retransmission timeout or three duplicate acknowledgements. So the message arrives late rather than not at all, and the connection's window shrinks. **The ns-3 backend shows that behaviour.** There Modbus and MQTT run over ns-3's TCP (CUBIC, minimum RTO 1 s, fast retransmit), and a dropped segment is retransmitted. The engines therefore disagree by design on these protocols under loss: SimPy and Rust count a lost poll, while ns-3 delivers it late and shows the delay in `modbus_latency`. That difference is measured (below), not hidden.

### Metrics

| Metric | On | Value at a sample | Unit |
|---|---|---|---|
| `drops` | switch | frames its egress buffers dropped in the window. Sampled every time, 0 included. | frames |
| `drops_total` | switch | frames its egress buffers dropped so far | frames |
| `frames_sent` | adapter | frames whose transmission on the adapter's link has completed so far, as `bytes_sent` | frames |
| `frames_received` | adapter | frames the adapter has accepted so far, as `bytes_received` | frames |
| `modbus_unanswered` | adapter | Modbus requests the adapter has generated so far, less the responses it has accepted so far: lost polls plus polls in flight | polls |

- **Loss of a flow.** For a flow `a → b`, when `a` sends nothing else: lost = `frames_sent:a` − `frames_received:b`, less the frames in flight at the sample. That is at most the frames buffered on the path. Over a long run the loss ratio is `1 − frames_received:b / frames_sent:a`.
- **Lost polls.** `modbus_unanswered` includes polls still in flight. That is at most `⌈max RTT / poll period⌉` per poll statement, and with bounded buffers the RTT is bounded. It is a count, not a ratio: the requests generated are `rate × t`.
- **All counts are integers** and are reported as they are, like `pn_watchdog_expiries`.
- **Why not count losses per destination at the switch?** A flooded frame's copy that is dropped on a port leading away from its destination is not a loss to anyone, and the switch cannot tell which copy that is. Counts at the end stations are exact.

### The IR stays version 2

- **New field:** `SwitchNode.buffer_bytes`, optional.
- **No new version.** By ADR-0007's rule, a new optional field does not change the version, and only a change in meaning does. A switch without the field means what it always meant.
- **Nothing is run without it.** Every reader rejects fields it does not know. An engine that predates this ADR therefore rejects a document with a bounded switch instead of running it as lossless. Before that, `check()` rejects the scenario for the missing capability (below).
- **Written only when set.** `buffer_bytes` is left out of the JSON while it is `None`, so a document without bounded switches is byte for byte what it was before, and older readers still read it.
- **The Rust package.** `smolpy[rust]` requires the `smolpy-engine-rs` release that runs bounded switches.

### Capabilities

A new capability feature, `"bounded_buffer"`. `smolpy.backends.check` rejects a scenario with a bounded switch on a backend without it, naming the switch, before the run. A backend never ignores `buffer` silently. The new metrics are capabilities as usual. All three backends get the feature in this change.

### ns-3

The ns-3 backend does not use ns-3 queues for switches. Links and switches are SMOL's own (`SmolDevice`, `SmolSwitch` in `smol-ns3.cc`). So:
- **The limit** is implemented in `SmolDevice::Enqueue`, with a byte count of the queued frames and a drop counter. The `switch` line carries the limit: `switch <buffer bytes>`, 0 for unbounded.
- **The idle-port rule above.** `SmolDevice` today starts the first frame queued on an idle port at once, while SimPy and Rust take it after the events already scheduled for that instant. That difference was not observable so far, apart from a same-instant priority order no test covered. It is now, so `SmolDevice` defers the start with `Simulator::ScheduleNow`, which ns-3 orders FIFO among same-instant events, as SimPy does.
- **Format.** The line format becomes `smol-ns3 2`, and the observe kinds `drops`, `frames_sent`, `frames_received` and `modbus_unanswered` are added.
- **Expected agreement:**
    - Ethernet over UDP and PROFINET RT stay bit-exact with SimPy, drops included, stochastic scenarios with the same seed included;
    - Modbus and MQTT differ by TCP, as now, and now also by its retransmissions.

### Validation and tests

- **Unit tests:**
    - validation of `buffer`;
    - no drops while the limit is never reached;
    - a drop exactly when `q + s > buffer`, for each class and for flooded copies;
    - same-instant arrivals at a full queue;
    - delay within the bound above;
    - `drops_total` = `frames_sent` − `frames_received` − in flight, for one flow.
- **Analytical test.** One egress port with Poisson arrivals of fixed-size frames is an M/D/1/K queue:
    - service time `D = tx(8 + s) + tx(12)`;
    - `K = 1 + ⌊buffer / s⌋`: the frame in transmission and the waiting room.

  Its blocking probability and mean waiting time come from the embedded Markov chain at departures. They are compared with `drops_total` / `frames_sent` and with the mean latency, over several seeds, with a tolerance from the spread across seeds. The model differs from M/D/1/K only for the first frame of a busy period, whose remaining gap is shorter than `tx(12)`. The test uses 9 000 B frames, where that is under 0.14 % of `D`.
- **Cross-engine:**
    - deterministic scenarios with drops, same-instant arrivals at a full queue among them, go into the bit-exact set of SimPy and Rust, event counts included;
    - the same Ethernet and PROFINET scenarios are compared exactly on ns-3.
- **The testbed scenario.** Poisson UDP of 1 518 B frames and Modbus polls every 10 ms share a 100 Mb/s port with `buffer=115000`, at ρ from 0.9 to 1.05. It runs on all three engines. The report covers the Modbus RTT (mean, median, p99, maximum), lost polls on SimPy and Rust against late polls on ns-3, and the UDP loss ratio, set against the testbed: 9.8 ms and 0.09 % at ρ = 0.996.

### Steps

1. **The model.**
    - the optional IR field, written only when set;
    - `buffer` in the Python API and in `.smol`, with its suffixes;
    - validation;
    - the capability feature, which no backend has yet, so every backend rejects a bounded switch.
2. **SimPy:**
    - the limit, the drops and the five metrics;
    - unit, semantics and M/D/1/K tests;
    - Model Semantics updated.
3. **Rust:**
    - the same;
    - the bit-exact scenarios with drops;
    - the `smolpy-engine-rs` floor raised.
4. **ns-3:**
    - format 2, the limit and the counters, the deferred start on an idle port;
    - the exact comparisons;
    - the testbed scenario on all three engines, with its results recorded.
5. **Documentation:**
    - language reference, DSL reference, backends;
    - an example scenario: a 100 Mb/s bottleneck with `buffer=115000` near ρ = 1;
    - the roadmap item moved to done.

---

## Consequences

**Positive**
- **The model can be used at and above saturation.** Delay is capped by the buffer, and loss appears where it does on real switches.
- **The testbed's knee is reproducible:** capped RTT and the onset of loss at ρ ≈ 1.
- **All three engines implement the same rule exactly,** so SimPy and Rust stay bit-exact and ns-3 stays bit-exact on Ethernet and PROFINET.
- **ns-3's TCP can now be studied under loss,** with retransmissions, which the previous load study could not show.

**Negative / trade-offs**
- **A new `smolpy-engine-rs` release.** `smolpy[rust]` needs the engine that runs bounded switches.
- **On SimPy and Rust, Modbus and MQTT simply lose messages,** where a real stack retransmits them. Results for these protocols under loss are a lower bound on delivery and say nothing about retransmission delay. ns-3 is the backend for that.
- **One limit per port, shared by all priorities.** Switches that reserve buffer for high priorities, or share a pool across ports, are not modelled yet.
- **The idle-port rule is now normative.** It was an implementation detail of SimPy, and ns-3 is changed to follow it.
