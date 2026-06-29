# SMOLPy

Python rewrite of SMOL — a Network Description Language and Discrete-Event Simulator for industrial Measurement-Diagnostics-Control (MDC) networks.

SMOLPy lets you describe a network topology in pure Python, define traffic flows, and run a discrete-event simulation (powered by SimPy) that produces real metric time-series.  A built-in Dear PyGui desktop dashboard shows the topology and live metric charts as the simulation runs.

---

## Install

```bash
uv sync          # installs all runtime + dev dependencies
```

---

## Quick start

```python
from smolpy import Network

net    = Network("office-net")
host_a = net.adapter("host-A", ip="10.0.0.1")
server = net.adapter("server",  ip="10.0.0.10")
sw1    = net.switch("sw1", ports=8, mode="store-and-forward")

net.link(host_a, sw1, speed=1_000,  length=5)   # speed in Mb/s, length in metres
net.link(server, sw1, speed=10_000, length=2)

host_a.sends(to=server, rate=8_000, size=1_518, pattern="constant")

net.observe("throughput",  on=server, every=100)   # sample every 100 ms
net.observe("queue_depth", on=sw1,    every=50)

result = net.simulate(duration=30_000, live=True)  # 30 s simulation with live dashboard
result.report()                                     # print summary table to terminal
```

Run it:

```bash
uv run smolpy run my_script.py
```

---

## Dashboard

When `live=True` the simulation runs in a background thread while a full-screen Dear PyGui window opens immediately.

### Topology panel (left)

Each node is drawn as a coloured circle:

| Colour | Node type |
|---|---|
| Blue | Adapter (host / server / NIC) |
| Green | Switch |
| Orange | Hub |

Node fill changes dynamically during the simulation:

| Appearance | Meaning |
|---|---|
| Dim (faded) | Node idle — no traffic yet |
| Pulsing bright fill | Node actively **transmitting** (`bytes_sent > 0`) |
| Solid bright fill | Node forwarding traffic (switch / hub) |
| Pulsing amber outer ring | Node actively **receiving** data (`bytes_received > 0`) |

Animated particles flow along every link to show live traffic direction.

### Metrics panel (right)

One chart per observed metric.  All series update in real time.  Axes auto-scale to fit the data.

### Simulation controls

Three controls appear in the title bar during a live simulation:

| Control | Effect |
|---|---|
| **⏸ Pause** | Freezes simulation time; dashboard stays interactive.  Click again to resume. |
| **▶ Resume** | Continues from the exact pause point. |
| **⏹ Stop** | Ends the simulation early; plots freeze at the last collected sample. |

Status indicator:
- **● Simulating…** — running
- **● Paused** — paused by user
- **● Done** — completed normally
- **● Stopped** — ended by user

---

## DSL reference

### Topology builders

```python
adapter = net.adapter("name", ip="10.0.0.1")          # NIC / host / server
switch  = net.switch("name",  ports=16, mode="store-and-forward")
hub     = net.hub("name",     ports=8)
broker  = net.mqtt_broker("name", ip="10.0.2.1")       # MQTT message broker
net.link(a, b, speed=1_000, length=10)                 # Mb/s and metres
```

Multiple switches can be chained to model hierarchical topologies:

```python
core_sw = net.switch("core-sw", ports=16, mode="store-and-forward")
edge_sw = net.switch("edge-sw", ports=8,  mode="store-and-forward")
net.link(edge_sw, core_sw, speed=1_000, length=5)      # inter-switch uplink
```

### Traffic

```python
# Basic Ethernet send
src.sends(to=dst, rate=8_000, size=1_518, pattern="constant")

# Delayed start (useful for staggered scenarios)
src.sends(to=dst, rate=8_000, size=1_518, pattern="constant", delay_ms=5_000)

# MQTT publish (sensor-style, constant-rate)
sensor.publishes(to=broker, topic="plant/temp", rate=1.0, payload=20, qos=1)
sensor.publishes(to=broker, topic="plant/temp", rate=1.0, payload=20, qos=0, delay_ms=2_000)

# Broker topic routing — must be called before simulate()
broker.routes("plant/temp", to=[server])
```

| Parameter | Type | Description |
|---|---|---|
| `to` | Adapter | Destination adapter |
| `rate` | float | Frames per second |
| `size` | int \| `"imix"` | Frame size in bytes, or Internet Mix distribution |
| `pattern` | str | `"constant"`, `"poisson"`, or `"bursty"` |
| `delay_ms` | float | Simulation time before this flow starts (default 0) |

**`publishes()` parameters**

| Parameter | Type | Description |
|---|---|---|
| `to` | MQTTBroker | Target broker |
| `topic` | str | MQTT topic string |
| `rate` | float | Messages per second (default 1.0) |
| `payload` | int | Payload bytes (default 20) |
| `qos` | int | 0 = fire-and-forget, 1 = PUBACK acknowledgement |
| `delay_ms` | float | Simulation time before publishing starts (default 0) |

**Traffic patterns**

| Pattern | Description |
|---|---|
| `"constant"` | Fixed inter-frame gap — models a saturated link |
| `"poisson"` | Exponentially distributed gaps — models random/bursty traffic |
| `"bursty"` | Pareto-distributed burst lengths — models ON/OFF sources |

**Frame sizes**

| Value | Description |
|---|---|
| integer | Fixed size in bytes (e.g. `512`, `1_518`) |
| `"imix"` | 40 % × 64 B, 57 % × 594 B, 3 % × 1 518 B |

### Observations

```python
net.observe(metric, on=node, every=interval_ms)
```

| Metric | Unit | Observed on |
|---|---|---|
| `throughput` | Mb/s | Adapter |
| `latency` | µs | Adapter |
| `frame_loss` | % | Adapter |
| `bytes_sent` | MB | Adapter (sender) |
| `bytes_received` | MB | Adapter (receiver) |
| `queue_depth` | frames | Switch |
| `utilization` | % | Any node |
| `collision_rate` | /s | Hub |
| `broker_queue` | msgs | MQTTBroker |

### Simulation

```python
result = net.simulate(duration=30_000)             # headless, duration in ms
result = net.simulate(duration=30_000, live=True)  # with live dashboard

result.report()   # print summary table (avg / min / max per metric)
result.plot()     # open static dashboard for a completed result

# Export metric time-series (format inferred from extension)
result.export("results.csv")    # long CSV: time_ms, metric, value
result.export("results.json")   # JSON dict of lists-of-pairs
result.export("out.csv", format="csv")   # explicit format override
```

---

## MQTT publish-subscribe

SMOLPy models application-layer MQTT traffic on top of the standard Ethernet/IP/TCP wire model.

### What is modelled

- **Publisher adapters** call `publishes()` to emit periodic MQTT PUBLISH frames at a fixed rate toward an `MQTTBroker` node.
- **The broker** receives PUBLISH frames and fans out one copy to each registered subscriber per topic (`routes()`).  QoS 0 delivers silently; QoS 1 additionally sends a PUBACK frame (58 bytes) back toward the publisher.
- **Subscriber adapters** receive forwarded copies just like normal Ethernet frames; all standard metrics (`throughput`, `latency`, `bytes_received`) apply.
- **`broker_queue`** samples the broker's inbound store depth — unprocessed PUBLISH frames waiting to be forwarded.  A non-zero and rising queue indicates the broker or its downstream link is becoming a bottleneck.

### Frame size formula

```
frame_size = 54 (Ethernet+IPv4+TCP) + 2 (MQTT fixed header) + 2 (topic-length field) + len(topic) + (2 if qos > 0 else 0) + payload_bytes
```

A typical small sensor message (`topic="plant/temperature"`, `payload=20`, `qos=1`) produces a 96-byte frame, roughly 16× smaller than a maximum-size bulk frame (1 518 B).

### Dashboard

`MQTTBroker` nodes appear as **purple** circles in the topology panel.

---

## Simulation engine

- **MAC-learning switch** — each switch pre-seeds its forwarding table from the topology wiring, eliminating spurious flooding toward silent endpoints (e.g. a server that only receives).  Dynamic learning still operates for traffic through intermediate switches.
- **Store-and-forward model** — transmission delay + propagation delay per hop.
- **Queuing** — each link direction is an independent SimPy Store; `queue_depth` reports buffered frames at the switch's outbound ports.
- **Traffic shaping** — constant, Poisson, and Pareto-burst patterns; IMIX frame-size distribution.
- **Live mode** — simulation runs in 200 chunks (~8 s total wall time); the dashboard reads shared metric arrays between chunks via Python's GIL.

---

## Examples

See [`examples/README.md`](examples/README.md) for eight ready-to-run scenarios covering single-switch saturation, oversubscription, two-tier access bottlenecks, and MQTT publish-subscribe.

```bash
uv run smolpy run examples/example.py
uv run smolpy run examples/example_two_tier.py
```
