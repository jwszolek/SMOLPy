# ADR-0013: Switch Buffers Accounted per Ingress Port, and Limits in Frames

**Status:** Accepted  
**Date:** 2026-11-24  
**Amends:**
- ADR-0011: a switch buffer can also be accounted per ingress port and counted in frames. The rationale "Unit: bytes" is revised by the measurements below.
- ADR-0007: four new optional IR fields, and flows between any two end stations. The IR stays version 2.
- ADR-0004: the `smol-ns3` line format becomes version 4.
- [Model Semantics](../model-semantics.md) §3, §4, §5, §6, §8, §9 and the open issues.

**Roadmap:** ADR-0011's possible extensions "a limit in frames, for switches that count descriptors" and "Ingress buffering is not modelled".

---

## Context

> *Note, 2026-11-24:* the unit is refuted by [ADR-0014](0014-buffer-cells-and-hysteresis.md). Burst tests with 512 B and 64 B frames show that the testbed switch counts 128 B cells, not frames, and its loss runs show a resume threshold. The scope of this ADR, accounting per ingress port, is confirmed. The text below is as it was decided.
>
> *Note, 2026-11-24:* [ADR-0015](0015-shared-pool-and-drain-resume.md) adds the shared pool this ADR names as its next extension: the ingress accounts of a switch can share one pool, beyond a reserve each.

### The testbed, measured more widely

The testbed of ADR-0011 is the following:
- **Switch:** a TP-Link LS108GP, an unmanaged 8 × 1 Gb/s store-and-forward switch.
- **Traffic:** a Raspberry Pi on a 1 Gb/s port sends Poisson UDP traffic of 1 518 B frames to a laptop whose port runs at 100 Mb/s. 802.3x PAUSE is disabled on the Pi.
- **Polling:** the laptop polls a Modbus TCP server on the same Pi every 10 ms, with one request in flight (ADR-0012).
- **A second server:** a Waveshare Modbus TCP relay module sits on another 1 Gb/s port.

A campaign of 214 runs on SMOLPy 1.11 compared the hardware with SimPy, Rust and ns-3 across 7 families of scenarios. The model used `buffer=91080`, which is 60 frames of 1 518 B, taken from a burst test that measured about 60–62 frames.

**Where a single source congests the port, the model matches the hardware:**

| ρ of the 100 Mb/s port | Mean queueing wait, hardware | Mean queueing wait, ns-3 |
|---|---|---|
| 0.96 | 1 368 µs | 1 406 µs |
| 0.984 | 2 520 µs | 2 518 µs |
| 0.997 | 3 469 µs | 3 328 µs |

At ρ = 0.997:
- **UDP loss:** 0.66 % on the hardware, 0.73 % on ns-3.
- **Modbus responses retransmitted by TCP**, with an RTT of about 215 ms: 0.42 % of polls on the hardware, 0.52 % on ns-3.

### Two observations a shared egress buffer cannot explain

**1. The limit is per ingress port.** The test ran 8 100 frames/s of UDP from the Pi for 310 s, giving 0.66 % UDP loss. Meanwhile the laptop polled the Pi's server every 10 ms and the Waveshare every 20 ms. Both responses are frames of under 100 B, and both queue on the same congested egress port towards the laptop.

| Server polled | Responses lost and retransmitted |
|---|---|
| Pi | 133 of 30 000 (0.44 %) |
| Waveshare | 0 of 15 000 |

- **With one shared egress buffer,** the Waveshare would have lost about 66.
- **An earlier run at 8 000 frames/s agrees:** 0 lost of 5 600, where ns-3 predicted about 11.

So the switch drops frames from the port that causes the congestion, and not frames from another ingress port to the same egress port. That is what per-ingress-port buffer limits do, and they are typical of Realtek switch chips.

**2. The limit counts frames, not bytes.** The Pi's small Modbus responses are dropped almost as often as its 1 518 B UDP frames: 0.44 % against 0.66 %.
- **With a limit in bytes,** a 75 B frame nearly always fits in the space left beside full-size frames. ns-3 with `buffer=90000` showed exactly that: almost no response was lost.
- **SMOLPy reproduces the hardware only by coincidence:** 91 080 B is an exact multiple of 1 518 B, so a full queue has no space left. That breaks as soon as frame sizes are mixed.

### Not known yet

Whether the LS108GP also has a per-egress limit, or a shared pool, is not known. A testbed run with two Pis sending to the laptop at once will tell:
- if the delay cap stays near 9.8 ms, there is also an egress limit;
- if it doubles, there is not.

The model must express all of these cases, so that the measurement only picks the parameters.

### A host that is both a source and a server

The testbed's Pi is one physical port that carries both the UDP stream and the Modbus responses. SMOL cannot express that today: `flow pi -> laptop ...` from a `modbus_slave` fails with *"'pi' is a ModbusSlave, expected Adapter"*. The ADR-0011 and ADR-0012 testbed models therefore use two nodes on two links. With ingress accounting, that would put the responses on their own ingress port, which is exactly the wrong answer.

### Options considered

- **A: a shared egress buffer only** (ADR-0011, as now). It cannot explain observation 1 at any size.
- **B: per-ingress-port and per-egress-port limits, each optional, each in bytes or frames.** **Chosen.** It expresses every case the pending measurement can show, apart from a shared pool, and every engine can reproduce it exactly.
- **C: a shared pool with dynamic thresholds** (Broadcom-style). The sharing policy is vendor-specific, and nothing measured needs it yet. It is the next possible extension (Scope, below).
- **D: limits in cells,** e.g. 128 B pages. These are a common chip internal, between bytes and frames. A 75 B and a 1 518 B frame would take 1 and 12 cells, which observation 2 contradicts for this switch. So they are not modelled now.

---

## Decision

### The parameters

Each switch takes two optional limits, each applying to each of its ports:
- **`buffer`:** the egress limit of ADR-0011;
- **`ingress_buffer`:** the new per-ingress-port limit.

A limit is a number of **bytes**, as in ADR-0011, or of **frames**:

```
switch sw ports=8 ingress_buffer=60frames
switch sw ports=8 buffer=115kB ingress_buffer=60frames
switch sw ports=8 buffer=40frames
```

```python
from smolpy import Frames

sw = net.switch("sw", ports=8, ingress_buffer=Frames(60))
sw = net.switch("sw", ports=8, buffer=115_000, ingress_buffer=Frames(60))
```

- **Spelling in `.smol`.** `<n>frames` is a limit in frames; `1frame` is accepted too. Without the suffix, or with `kB`, `MB`, `KiB` or `MiB`, the limit is in bytes, as now.
- **Spelling in Python.** An `int` is bytes, as now, and `Frames(n)` is frames. The unit belongs to the value, so the Python API and `.smol` have one keyword per scope each, and a scope cannot be given twice.
- **One unit per scope.** A scope's limit is in bytes or in frames, not both at once. A switch whose memory runs out in bytes and descriptors in frames would need both, but nothing measured needs it, and it would double the cases to test. The scopes can use different units: e.g. ingress in frames and egress in bytes.
- **Default: unbounded,** as now. A scenario without `ingress_buffer` and without a frame limit means exactly what it meant before. It gives the same results, event counts and benchmark hashes (B1–B4), and the same equivalence suite.
- **IR.** Four optional fields of `SwitchNode`, each written only when set, and at most one per scope:
    - `buffer_bytes` (ADR-0011) and `buffer_frames`;
    - `ingress_buffer_bytes` and `ingress_buffer_frames`.
- **Validation:**
    - a limit is a positive integer, not a bool;
    - a byte limit, of either scope, is at least one maximum frame (1 522 B) and at least the largest frame of the scenario, as in ADR-0011;
    - a frame limit is at least 1;
    - both units in one scope are an error in the IR.

### What an account holds

A switch has one **egress account** and one **ingress account** per port. Each counts frames that are **queued**: in an egress queue of the switch, and not yet taken by its transmitter. That is the frames `queue_depth` counts, so `queue_depth` and both limits share one notion of "queued".
- **Egress account of port `p`:** the frames queued on `p`'s egress queue, as in ADR-0011.
- **Ingress account of port `p`:** the frames that entered the switch on `p` and are still queued on any of its egress ports.
- **The frame in transmission is not counted** in either account. That is ADR-0011's rule, now applied to both.
- **In bytes,** a frame counts its frame bytes (Model Semantics §2). **In frames,** a frame of any size counts 1.
- **Priorities.** Each account is shared by all 8 classes, as in ADR-0011. A frame is admitted or dropped whatever its priority.
- **Ports between switches.** The ingress port of a frame arriving from another switch is that inter-switch port. The upstream switch releases its own accounts for the frame when its transmitter takes the frame onto the inter-switch link. The downstream switch then charges its own ingress port.
- **End stations stay unbounded,** as in ADR-0011.

### Admission

A switch forwards a frame at the instant it has fully arrived (Model Semantics §4). If the frame is not a hairpin, it is admitted in this order:
1. **Ingress check.** If the ingress port's account has no room for the frame, the frame is dropped, as one **ingress drop**. No egress account is looked at, and nothing else changes.
2. **Egress check, for each copy.** A unicast frame is one copy, and a flooded frame has a copy for every other port, in port order. Each copy is queued if its egress account has room, and is otherwise dropped, as one **egress drop**, exactly as in ADR-0011.
3. **Charge.** If at least one copy was queued, the ingress account is charged once, for the frame. If none was, nothing is charged.

The order matters only for counting. A frame for which both accounts are full is counted once, as an ingress drop. It does not change which frames are queued, because a frame is queued only if every account that applies has room.

**"Room" is the tail-drop rule of ADR-0011**, in the scope's unit:
- in bytes: `q + s ≤ limit`, with `q` the bytes the account holds and `s` the frame's;
- in frames: `n + 1 ≤ limit`, with `n` the frames the account holds.

A dropped frame or copy is never queued, displaces nothing and schedules nothing.

### Release

**When an egress transmitter takes a copy, it leaves its egress account** at that instant, as in ADR-0011. The rules of when a transmitter takes a frame are unchanged:
- at once when it has sent the previous frame's FCS;
- when idle, after the events already scheduled for that instant (Model Semantics §9).

**When the last queued copy of a frame is taken, the frame leaves its ingress account,** at the same instant. A unicast frame has one copy, so both accounts are released together.

No event is added, so the event graph of a scenario without the new limits is unchanged. With them, every engine decides every drop from the same state, at the same point of the same event.

### Flooding

A flooded frame is stored once in a real switch, with several copies queued that point to it. So:
- **ingress:** it counts once on its ingress port, from its admission until its last queued copy is taken;
- **egress:** each copy counts on its own egress port, as in ADR-0011.

The alternative, counting each copy on the ingress port, would let one flooded frame take up to `ports − 1` slots of its ingress account. Under learning, one broadcast-like frame would then drop the next frames of a busy port, which a switch that stores the frame once does not do.

### Identity with the egress limit

When every frame that enters a port is queued on one egress port, and that egress port carries only frames from that ingress port, both accounts hold the same frames at every instant. Then `ingress_buffer=L` gives exactly the same run as `buffer=L`, in either unit:
- every metric and the event count are the same;
- only the split of drops into ingress and egress differs.

That holds for one flow through a switch, for example, and is a test (below).

### Hosts that are a source and a server

**Decided: flows between any two end stations,** as a separate change, done first (step 2).
- **What changes.** A `flow` may start at, and go to, any end station: an adapter, a Modbus slave, an MQTT broker or a PROFINET device. So the testbed's Pi is one `modbus_slave` node that also runs `flow pi -> laptop ...`.
- **What the station does with it:**
    - a station's flows share its single link and its egress queue with everything else it sends;
    - a station that receives a flow's frames accepts them at its NIC and, apart from an adapter's counters, ignores them, as it ignores every frame not meant for it (Model Semantics §5).
- **Start-up order.** Each end station starts its own process first, then its flows, in statement order:
    - its own process is the receiver, responder, forwarder or PROFINET receiver;
    - an adapter keeps its order: PROFINET ARs, flows, publishes, polls.
- **Source ids** are unchanged: `flow:<from>-><to>#k`.
- **Metrics on stations.**
    - **`frames_sent` and `bytes_sent`** become observable on every end station. They are counters of its link, and the testbed needs the Pi's.
    - **The Modbus metrics on a slave.** `modbus_latency`, `modbus_unanswered`, `modbus_timeouts` and `modbus_skipped` become observable on a Modbus slave, over the polls of that slave, from every master. They are measured at the masters, with the rules of ADR-0012, and summed per slave. The testbed needs the Pi's and the Waveshare's polls separately, from one master.
- **The other engines.**
    - **Rust** starts the flows of every end station, not only an adapter's.
    - **ns-3** already lowers every end station as an IP host, so a flow from a slave is a UDP application on that host.
- **Capabilities.** An engine that does not have the change would silently not run a slave's flows. So two features are added: `"station_flows"` (flows from and to any end station, and `frames_sent` and `bytes_sent` on any end station) and `"slave_metrics"` (the Modbus metrics on a slave).

**The alternative is not chosen.** It would model the Pi as two nodes, aggregated onto one link by an unbounded 10 Gb/s switch. Then the responses and the UDP stream would share one ingress port of the LS108GP, as on the hardware. It costs a store-and-forward hop of about 1.2 µs per 1 518 B frame, a second switch's events, and a topology that is not the testbed's. The change above costs little in every engine.

### Metrics

| Metric | On | Value at a sample | Unit |
|---|---|---|---|
| `drops` | switch | frames and copies the switch dropped for lack of buffer in the window: ingress and egress drops. Sampled every time, 0 included. | frames |
| `drops_total` | switch | the same, so far | frames |
| `ingress_drops_total` | switch | ingress drops so far | frames |
| `egress_drops_total` | switch | egress drops so far | frames |
| `frames_sent`, `bytes_sent` | any end station | as for an adapter (ADR-0011): frames, and their bytes, whose transmission on its link has completed | frames, MB |
| `modbus_latency`, `modbus_unanswered`, `modbus_timeouts`, `modbus_skipped` | Modbus slave | as on a master (ADR-0011, ADR-0012), over the polls of this slave, from every master | µs, polls |

- **`drops` and `drops_total` keep their meaning:** everything the switch dropped for lack of buffer. Without ingress limits they are the egress drops, as before.
- **Per-port metrics are left open.** `observe <metric> on <node>` addresses a node, not a port, so the occupancy and drops of one port cannot be observed. A port address, e.g. `on sw:2`, would need a grammar, IR and lowering change of its own. It is on the roadmap.
- **The loss of a station's frames.** With ingress limits only, the frames dropped from a station's port are that port's ingress drops. When only one port is ever full, that is `drops_total`.

### Capabilities

New features, so that `backends.check` rejects a scenario an engine cannot run instead of ignoring a limit:

| Feature | Rejects without it |
|---|---|
| `"ingress_buffer"` | a switch with `ingress_buffer` |
| `"buffer_frames"` | a limit in frames, of either scope |
| `"station_flows"` | a flow from or to an end station other than an adapter; `frames_sent` or `bytes_sent` on one |
| `"slave_metrics"` | a Modbus metric observed on a Modbus slave |

The metrics `ingress_drops_total` and `egress_drops_total` are capabilities as usual. All three backends get every feature in this change.

### The IR stays version 2

- **New fields:** `SwitchNode.buffer_frames`, `ingress_buffer_bytes` and `ingress_buffer_frames`, all optional, and written only when set.
- **Flows between any end stations** add no field. An older reader would accept a slave's flow and not run it, so the Rust module's capabilities gate it (above).
- **Nothing is run without the change.** Every reader rejects fields it does not know, and `check()` rejects the scenario first, for the missing capability.

### ns-3

The switch is SMOL's own (`SmolDevice`, `SmolSwitch`), not ns-3's queues:
- **The ingress accounts belong to the `SmolSwitch`,** shared by its devices.
    - `SmolSwitch::Receive` does the ingress check, then hands each copy to `SmolDevice::Enqueue`, which does the egress check.
    - It charges the ingress account if a copy was queued.
- **The release is in `SmolDevice::StartNext`,** where the egress account is released today. A frame carries its ingress port and the count of its copies still queued.
- **Format.** The line format becomes `smol-ns3 4`:
    - the `switch` line is `switch <egress limit> <egress unit> <ingress limit> <ingress unit>`, with the unit `b` or `f` and a limit of 0 for none;
    - the observe kinds `ingress_drops` and `egress_drops` are added;
    - Modbus kinds may target a slave.
- **Expected agreement:**
    - Ethernet over UDP and PROFINET RT stay bit-exact with SimPy, drops included, stochastic scenarios with the same seed included;
    - Modbus and MQTT differ by TCP, as in ADR-0011.

### Scope

- **In scope:**
    - per-ingress-port and per-egress-port limits, each optional, in bytes or frames;
    - flows between any end stations, with the station metrics above.
- **Possible later extensions, not part of this decision:**
    - **A shared pool with dynamic thresholds,** the next one. Each port could take up to `α × (pool − used)`, as many managed switches allow. It needs a measured `α` and pool, which the two-Pi run may give.
    - **Per-port metrics,** with a port address in `observe`.
    - **Limits in cells,** and both units in one scope.
    - **Limits per priority class,** as in ADR-0011.

### Validation and tests

- **Unit tests:**
    - **validation:** each unit and scope, the suffixes, both units in one scope, `Frames(0)`, and a byte limit below the largest frame;
    - **no drops below the limits;**
    - **a drop exactly when an ingress account is full,** in bytes and in frames;
    - **release when the transmitter takes the frame,** with a same-instant arrival that fits only because of it;
    - **flooding:** one charge on the ingress port; a frame whose copies are all dropped at egress charges nothing; the account is released only at the last copy;
    - **a frame limit drops a 64 B frame exactly like a 1 518 B one;**
    - **stations:** a flow from a slave, a broker and a device, to a non-adapter, the start-up order, the station metrics, and the Modbus metrics on a slave summing every master's polls of it.
- **Identity.** One flow through a switch, with `ingress_buffer=L` and with `buffer=L`, in both units and on all three engines:
    - every metric and the event count are the same;
    - the drops are ingress drops in one run and egress drops in the other.
- **The isolation case.** Two sources share one 100 Mb/s egress port: A overloads it and B sends a light Poisson stream.
    - **With `ingress_buffer` only,** B loses nothing.
    - **With `buffer` only,** B loses frames at A's rate, by PASTA, within the spread across seeds.
    - **With both, equal,** A's frames fill both its ingress account and the egress account. B's frames then pass the ingress check and fail the egress check, so B loses as with `buffer` only, now as egress drops, while A's further frames are ingress drops.
    - **With both, the ingress limit smaller,** A's frames take at most the ingress limit of the egress account. B loses nothing as long as its own queued frames fit in the difference.
- **Analytical.** One port with a frame limit `L` and one Poisson source of IMIX frames is an M/G/1/K queue with K = 1 + L, with one difference: an exceptional first service.
    - **Service time:** `S = tx(12) + tx(8 + s)`, with `s` drawn from IMIX. A frame taken at once after the previous frame's FCS also waits out that frame's gap.
    - **The first frame of a busy period** is taken when it arrives, `A` after the last FCS, so its service is `tx(8 + s) + max(0, tx(12) − A)`.
    - **The embedded chain at FCS instants:** the arrivals during a service, Poisson given `S`, give the departure distribution. Then the blocking probability is `1 − throughput / λ`. All of it is computed numerically.
    - It is compared with `drops_total / frames_sent` of the source, over several seeds, within a tolerance from the spread across seeds.
- **Cross-engine:**
    - **bit-exact set:** deterministic scenarios with ingress drops, a flooded frame at a full ingress account, and same-instant arrivals at a full ingress account join the bit-exact set of SimPy and Rust, event counts included;
    - **ns-3:** the same Ethernet and PROFINET scenarios are compared exactly on it;
    - **the existing suite** passes unchanged.

### The testbed scenario

`python -m benchmarks ingress` runs scenario T3, next to T1 (ADR-0011) and T2 (ADR-0012):
- **Topology:**
    - one `modbus_slave` Pi on a 1 Gb/s port, which sends Poisson UDP of 1 518 B frames to the laptop and serves the laptop's polls every 10 ms;
    - a Waveshare-like `modbus_slave` on another 1 Gb/s port, polled every 20 ms;
    - the laptop on a 100 Mb/s port;
    - both polls with `outstanding=1` and a timeout of 1 s.
- **The switch:** `ingress_buffer=60frames`, and beside it the old model, `buffer=91080`.
- **Runs:** ρ = 0.96, 0.984 and 0.997 of the laptop's port; ρ = 0.997 is 8 100 frames/s. Five seeds of 30 s each, on all three engines.
- **Reported:**
    - the polls of each server lost (SimPy, Rust) or retransmitted (ns-3, RTT above 100 ms), from the Modbus metrics on each slave;
    - the loss of the Pi's frames: `drops_total / frames_sent:pi`. The responses are 1.2 % of the Pi's frames, and with a limit in frames they are lost at nearly the UDP rate, so this is the UDP loss to within about 1 % of its value;
    - the quantiles of the Modbus RTT.
- **Against the hardware** at 8 100 frames/s: the Pi's server had 0.44 % of its polls retransmitted, the Waveshare 0, and UDP loss was 0.66 %.

*As built (2026-11-24):*
- **No new event, and nothing changes without the new limits.**
    - The benchmark hashes of B1–B4 are unchanged on SimPy and on Rust, event counts included.
    - Every earlier test passes, on all three engines. Four were adapted: two validation messages ("expected an end station"), two tests that hook SimPy's egress queue, and the `switch` line of format 4.
    - A switch without limits runs as before: its queues keep no accounts.
- **The Python spelling** is `Frames(n)`, exported as `smolpy.Frames`. In `.smol` it is `<n>frames`, or `1frame`.
- **Flows from any station** are one rule in each engine: each station starts its own process, then its flows.
    - Rust's PROFINET device used to hand every frame for it to its PROFINET consumer, because none but cyclic frames reached it. It now ignores a flow's frames, as SimPy's does.
- **The identity holds exactly.** One flow with `ingress_buffer=L` gives the run that `buffer=L` gives, every sample and the event count, in bytes and in frames, on all three engines.
- **The isolation case**, five seeds of 2 s, A at 1.1 of a 100 Mb/s port:

    | Limits, in frames | A's frames lost | B's frames lost |
    |---|---|---|
    | ingress 20 | 13.9 % | none (at most 1 on its way at the end) |
    | egress 20 | 13.3 % | 13.4 % |
    | ingress 20, egress 20 | 13.3 % | 13.4 %, the same run as egress only |
    | ingress 15, egress 20 | 14.0 % | none |

- **M/G/1/K with an exceptional first service**, IMIX through a frame limit, five seeds of 40 000 frames:

    | ρ, limit | Exact | Plain M/G/1/K | Simulated (standard error) |
    |---|---|---|---|
    | 0.9, 4 | 0.10201 | 0.10273 | 0.10264 (0.00082) |
    | 1.2, 2 | 0.30153 | 0.30320 | 0.30260 (0.00111) |

    The simulation agrees with the exact chain within about one standard error. The first-service correction, 0.7 % of the blocking here, is below what five seeds resolve.
- **Bit for bit across engines:**
    - `ingress-drops` (flooded frames at a full ingress account, ingress limits in frames and in bytes, egress limits in frames, PROFINET) and `ingress-tie` (arrivals at the instants a frame is taken) are bit-exact on SimPy, Rust and ns-3, event counts included on Rust;
    - `station-flows` (a slave's flow through an ingress limit, and the Modbus metrics of two slaves) is bit-exact on Rust. On ns-3, over TCP and with drops, its window metrics sample only at sampling instants, as for every lossy scenario of ADR-0011;
    - on ns-3, Poisson IMIX and bursty traffic through ingress and egress limits gives SimPy's run with the same seed.
- **The testbed** ([Execution backends](../backends.md#ingress-buffers), `benchmarks/results/2026-11-24-0836-…-ingress-buffers.json`):
    - **with `ingress_buffer=60frames`,** the Waveshare loses none of its polls, on every engine and at every load, as on the hardware. With `buffer=91080` it loses 0.54 to 0.58 % at ρ = 0.997;
    - **at ρ = 0.997, on ns-3,** 0.62 % of the Pi's polls are retransmitted, against 0.44 % on the hardware, and 0.74 % of the Pi's frames are dropped, against 0.66 % of its UDP frames;
    - **every lost response comes back after the RTO,** 210 to 217 ms, and one lost twice after 636 ms.
- **The `smolpy-engine-rs` floor.** It was `>=1.12.0` while no build of the engine with this ADR existed to lock; 1.12.0 refuses a scenario with these limits by its capabilities. Since 1.13.0, the release with this ADR, it is `>=1.13.0`.

### Steps

1. **The model:**
    - the three optional IR fields, written only when set;
    - `ingress_buffer` and `Frames` in the Python API, `<n>frames` in `.smol`;
    - validation;
    - the four capability features, which no backend has yet, so every backend rejects a scenario that uses them.
2. **Flows between any end stations,** on all three engines:
    - the station metrics and the Modbus metrics on a slave;
    - the features `"station_flows"` and `"slave_metrics"`;
    - their tests.
3. **SimPy:**
    - ingress accounts, frame limits, the admission and release rules, and the drop split;
    - unit, identity, isolation and M/G/1/K tests;
    - Model Semantics updated.
4. **Rust:**
    - the same;
    - the bit-exact scenarios;
    - the `smolpy-engine-rs` floor raised. A `smolpy-engine-rs` release is needed with the next `smolpy` release.
5. **ns-3:**
    - format 4, the ingress accounts in `SmolSwitch`, and the release in `SmolDevice`;
    - the exact comparisons.
6. **Documentation and the testbed:**
    - T3 on all three engines, with its results recorded;
    - the language and DSL references, `backends.md`, Model Semantics;
    - an example scenario;
    - the roadmap.

---

## Consequences

**Positive**
- **The model can express the testbed switch as measured:** the Pi's responses are lost at nearly its UDP rate, and the Waveshare's not at all.
- **It can express every outcome of the pending two-Pi measurement:**
    - an ingress limit alone;
    - an ingress and an egress limit;
    - an egress limit alone.
- **A host can be a traffic source and a server on one port,** as real PLCs, gateways and Raspberry Pis are.
- **All three engines implement the same rules exactly,** with no new event. SimPy and Rust stay bit-exact, and so does ns-3 on Ethernet and PROFINET.

**Negative / trade-offs**
- **A new `smolpy-engine-rs` release and ns-3 format version.**
- **More cases in the switch.** Two scopes, two units and flooding make admission the most detailed rule of the model. Each case is a test.
- **No shared pool,** and no per-port metrics yet.
- **The units are a modelling choice.** Frames fit this switch, but another switch may count bytes or cells. The scenario has to say which, and only a measurement can tell.
