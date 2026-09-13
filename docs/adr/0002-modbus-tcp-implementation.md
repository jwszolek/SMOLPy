# ADR-0002: Modbus TCP Implementation

**Status:** Accepted  
**Date:** 2026-07-24

---

## Context

Modbus TCP is one of the most widely deployed protocols in industrial automation. It wraps the classic Modbus PDU (Protocol Data Unit) in a TCP/IP framing layer (MBAP header), making it a natural fit for SMOLPy's existing Ethernet/IP simulation model.

Unlike MQTT, which is a fire-and-forget publish/subscribe protocol, Modbus TCP is strictly **request/response**: a Master initiates all communication by polling one or more Slaves, each of which replies with the requested register data. This round-trip nature makes it possible to measure **poll latency** (RTT) as a first-class metric.

Per ADR-0001, the `Adapter` in `core/` is the protocol-agnostic endpoint that accumulates traffic-generation methods as protocols are added. A new `modbus/` package provides the infrastructure node (`ModbusSlave`).

---

## Decision

### Package layout

```
src/smolpy/
└── modbus/
    ├── __init__.py
    └── slave.py        # ModbusSlave node
```

`ModbusSlave` is the only infrastructure node — the master role is carried by a regular `Adapter` via the new `polls()` method, consistent with how `publishes()` handles MQTT.

### DSL API

```python
plc    = net.adapter("plc",    ip="10.0.0.1")
sensor = net.modbus_slave("sensor", ip="10.0.0.10", unit_id=1)

net.link(plc, sw1, speed=100, length=5)
net.link(sensor, sw1, speed=100, length=10)

# Poll 10 holding registers at 1 Hz
plc.polls(sensor, register=40001, count=10, rate=1.0)

# Optional delayed start
plc.polls(sensor, register=30001, count=5, rate=2.0, delay_ms=500)

net.observe("modbus_latency", on=plc, every=500)
```

### `ModbusSlave` node (`modbus/slave.py`)

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Node name |
| `ip` | `str` | IPv4 address |
| `mac` | `str` | MAC address (auto-derived if omitted) |
| `unit_id` | `int` | Modbus unit identifier (1–247) |

### `ModbusSpec` (added to `core/adapter.py`)

Stored in `Adapter.modbus_specs: list[ModbusSpec]`.

| Attribute | Type | Description |
|---|---|---|
| `slave` | `ModbusSlave` | Target slave node |
| `register` | `int` | Starting register address |
| `count` | `int` | Number of registers to read |
| `rate_hz` | `float` | Poll rate in Hz |
| `delay_ms` | `float` | Simulation time before first poll |
| `request_frame_size` | `int` | Computed — see Frame sizes below |
| `response_frame_size` | `int` | Computed — see Frame sizes below |

### Frame sizes

Modbus TCP uses a 6-byte MBAP header (Transaction ID, Protocol ID, Length, Unit ID) prepended to the standard Modbus PDU.

```
Request  = Ethernet/IP/TCP (54 B) + MBAP (6 B) + FC (1 B) + Start addr (2 B) + Quantity (2 B)
         = 65 bytes  (constant regardless of register count)

Response = Ethernet/IP/TCP (54 B) + MBAP (6 B) + FC (1 B) + Byte count (1 B) + Data (2 × count B)
         = 62 + 2 × count  bytes
```

### New `_Frame` fields (`sim/engine.py`)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `modbus_unit_id` | `int \| None` | `None` | Identifies Modbus frames; `None` = not Modbus |
| `modbus_register_count` | `int` | `0` | Carried in request so slave can size the response |
| `modbus_is_response` | `bool` | `False` | Distinguishes slave response from master request |

### Simulation processes

**`_modbus_poll_gen`** — runs per `ModbusSpec` on the master adapter.  
Emits a request `_Frame` at `rate_hz`, optionally after `delay_ms`.  
`created_at_us` is stamped at send time and preserved through the network for RTT calculation.

**`_modbus_slave_responder`** — runs per `ModbusSlave`.  
Reads its inbound store, ignores non-Modbus frames and frames not addressed to its `unit_id`, then enqueues a response frame back toward the master, copying `created_at_us` from the request so the master can measure RTT on arrival.

**`_modbus_latency_sampler`** — runs when `modbus_latency` is observed on an `Adapter`.  
Samples `_AdapterCounters.modbus_rtt_us` (a new list populated by `_adapter_receiver` when it sees `modbus_is_response=True` frames) and appends `(time_ms, avg_rtt_µs)` to the metric series.

### New metric

| Metric | Unit | Observed on |
|---|---|---|
| `modbus_latency` | µs | `Adapter` (master) |

Reports the average round-trip time per polling interval — from when the master sends a request to when the slave's response is delivered back.

### `ModbusSlave` routing in the engine

`ModbusSlave` is treated the same as `Adapter` and `MQTTBroker` for link-channel assignment — it gets an `adapter_out` channel and is pre-seeded into the MAC table of any directly connected switch. This requires adding `ModbusSlave` to the `isinstance` checks in the link-building loop.

---

## Consequences

**Positive**
- Modbus TCP simulation is now possible, including accurate frame sizing and RTT measurement.
- The request/response model is correctly represented — unlike MQTT, both request and response frames traverse the network and contend for bandwidth.
- Adding further Modbus function codes (Write Single Register, Write Multiple Registers) requires only extending `ModbusSpec` and `_modbus_poll_gen` — no structural changes.

**Negative / trade-offs**
- `_Frame` grows three more fields; all explicit `_Frame(...)` construction sites must be updated.
- The current implementation models only **Read Holding Registers** (FC 03). Write operations and other function codes are deferred.
- Slave processing time (the time a real PLC takes to prepare a response) is not modelled — response is sent immediately on frame arrival. This can be added later via a configurable `processing_delay_ms` on `ModbusSlave`.
