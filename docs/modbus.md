# Modbus TCP master/slave polling

SMOLPy also models Modbus TCP — a request/response protocol, unlike MQTT's fire-and-forget publish/subscribe. Because Modbus TCP already wraps its PDU in a standard Ethernet/IP/TCP frame (no serial bus, no gateway required), a `ModbusSlave` is a first-class network node wired into the same switch fabric as any `Adapter`.

## What is modelled

- **The master** is a regular `Adapter` — it calls `polls()` to periodically send Read Holding Registers (FC 03) requests toward a `ModbusSlave`.
- **Each slave** is addressed by its own `unit_id` (1–247); it replies only to requests matching its `unit_id` and ignores everything else. Multiple sensors can share one switch, each polled independently by the same master.
- **Every poll is a request/response round trip** — both frames traverse the network and contend for bandwidth, unlike MQTT where only the publisher-to-broker leg carries the payload.
- **`modbus_latency`** measures poll round-trip time (RTT): the time from when the master sends a request to when the matching response arrives back. This is the key metric for judging whether a polling interval is achievable on a given network.
- **Not modelled:** the slave's processing time (it answers at once), write function codes, and reconnecting after a timeout.

## One request in flight

By default a poll is **open loop**: the master sends a request at every instant of its schedule, whatever happened to the previous one. A real Modbus TCP master usually waits for each response before it sends the next request, with a response timeout. `outstanding=1` models that master ([ADR-0012](adr/0012-modbus-one-request-in-flight.md)):

```python
plc.polls(meter, register=0, count=10, rate=100, outstanding=1, timeout_ms=1_000)
```

```
poll plc -> meter register=0 count=10 rate=100 outstanding=1 timeout=1000
```

- **Skipped instants.** A poll instant that comes while the previous request awaits its response is skipped, and counted in **`modbus_skipped`**. The master does not send the request late: it waits for the next instant of the schedule, so the schedule does not drift.
- **Transaction ids.** Each request carries the next MBAP transaction id of its master–slave pair. A response is accepted only if it carries the id of the request awaiting it.
- **The timeout.** A request that has no response after `timeout_ms` counts in **`modbus_timeouts`**, and the pair is free again. A response that comes later is discarded: it is a frame received by the master's NIC, but not a response for `modbus_latency`.
- **Without a timeout** the master waits for ever.
- **One poll per pair.** A master can have only one poll with `outstanding=1` towards a given slave.

What a lost request or response does depends on the backend:
- **SimPy and Rust** have no transport layer and do not retransmit. With a timeout, a loss is a timeout, followed by the instants skipped while waiting for it. Without one, the poll stops at the first loss.
- **ns-3** runs the polls over its TCP, set to Linux's minimum RTO of 200 ms. A lost segment is retransmitted after the RTO, and the poll is answered about 210 ms after its request, as on real hardware ([Execution backends](backends.md#one-request-in-flight)). Open-loop polls keep segments flowing, so there TCP recovers by a fast retransmit after a few poll periods instead.

[`example_modbus_timeout.smol`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_modbus_timeout.smol) shows both, at a bottleneck that loses frames.

## Metrics

| Metric | Unit | Meaning |
|---|---|---|
| `modbus_latency` | µs | mean round-trip time of the responses accepted in the window |
| `modbus_unanswered` | polls | requests sent so far less the responses accepted so far: lost, timed out or in flight |
| `modbus_timeouts` | polls | requests that timed out so far, with `outstanding=1` |
| `modbus_skipped` | polls | poll instants skipped so far, with `outstanding=1` |

All four are observed on the master, over its polls, or on a slave, over the polls of that slave from every master ([ADR-0013](adr/0013-ingress-buffer-accounting.md)). So one master that polls two servers can tell their losses apart. Their exact definitions are in [Model Semantics](model-semantics.md#8-metrics).

**A slave can also be a traffic source.** Every end station can send flows ([ADR-0013](adr/0013-ingress-buffer-accounting.md)), so a host that serves Modbus and streams data on one port is one `modbus_slave` with a `flow`. Its responses then share its port, and its ingress account at the switch, with its stream: [`example_ingress_buffer.smol`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_ingress_buffer.smol).

## Frame size formula

```
request_size  = 69 bytes                                  (constant — Ethernet/IP/TCP/FCS + MBAP + FC + address + quantity)
response_size = 66 + 2 × register_count bytes              (grows with the number of registers read; at least 64)
```

## Dashboard

`ModbusSlave` nodes appear as **coral red** circles in the topology panel — see [Dashboard](dashboard.md).

See [`example_modbus.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_modbus.py) for a full working scenario — a PLC polling three field sensors while sharing its uplink with regular Ethernet traffic — or [Examples](examples.md) for the write-up.
