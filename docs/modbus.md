# Modbus TCP master/slave polling

SMOLPy also models Modbus TCP — a request/response protocol, unlike MQTT's fire-and-forget publish/subscribe. Because Modbus TCP already wraps its PDU in a standard Ethernet/IP/TCP frame (no serial bus, no gateway required), a `ModbusSlave` is a first-class network node wired into the same switch fabric as any `Adapter`.

## What is modelled

- **The master** is a regular `Adapter` — it calls `polls()` to periodically send Read Holding Registers (FC 03) requests toward a `ModbusSlave`.
- **Each slave** is addressed by its own `unit_id` (1–247); it replies only to requests matching its `unit_id` and ignores everything else. Multiple sensors can share one switch, each polled independently by the same master.
- **Every poll is a request/response round trip** — both frames traverse the network and contend for bandwidth, unlike MQTT where only the publisher-to-broker leg carries the payload.
- **`modbus_latency`** measures poll round-trip time (RTT): the time from when the master sends a request to when the matching response arrives back. This is the key metric for judging whether a polling interval is achievable on a given network.

## Frame size formula

```
request_size  = 65 bytes                                  (constant — Ethernet/IP/TCP + MBAP + FC + address + quantity)
response_size = 62 + 2 × register_count bytes              (grows with the number of registers read)
```

## Dashboard

`ModbusSlave` nodes appear as **coral red** circles in the topology panel — see [Dashboard](dashboard.md).

See [`example_modbus.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_modbus.py) for a full working scenario — a PLC polling three field sensors while sharing its uplink with regular Ethernet traffic — or [Examples](examples.md) for the write-up.
