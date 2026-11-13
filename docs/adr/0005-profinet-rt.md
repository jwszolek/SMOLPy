# ADR-0005: PROFINET RT Cyclic I/O

**Status:** Proposed  
**Date:** 2026-11-13

---

## Context

SMOLPy models two industrial communication patterns today: **request/response polling** (Modbus TCP, ADR-0002) and **publish/subscribe** (MQTT). Neither represents the pattern that dominates real-time control on industrial Ethernet: **cyclic producer–consumer exchange**, where controller and field device each send their process data every cycle, unprompted, and each side supervises the other's frames with a watchdog.

PROFINET IO is the most widely deployed protocol of this kind. Its real-time class **PROFINET RT** fits SMOLPy's existing model unusually well:

- RT cyclic frames are plain Layer-2 Ethernet frames (EtherType `0x8892`) — no IP, no TCP — so they travel through SMOLPy's existing switch and link model without any new transport layer.
- What matters for RT is exactly what SMOLPy measures: queuing and transmission delay in switches, and whether frames arrive on time every cycle.

Supporting it also exposes a gap in the engine. PROFINET RT relies on **802.1Q priority tagging** (RT frames carry priority 6) so that switches forward cyclic data ahead of ordinary traffic. Every SMOLPy egress port is currently a single FIFO queue, so prioritisation cannot be modelled at all — and without it, any PROFINET result under load would be misleading.

### PROFINET background (as modelled)

- **Roles.** An *IO Controller* (typically a PLC) and *IO Devices* (field devices, remote I/O). The controller establishes an *Application Relationship* (AR) with each device.
- **Cyclic data, both directions.** In each AR the controller provides **output** data to the device and the device provides **input** data to the controller. Each direction is sent independently every *update time*, not as a request and reply.
- **Update time.** `send clock × reduction ratio`. With the common 1 ms send clock, RT update times are 1, 2, 4, … 512 ms.
- **Watchdog.** A consumer that receives no new frame for `data hold factor × update time` declares the AR failed. The default data hold factor is 3.

### Options considered

**Controller role**
- **A — a dedicated `ProfinetController` node type.** Clean naming, but a PLC is usually also a Modbus master, an MQTT client, etc. — a separate node type would force users to model one physical device as several nodes.
- **B — controller as a capability of `Adapter`, device as a new node type.** Exactly the pattern of ADR-0001/0002 (Modbus master = `Adapter.polls()`, `ModbusSlave` node). **Chosen.**

**Priority queuing scope**
- **A — priority queues only for PROFINET frames.** Minimal, but prioritisation is a general switch feature and other traffic (e.g. deliberately mis-prioritised bulk flows) must be able to use it too.
- **B — general 802.1Q priority on every frame and every egress queue.** **Chosen.** Existing traffic defaults to priority 0, so behaviour of all existing scenarios is unchanged.

**Watchdog expiry behaviour**
- **A — stop the AR on expiry (as a real device does).** Realistic, but one expiry ends all measurement for that AR, and AR re-establishment (connect, parameterisation) takes far longer than the cycle and is not modelled.
- **B — count expiries and keep exchanging.** **Chosen.** Gives a usable robustness metric; the limitation is documented.

---

## Decision

### Package layout

Following ADR-0001, a new protocol package holds the infrastructure node; the controller role is an `Adapter` capability:

```
src/smolpy/
└── profinet/
    ├── __init__.py
    └── device.py        # ProfinetDevice node
```

### DSL API

```python
plc = net.adapter("plc", ip="10.0.0.1")
io1 = net.profinet_device("io1", ip="10.0.4.1")

net.link(plc, sw1, speed=100, length=5)
net.link(io1, sw1, speed=100, length=10)

# AR: 4 ms update time, 32 B input data, 16 B output data, watchdog = 3 × 4 ms
plc.controls(io1, cycle=4, inputs=32, outputs=16, hold=3)

# Optional priority on ordinary traffic (802.1Q PCP 0–7, default 0)
host.sends(to=server, rate=8_000, size=1_518, priority=0)

net.observe("pn_jitter", on=plc, every=100)
net.observe("pn_watchdog_expiries", on=plc, every=100)
```

### `ProfinetDevice` node (`profinet/device.py`)

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Node name |
| `ip` | `str` | IPv4 address (assigned to real devices; used here for MAC derivation, consistent with other nodes) |
| `mac` | `str` | MAC address (auto-derived if omitted) |

### `ProfinetSpec` (added to `core/adapter.py`)

Stored in `Adapter.profinet_specs: list[ProfinetSpec]`; one spec = one AR.

| Attribute | Type | Description |
|---|---|---|
| `device` | `ProfinetDevice` | Target device |
| `cycle_ms` | `int` | Update time; one of 1, 2, 4, … 512 |
| `input_bytes` | `int` | Input data per frame (device → controller), incl. IOPS/IOCS status bytes, 1–1440 |
| `output_bytes` | `int` | Output data per frame (controller → device), same rules |
| `hold` | `int` | Data hold factor, 1–255, default 3 |
| `delay_ms` | `float` | Simulation time before cyclic exchange starts |
| `input_frame_size` / `output_frame_size` | `int` | Computed — see below |

At most one AR per controller–device pair; a second `controls()` for the same pair raises `ValueError`.

### Frame size

Using SMOLPy's existing convention (header bytes counted, FCS / preamble / inter-frame gap not — matching the 54 B Ethernet/IP/TCP overhead used for Modbus and MQTT):

```
frame = Ethernet header (14) + 802.1Q tag (4) + FrameID (2) + IO data (n) + APDU status (4)
      = max(60, 24 + n)  bytes          # padded to the Ethernet minimum (64 B incl. FCS)
```

### Engine changes (`sim/engine.py`)

**802.1Q priority on all frames.** `_Frame` gains `priority: int = 0` (PCP 0–7). PROFINET RT frames are sent with priority 6.

**Strict-priority egress queues.** `_LinkChannel.queue` (one `simpy.Store` per direction) becomes a strict-priority queue: 8 traffic classes, higher class always transmitted first, FIFO within a class, non-preemptive (a frame already being transmitted is never interrupted). Implemented with `simpy.PriorityStore` keyed on `(-priority, sequence_number)`; the sequence number is required because `simpy.PriorityItem` alone does not guarantee FIFO order among equal priorities. Applies to switch egress ports and to node NIC egress queues alike.

**Per-switch override.** `switch ... qos=fifo` (Python: `net.switch(..., qos="fifo")`) turns prioritisation off for that switch, so the same scenario can be run with and without it. Default is `strict`.

**PROFINET processes**, one set per AR:
- **providers** — `_pn_provider` on the controller (output frames) and on the device (input frames): emit one frame every `cycle_ms` starting at `delay_ms`, carrying a cycle counter and send timestamp.
- **consumers** — each side records arrival time, latency (send → receive) and inter-arrival time of the other side's frames, and runs the **watchdog**: if no frame arrives within `hold × cycle_ms` of the previous one, an expiry is counted; the watchdog re-arms on the next frame.

New `_Frame` fields: `pn_ar_id: int | None = None` (identifies AR and direction), `pn_cycle_counter: int = 0`. `ProfinetDevice` joins `Adapter`, `MQTTBroker` and `ModbusSlave` in the link-building `isinstance` checks (own NIC egress channel, pre-seeded into directly connected switches' MAC tables).

### New metrics

All three are observed on the **consuming** node: on a controller they cover input frames from all its devices, on a device they cover output frames from its controller.

| Metric | Unit | Observed on | Meaning per sampling window |
|---|---|---|---|
| `pn_latency` | µs | Adapter (controller), ProfinetDevice | Mean provider-send → consumer-receive time |
| `pn_jitter` | µs | Adapter (controller), ProfinetDevice | Max \|inter-arrival time − cycle\| |
| `pn_watchdog_expiries` | count | Adapter (controller), ProfinetDevice | Cumulative watchdog expiries |

`MetricName`, the engine's observation dispatch, and the `.smol` interpreter's metric/node-type table are extended accordingly.

### SMOL language (ADR-0003)

```
profinet_device io1 ip=10.0.4.1
control plc -> io1 cycle=4 inputs=32 outputs=16 [hold=3] [delay=<ms>]
switch sw1 ports=8 [qos=strict|fifo]
flow host -> server rate=8000 size=1518 [priority=<0-7>]
```

Semantic errors (`SMOLSemanticError`): `control` source not an `Adapter` or target not a `ProfinetDevice`; `cycle` not a power of two in 1–512; `inputs`/`outputs` outside 1–1440; `hold` outside 1–255; `priority` outside 0–7; duplicate AR for the same pair.

### Execution backends (ADR-0004)

PROFINET RT is initially a SimPy-backend feature. Until an ns-3 implementation exists (custom Layer-2 Applications plus priority queuing via ns-3 traffic control), the ns-3 backend's capability check rejects `control`, `profinet_device`, the `pn_*` metrics and non-default `priority`/`qos`.

### Dashboard

`ProfinetDevice` nodes get their own colour (teal) in the topology panel.

### Documentation and examples

- `docs/profinet.md` — model, frame size, metrics, limitations; added to the MkDocs nav.
- `docs/dsl-reference.md`, `docs/language-reference.md`, README — new statements, parameters and metrics.
- `examples/example_profinet.py` and `.smol` — a PLC with several IO devices sharing a switch with bulk traffic, run with `qos=strict` and `qos=fifo` to show the effect of prioritisation on `pn_jitter`.

### Tests

- Frame size formula, including padding to the 60 B minimum.
- Frame counts per direction ≈ `duration / cycle`; `pn_latency` positive; `pn_jitter` ≈ 0 and no watchdog expiries on an idle network.
- **Prioritisation:** with a bulk priority-0 flow saturating a shared uplink, `pn_jitter` stays low with `qos=strict` and rises clearly with `qos=fifo`.
- Watchdog expiries > 0 in a deterministic overload scenario with `qos=fifo`.
- Semantic error tests for every rule above.
- **Regression invariant:** all existing tests pass unchanged (all existing traffic is priority 0, so strict-priority queues behave as FIFO).

---

## Consequences

**Positive**
- A third industrial communication pattern — cyclic producer–consumer with watchdog supervision — alongside polling and pub/sub.
- Metrics that match how control engineers judge a network: jitter against the cycle and watchdog expiries, not just average latency.
- 802.1Q prioritisation becomes a general engine feature, usable by any traffic, and comparable on/off per switch.
- No new transport layer: RT frames reuse the existing Layer-2 switch and link model.

**Negative / trade-offs**
- Only **RT** is modelled. **IRT** (isochronous, time-scheduled, sub-millisecond) needs a TSN-style scheduling model and is out of scope.
- Not modelled: AR establishment (DCP naming, RPC connect, parameterisation), acyclic record data, alarms, media redundancy (MRP), shared devices and multicast.
- Watchdog expiries are counted, but the AR keeps running (Option B); real devices drop the AR and take much longer to reconnect.
- SMOLPy queues are unbounded, so frames are never dropped — late frames are modelled, lost frames are not. Bounded switch buffers with tail drop would be a natural follow-up.
- Real switches typically map the 8 PCP values onto fewer hardware queues (often 4); SMOLPy uses 8 strict classes.
- Changing every egress queue to a priority queue is a core engine change; the regression invariant above must hold.
- The ns-3 backend needs its own PROFINET and priority implementation before it can run these scenarios.
