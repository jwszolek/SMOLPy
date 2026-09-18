# Examples

Nine ready-to-run scenarios covering basic connectivity, file transfers, load scaling, multi-tier access bottlenecks, MQTT publish-subscribe, and Modbus TCP polling. Every example opens a live desktop dashboard as the simulation runs (see [Dashboard](dashboard.md) for what the controls and node colours mean).

```bash
uv sync          # install all dependencies (run once from the project root)
uv run smolpy run examples/<filename>.py

# Export metric time-series after the simulation finishes
uv run smolpy run examples/example.py --output results.csv
```

The full source for every example lives in [`examples/`](https://github.com/jwszolek/SMOLPy/tree/main/examples) on GitHub.

## 1. Basic office network — [`example.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example.py)

Two hosts and a server behind a single switch. One constant-rate flow and one Poisson flow with IMIX frames run concurrently.

```
[host-A] ── 1 Gbps ──┐
                   [sw1] ── 10 Gbps ── [server]
[host-B] ── 1 Gbps ──┘
```

Simulation: **10 s**

| Metric | Expected behaviour |
|---|---|
| `throughput:server` | Steady ~450 Mb/s (combined from both hosts) |
| `latency:server` | Low and stable — switch is not congested |
| `queue_depth:sw1` | Near zero — 10 Gbps server link has plenty of headroom |

Also available as a standalone [`.smol` file](https://github.com/jwszolek/SMOLPy/blob/main/examples/example.smol) — same topology, no Python required (`uv run smolpy run examples/example.smol`). See the [SMOL Language Reference](language-reference.md).

## 2. File transfer with background traffic — [`example_file_transfer.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_file_transfer.py)

A client saturates its 100 Mbps uplink pushing a large file to the server while a background host adds light web traffic.

```
[client]  ── 100 Mbps / 10 m ──┐
                             [core-sw] ── 1 Gbps / 2 m ── [server]
[bg-host] ── 100 Mbps / 50 m ──┘
```

Simulation: **12 s**

| Metric | Expected behaviour |
|---|---|
| `throughput:server` | ~94 Mb/s from client + ~10 Mb/s background ≈ 104 Mb/s |
| `queue_depth:core-sw` | Near zero — 1 Gbps server link drains the switch instantly |
| `utilization:core-sw` | ~100 % (two 100 Mbps inputs, both active) |

## 3. Three clients with delayed starts — [`example_staggered_transfer.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_staggered_transfer.py)

Three clients begin sending at different times. Cumulative MB charts make the transfer schedule immediately visible.

```
[client-1] ── 100 Mbps ──┐
[client-2] ── 100 Mbps ──┤── [core-sw] ── 1 Gbps ── [server]
[client-3] ── 100 Mbps ──┘
```

| Client | Starts at |
|---|---|
| client-1 | t = 0 s |
| client-2 | t = 5 s |
| client-3 | t = 25 s |

Simulation: **40 s**

| Metric | Expected behaviour |
|---|---|
| `bytes_sent:client-*` | Three distinct lines; each rises when its delay expires |
| `bytes_received:server` | Slope increases at t = 5 s and t = 25 s |
| `throughput:server` | Steps up by ~94 Mb/s each time a client joins |
| `queue_depth:core-sw` | Near zero — 300 Mb/s is well within the 1 Gbps link |

## 4. 9 clients approach saturation — [`example_10_clients.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_10_clients.py)

Ten adapters are configured but the 50 s simulation window means clients 1–9 are active (client-10 starts at t = 54 s, after the sim ends). Nine clients × 100 Mbps = 900 Mbps — 90 % of the 1 Gbps server link.

```
[client-1 … 9]  ── 100 Mbps ──┐
                            [core-sw] ── 1 Gbps ── [server]
```

**Schedule:** one new client every 6 s (t = 0, 6, 12, … , 48 s). Simulation: **50 s**.

| Metric | Expected behaviour |
|---|---|
| `throughput:server` | Steps up ~100 Mb/s every 6 s; plateaus at ~900 Mb/s |
| `queue_depth:core-sw` | Stays near zero — 1 Gbps link has headroom |
| `bytes_sent:client-*` | 9 distinct lines, each starting 6 s after the previous |

## 5. 12 clients, 20 % oversubscription — [`example_12_clients.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_12_clients.py)

All twelve clients join across a 90 s window. When client-11 joins at t = 50 s the combined load crosses the 1 Gbps server link and the switch queue starts to grow.

```
[client-1 … 12]  ── 100 Mbps ──┐
                             [core-sw] ── 1 Gbps ── [server]
```

**Schedule:** one new client every 5 s (t = 0, 5, 10, … , 55 s). Simulation: **90 s**.

| Metric | Expected behaviour |
|---|---|
| `throughput:server` | Locks at ~1 Gb/s when the link saturates; does not rise with clients 11–12 |
| `queue_depth:core-sw` | Near zero until t ≈ 50 s, then rises sharply |
| `bytes_received:server` | Slope flattens at the 1 Gbps ceiling from t ≈ 50 s |

## 6. Up to 17 clients, 2× oversubscription — [`example_20_clients.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_20_clients.py)

Twenty adapters are configured; with a 50 s simulation and 3 s gaps, clients 1–17 actually run. At peak, 17 × 100 Mbps = 1 700 Mbps floods a 1 Gbps server link.

```
[client-1 … 17 active]  ── 100 Mbps ──┐
                                    [core-sw] ── 1 Gbps ── [server]
```

**Schedule:** one new client every 3 s (t = 0, 3, 6, … , 48 s active window). Simulation: **50 s**.

| Metric | Expected behaviour |
|---|---|
| `throughput:server` | Saturates at ~1 Gb/s around t = 30 s (10 active clients) |
| `queue_depth:core-sw` | Climbs steeply from t ≈ 30 s and keeps growing |
| `bytes_received:server` | Flat at 1 Gbps from t ≈ 30 s despite more senders joining |

## 7. Two-tier switching, access bottleneck — [`example_two_tier.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_two_tier.py)

Nine direct clients and five edge clients behind a second switch whose **uplink is only 100 Mbps** — the same as a single client link. Clients join one per second so each bytes_sent line is individually visible.

```
[client-1 … 9]  ── 100 Mbps / 20 m ──┐
                                    [core-sw] ── 1 Gbps / 2 m ── [server]
[edge-sw] ─── 100 Mbps / 5 m ─────┘
    │
[edge-1 … 5] ── 100 Mbps / 10 m ──┘
```

| Time window | Event |
|---|---|
| t = 0–8 s | Direct clients join one per second; load ramps 100 → 900 Mbps |
| t = 9–11 s | Direct load stable at 900 Mbps; server link at 90 % |
| t = 12 s | edge-1 joins; 100 Mbps on uplink — just fits; server = 1 Gbps |
| t = 13 s | edge-2 joins; **200 Mbps > 100 Mbps uplink → edge-sw queue starts** |
| t = 14–16 s | edge-3/4/5 join; edge-sw queue grows continuously |

Simulation: **40 s**

| Metric | Expected behaviour |
|---|---|
| `queue_depth:edge-sw` | Near zero until t = 13 s, then climbs steeply |
| `queue_depth:core-sw` | Stays near zero — edge-sw uplink caps inbound at 100 Mbps |
| `throughput:server` | Ramps to ~1 Gbps and holds flat regardless of edge client count |
| `bytes_sent:client-*` | 9 lines at ~11.8 MB/s each |
| `bytes_sent:edge-*` | edge-1 achieves ~11.8 MB/s; others are starved (~4 MB/s each once 5 share the uplink) |

## 8. 10 temperature sensors via MQTT broker — [`example_mqtt.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_mqtt.py)

Ten sensors publish MQTT messages at 1 msg/s to a broker on topic `"plant/temperature"`. The broker fans out each message to the server subscriber. Sensors stagger their start by 2 s so each sensor's contribution ramps up individually.

```
[sensor-1 … 10] ── 100 Mbps / 10 m ──┐
                                    [sw] ── 1 Gbps / 2 m ── [mqtt-broker] ── 1 Gbps / 2 m ── [server]
```

| Sensor | Starts at |
|---|---|
| sensor-1 | t = 0 s |
| sensor-2 | t = 2 s |
| … | … |
| sensor-10 | t = 18 s |

Simulation: **40 s**

| Metric | Expected behaviour |
|---|---|
| `broker_queue:mqtt-broker` | Stays at 0 — 1 Gbps uplink drains the queue instantly |
| `throughput:server` | Steps up by ~0.77 kb/s every 2 s as each sensor joins; total ≈ 7.7 kb/s at t = 18 s |
| `latency:server` | Steady and low — propagation + one switch hop, no congestion |

Frame size: 96 B per PUBLISH (`Ethernet+IPv4+TCP=54` + `MQTT overhead=22` + `payload=20`). See [MQTT](mqtt.md) for the general model.

## 9. PLC polling 3 field sensors via Modbus TCP — [`example_modbus.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_modbus.py)

A PLC polls three `ModbusSlave` sensors (temperature, pressure, flow), each on its own `unit_id`, while also pushing periodic bursty traffic to a historian server over the same uplink — showing Modbus and regular Ethernet traffic sharing one network.

```
[temp-sensor]      ── 100 Mbps / 10 m ──┐
[pressure-sensor]  ── 100 Mbps / 10 m ──┼── [sw] ── 1 Gbps / 2 m ── [historian]
[flow-sensor]      ── 100 Mbps / 10 m ──┤
[plc]              ── 100 Mbps / 5 m ───┘
```

| Poll | Registers | Rate |
|---|---|---|
| temperature | 2 @ 40001 | 1 Hz |
| pressure | 2 @ 40010 | 1 Hz |
| flow | 4 @ 40020 | 0.5 Hz |

Simulation: **30 s**

| Metric | Expected behaviour |
|---|---|
| `modbus_latency:plc` | Low and stable (~20–30 µs) — 100 Mbps links have plenty of headroom for three low-rate pollers |
| `queue_depth:sw` | Near zero — no link is close to saturation |
| `throughput:historian` | Bursty, averaging well under 1 Mb/s |

See [Modbus TCP](modbus.md) for the general model.
