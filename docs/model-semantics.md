# Model Semantics

This is the normative meaning of a [Scenario](scenario-model.md), independent of any engine ([ADR-0007](adr/0007-execution-independent-scenario-model.md)). A second engine, such as the Rust engine of ADR-0008, is correct when it produces what this page describes. The SimPy engine is the **reference implementation**. Where it and this page disagree, that is a bug in one of them, and it is fixed explicitly. Known disagreements are listed under [Open issues](#open-issues).

`tests/test_semantics.py` checks the statements on this page against the SimPy engine. Every section names the tests that cover it.

## 1. Time and units

The scenario keeps the units users give. Engines convert them as follows:

| Quantity | Scenario unit | Meaning in simulated time |
|---|---|---|
| durations, delays, cycles, `every` | ms | × 1 000 µs |
| link speed | Mb/s | × 10⁶ bit/s |
| link length | m | propagation delay = length / (2 · 10⁸ m/s) |
| traffic rate | frames, messages or polls per second | mean interval = 1 / rate s |
| sizes, payloads | bytes | × 8 bits on the wire |

- **Simulated time starts at 0 and the run covers `[0, duration)`.** Nothing scheduled at exactly `t = duration` happens, neither a frame event nor a metric sample.
- **Each engine chooses its own representation of time.** SimPy uses float µs, and the Rust engine is planned to use integer ns. Results may differ only by that rounding (see [§9](#9-simultaneous-events)).
- **Metric sample times are reported in ms.**

*Tests: `test_run_covers_half_open_interval`, `test_samples_fall_on_multiples_of_every`.*

## 2. Frames

A frame carries its source and destination MAC, its size in bytes, an **origin time**, and a priority (802.1Q PCP 0–7).

**Wire sizes exclude the FCS, the preamble and the inter-frame gap.** Transmission time is `size × 8 / speed`. Real Ethernet adds 24 B per frame on the wire, so the model's link capacity is slightly higher than a real link's (see [Open issues](#open-issues)).

| Frame | Size (B) | Priority |
|---|---|---|
| `Flow` | `size`, or IMIX per frame: 64 B with p = 0.40, 594 B with p = 0.57, 1 518 B with p = 0.03 | `priority` |
| MQTT PUBLISH | 54 + 2 + 2 + len(topic) + (2 if qos > 0) + payload | 0 |
| MQTT PUBLISH forwarded by the broker | same size as the PUBLISH received | 0 |
| MQTT PUBACK | 58 | 0 |
| Modbus request (FC 03) | 65 | 0 |
| Modbus response | 62 + 2 × count | 0 |
| PROFINET RT cyclic frame | max(60, 24 + io_bytes) | 6 |

- **54 B is the Ethernet, IPv4 and TCP headers** (14 + 20 + 20).
- **Padding:** only PROFINET frames are padded to the 60 B Ethernet minimum, and only they include an 802.1Q tag. A `Flow` with a `priority` does not grow by 4 B.
- **Formulas:** all are implemented in `smolpy.model.frames`.

**The origin time** is the instant the data was created:
- A frame a station generates gets the instant it is generated.
- A frame forwarded by an MQTT broker keeps the origin time of the PUBLISH it copies.
- A Modbus response keeps the origin time of its request.
- A PUBACK gets the instant the broker sends it.

*Tests: `test_frame_sizes`, existing frame-size tests in `test_simulation.py` and `test_profinet.py`.*

## 3. Links and egress queues

- **A link is full duplex.** Each direction is an independent channel with its own egress queue and transmitter at the sending node. Half-duplex links are valid in the model but not supported by the SimPy backend, which rejects them.
- **Transmitter:**
    - it takes the next frame from its queue;
    - it holds the wire for `size × 8 / speed`;
    - the frame then needs the propagation delay to arrive.
- **Propagation does not occupy the wire.** The next transmission starts as soon as the previous one's last bit has left.
- **Arrival time.** A frame that starts transmission at `t` arrives at the far end at `t + tx + prop`.
- **Each egress queue has 8 strict priority classes:**
    - a higher class is always served first;
    - frames within a class are served first in, first out;
    - service is non-preemptive: a frame in transmission is never interrupted.
- **`qos="fifo"`** on a switch puts every frame into one class on that switch's egress queues. All other nodes, including end stations, use the 8 classes.
- **Queues are unbounded,** so no frame is ever dropped for lack of buffer.

*Tests: `test_single_hop_latency_is_tx_plus_prop_per_link`, `test_propagation_does_not_hold_the_link`, existing priority tests in `test_profinet.py`.*

## 4. Switches

**Switches** are store-and-forward with zero processing time:
1. A frame is forwarded at the instant it has fully arrived.
2. The switch learns the frame's source MAC on its ingress port.
3. It looks up the destination MAC:
    - unknown destination: a copy goes to every other port (flooding);
    - known destination on the ingress port: the frame is dropped (hairpin);
    - otherwise: the frame is queued on the destination's port.

**The initial MAC table** depends on `mac_table`:
- **`learning`:** the table starts with the end stations linked directly to the switch.
- **`topology`:** the table additionally holds the port towards every reachable end station.
    - The port is found by breadth-first search through switches. End stations are not traversed.
    - The first port to reach a station wins, which means the shortest path, with ties going to the lowest port index.
    - Learning continues on top of this table.

**Ports** are numbered per node from 0, in the order the node's links appear in the scenario.

**There are no hubs.** A shared-medium segment (half-duplex, CSMA/CD) is not modelled. A scenario with a `hub` node or a `collision_rate` observation is rejected (open issue 3).

*Tests: `test_cut_through_behaves_as_store_and_forward`, existing MAC tests in `test_simulation.py` (`TestNicFiltering`, `TestTopologyMacTables`).*

## 5. End stations

**NIC filtering.** Adapters, MQTT brokers, Modbus slaves and PROFINET devices accept only frames addressed to their own MAC. All other frames, flooded by a switch, are dropped without being counted.

**Each kind of end station handles accepted frames with zero processing time:**

| Station | On an accepted frame |
|---|---|
| Adapter | counts it for its metrics (any frame kind); PROFINET frames also go to its PROFINET consumer |
| MQTT broker | for a PUBLISH: one copy to every subscriber of its topic, in subscriber order, then a PUBACK to the publisher if `qos = 1`. A topic without a route is not forwarded. Other frames are ignored. |
| Modbus slave | for a request with its `unit_id`: one response to the requester |
| PROFINET device | passes cyclic frames of its ARs to its PROFINET consumer |

An end station sends everything over its single link, in the egress queue order of [§3](#3-links-and-egress-queues).

## 6. Traffic sources

- **Start time.** Every source starts at `t = delay_ms`.
- **Interval.** `T = 1 / rate` is the mean interval.
- **Random draws.** Random patterns and IMIX sizes draw from one generator seeded with `run.seed`, in the order the simulation needs the numbers (see [Open issues](#open-issues)).

| Source | Frames |
|---|---|
| `Flow`, `constant` | one frame at `delay + T`, `delay + 2T`, … |
| `Flow`, `poisson` | one frame after each exponential gap with mean `T`, the first gap starting at `delay` |
| `Flow`, `batch` | groups at Poisson instants with mean gap `T · E[X]`. Each group is X frames queued at the same instant, X drawn per group from the batch distribution. The long-run rate is `rate`. |
| `Flow`, `bursty` | repeated ON/OFF cycles: L = max(1, ⌊Pareto(1.5)⌋) frames spaced `T/4` (the first one `T/4` after the cycle starts), then a silence of `T · L · U(0.5, 2)`. The long-run rate is `rate / 1.5`, see [Open issues](#open-issues). |
| `Publish` | one PUBLISH at `delay + T`, `delay + 2T`, … |
| `Poll` | one request at `delay + T`, `delay + 2T`, … |
| `ProfinetAR` | the controller sends an output frame, and the device an input frame, at `delay`, `delay + cycle`, `delay + 2·cycle`, … Note that the first frame goes out at `delay`, not one cycle later. |

*Tests: `test_constant_flow_first_frame_after_one_interval`, `test_profinet_first_frame_at_delay`, `test_bursty_long_run_rate`.*

## 7. PROFINET consumer

- **Exchanges per AR.** Each AR has two data directions: outputs to the device, inputs to the controller. Each receiving side supervises its own direction.
- **Latency** of a cyclic frame is its arrival time minus its origin time.
- **Jitter** is `|Δ − cycle|`, where Δ is the time since the previous frame of the same AR and direction. It is defined from the second frame on.
- **Watchdog:**
    - it starts with the first frame of a direction;
    - it expires when no frame arrives within `hold × cycle` of the previous one;
    - each gap counts as at most one expiry, and supervision restarts with the next frame;
    - the exchange is not aborted (ADR-0005).

## 8. Metrics

**Sampling:**
- Each observation samples at `t = k · every`, for k = 1, 2, … and every `t < duration`.
- A **window** is the interval since the previous sample.
- **Window metrics** take no sample when their window is empty.
- All other metrics take a sample every time.

| Metric | Value at a sample | Unit |
|---|---|---|
| `throughput` | bytes the adapter accepted in the window × 8 / window length | Mb/s |
| `latency` | mean of (arrival − origin time) over frames the adapter accepted in the window, of any kind. *Window metric.* | µs |
| `bytes_received` | total bytes the adapter accepted so far / 10⁶ | MB |
| `bytes_sent` | total bytes whose transmission on the adapter's link has completed so far / 10⁶ | MB |
| `modbus_latency` | mean of (arrival − origin time) over Modbus responses the adapter accepted in the window: the round-trip time from generating the request. *Window metric.* | µs |
| `queue_depth` | frames waiting in all egress queues of the switch, not counting frames being transmitted | frames |
| `utilization` | bits whose transmission completed on the links *into* the node in the window, as a share of those links' combined capacity over the window | % |
| `broker_queue` | frames delivered to the broker and not yet processed. The broker has no service time, so this is non-zero only for frames arriving at the sample instant itself. | frames |
| `pn_latency` | mean PROFINET latency over cyclic frames received in the window. *Window metric.* | µs |
| `pn_jitter` | maximum jitter over the window. *Window metric.* | µs |
| `pn_watchdog_expiries` | total expiries so far, over all ARs of the node | count |

A metric's result key is `"<metric>:<node>"`. Its samples are `(time_ms, value)` pairs in time order.

*Tests: `test_metric_definitions`, `test_window_metrics_skip_empty_windows`, `test_modbus_latency_is_round_trip`, `test_mqtt_latency_is_end_to_end`, `test_watchdog_expiries_are_cumulative`.*

## 9. Simultaneous events

**Events at the same simulated instant are processed in the order they were scheduled** (first in, first out). Two cases are observable:

- **A frame arriving at exactly a sample instant** belongs to the window that ends there if its arrival was scheduled before the sample, and to the next window otherwise.
- **Frames queued at the same instant on one egress queue** are served by priority, then in the order they were queued:
    - a batch group in draw order;
    - broker copies in subscriber order, then the PUBACK.

**Start-up order** decides the scheduling order of the first events:
- link transmitters are started first, in link order, a then b direction;
- node processes are started in scenario node order;
- each adapter starts its sources in this order: PROFINET ARs, flows, publishes, polls;
- metric samplers start after all nodes, in observation order.

**Effect on cross-engine equivalence.** An engine with a different time representation can break a tie differently when two events coincide in one representation but not in the other. In deterministic scenarios that only moves a frame across a window boundary. The equivalence check of ADR-0007 allows for this.

## Open issues

Every disagreement between this page and the intended model is listed here, with its resolution once decided.

| # | Issue | Current behaviour (reference engine) | Status |
|---|---|---|---|
| 1 | `bursty` does not keep `rate` as its long-run average | Mean ON/OFF cycle per frame is `1.5 T`, so the long-run rate is `rate / 1.5`. All other patterns keep `rate`. | to be fixed in ADR-0007 step 5 |
| 2 | `mode="cut-through"` is not modelled | Accepted and ignored. Every switch is store-and-forward. | accepted: ignored until needed |
| 3 | Hubs were not a shared medium | They had per-port queues with strict priority, and collisions were approximated as simultaneous arrivals, so `collision_rate` almost never counted any. | resolved: hubs and `collision_rate` removed. A shared segment would come back as a new node kind with half-duplex links and CSMA/CD. |
| 4 | Wire overhead is not counted | Preamble, SFD, FCS and inter-frame gap (24 B per frame) are not on the wire, so the model's link capacity is higher than real by 24 B per frame: about 2 % for 1 518 B frames and 40 % for 60 B frames. | known simplification |
| 5 | One random stream for all sources | Adding or reordering one source changes the draws of all others. | to be fixed in ADR-0007 step 5 |
