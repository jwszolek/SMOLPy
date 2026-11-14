# SMOLPy

[![CI](https://github.com/jwszolek/SMOLPy/actions/workflows/ci.yml/badge.svg)](https://github.com/jwszolek/SMOLPy/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/smolpy)](https://pypi.org/project/smolpy/)
[![Python](https://img.shields.io/pypi/pyversions/smolpy)](https://pypi.org/project/smolpy/)
[![License](https://img.shields.io/github/license/jwszolek/SMOLPy)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://jwszolek.github.io/SMOLPy/)

Python rewrite of SMOL — a Network Description Language and Discrete-Event Simulator for industrial Measurement-Diagnostics-Control (MDC) networks.

SMOLPy lets you describe a network topology in pure Python, define traffic flows, and run a discrete-event simulation (powered by SimPy) that produces real metric time-series.  A built-in Dear PyGui desktop dashboard shows the topology and live metric charts as the simulation runs.

📖 **Full documentation:** [jwszolek.github.io/SMOLPy](https://jwszolek.github.io/SMOLPy/)

---

## Supported protocols

| Protocol | Runs over | Communication pattern | Nodes / API | `.smol` statements | Key metrics |
|---|---|---|---|---|---|
| **Ethernet** (generic traffic) | Layer 2 frames | One-way flows: constant, Poisson, bursty, batch | `Adapter.sends()` | `flow` | `throughput`, `latency`, `bytes_sent`, `bytes_received` |
| **MQTT** | TCP/IP | Publish / subscribe through a broker, QoS 0 and 1 | `MQTTBroker`, `Adapter.publishes()`, `MQTTBroker.routes()` | `mqtt_broker`, `publish`, `route` | `broker_queue`, `latency` |
| **Modbus TCP** | TCP/IP | Request / response polling (Read Holding Registers, FC 03) | `ModbusSlave`, `Adapter.polls()` | `modbus_slave`, `poll` | `modbus_latency` |
| **PROFINET RT** | Layer 2 (EtherType 0x8892), 802.1Q priority 6 | Cyclic producer / consumer exchange with watchdog | `ProfinetDevice`, `Adapter.controls()` | `profinet_device`, `control` | `pn_latency`, `pn_jitter`, `pn_watchdog_expiries` |

Infrastructure: MAC-learning **switches** with 802.1Q strict-priority egress queues (`qos="strict"`, or `qos="fifo"` to disable prioritisation). One `Adapter` can act in several roles at once — e.g. a PLC that is a Modbus master, an MQTT publisher and a PROFINET controller.

**Not supported (yet):** PROFINET IRT, PROFIBUS, EtherNet/IP, EtherCAT, OPC UA, Modbus RTU (serial), and Modbus write function codes. See the [Architecture Decision Records](https://jwszolek.github.io/SMOLPy/adr/0001-protocol-package-structure/) for what is planned.

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
| Blue | Adapter (host / server / NIC / PLC) |
| Green | Switch |
| Purple | MQTTBroker |
| Coral red | ModbusSlave |
| Teal | ProfinetDevice |

Node fill changes dynamically during the simulation:

| Appearance | Meaning |
|---|---|
| Dim (faded) | Node idle — no traffic yet |
| Pulsing bright fill | Node actively **transmitting** (`bytes_sent > 0`) |
| Solid bright fill | Node forwarding traffic (switch) |
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
switch  = net.switch("name",  ports=16, mode="store-and-forward")   # qos="strict" (default) or "fifo"
broker  = net.mqtt_broker("name", ip="10.0.2.1")       # MQTT message broker
slave   = net.modbus_slave("name", ip="10.0.0.10", unit_id=1)  # Modbus TCP slave
device  = net.profinet_device("name", ip="10.0.4.1")   # PROFINET IO device
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

# Modbus TCP poll (master → slave, Read Holding Registers)
plc.polls(slave, register=40001, count=10, rate=1.0)
plc.polls(slave, register=30001, count=5, rate=2.0, delay_ms=500)

# PROFINET RT cyclic I/O (controller ↔ device), 4 ms update time
plc.controls(device, cycle=4, inputs=32, outputs=16, hold=3)
```

| Parameter | Type | Description |
|---|---|---|
| `to` | Adapter | Destination adapter |
| `rate` | float | Frames per second |
| `size` | int \| `"imix"` | Frame size in bytes, or Internet Mix distribution |
| `pattern` | str | `"constant"`, `"poisson"`, `"bursty"`, or `"batch"` |
| `delay_ms` | float | Simulation time before this flow starts (default 0) |
| `priority` | int | 802.1Q priority (PCP) 0–7 (default 0) |

**`publishes()` parameters**

| Parameter | Type | Description |
|---|---|---|
| `to` | MQTTBroker | Target broker |
| `topic` | str | MQTT topic string |
| `rate` | float | Messages per second (default 1.0) |
| `payload` | int | Payload bytes (default 20) |
| `qos` | int | 0 = fire-and-forget, 1 = PUBACK acknowledgement |
| `delay_ms` | float | Simulation time before publishing starts (default 0) |

**`polls()` parameters**

| Parameter | Type | Description |
|---|---|---|
| `slave` | ModbusSlave | Target slave to poll |
| `register` | int | Starting holding-register address |
| `count` | int | Number of registers to read |
| `rate` | float | Polls per second (default 1.0) |
| `delay_ms` | float | Simulation time before polling starts (default 0) |

**`controls()` parameters**

| Parameter | Type | Description |
|---|---|---|
| `device` | ProfinetDevice | Target IO device |
| `cycle` | int | Update time in ms: 1, 2, 4, … 512 |
| `inputs` | int | Input data bytes per frame (device → controller), 1–1440 |
| `outputs` | int | Output data bytes per frame (controller → device), 1–1440 |
| `hold` | int | Data hold factor: watchdog = `hold × cycle` (default 3) |
| `delay_ms` | float | Simulation time before cyclic exchange starts (default 0) |

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
| `broker_queue` | msgs | MQTTBroker |
| `modbus_latency` | µs | Adapter (master) |
| `pn_latency` | µs | Adapter (controller), ProfinetDevice |
| `pn_jitter` | µs | Adapter (controller), ProfinetDevice |
| `pn_watchdog_expiries` | count | Adapter (controller), ProfinetDevice |

### Simulation

```python
result = net.simulate(duration=30_000)              # headless — silent, fastest
result = net.simulate(duration=30_000, text=True)   # rich text dashboard in terminal
result = net.simulate(duration=30_000, live=True)   # full Dear PyGui desktop window

result.report()   # print summary table (avg / min / max per metric)
result.plot()     # open static dashboard for a completed result

# Export metric time-series (format inferred from extension)
result.export("results.csv")    # long CSV: time_ms, metric, value
result.export("results.json")   # JSON dict of lists-of-pairs
result.export("out.csv", format="csv")   # explicit format override
```

**Text mode** (`text=True`) displays a live updating table in the terminal — no display server or GUI toolkit required.  Ideal for headless servers, SSH sessions, and CI environments.

### Many runs

```python
from smolpy import run_many

batch = run_many(net, runs=30, duration=10_000)   # seeds 42 … 71, on all CPU cores
batch.raise_on_failure()
results = batch.results()                         # one SimulationResult per run
```

Replications and parameter sweeps run in parallel worker processes; see the [DSL Reference](https://jwszolek.github.io/SMOLPy/dsl-reference/#many-runs).

### Quick demo

```bash
smolpy demo          # built-in 3-client scenario, text mode, no script needed
```

---

## SMOL external DSL

Network topologies can also be described in a standalone `.smol` file — no Python required. `smolpy run` executes `.smol` files directly, using the same simulation engine underneath.

```
network "office-net" {
    adapter host-A ip=10.0.0.1
    adapter server ip=10.0.0.10
    switch  sw1    ports=8 mode=store-and-forward

    link host-A -- sw1 speed=1000  length=5
    link server -- sw1 speed=10000 length=2

    flow host-A -> server rate=8000 size=1518 pattern=constant

    observe throughput  on server every=100
    observe queue_depth on sw1    every=50

    simulate duration=30000 mode=text
}
```

```bash
uv run smolpy run topology.smol
```

Malformed or semantically invalid `.smol` files (undeclared node references, wrong node type for a statement, an unsupported metric, ...) produce a clean `file:line: message` error — no Python traceback. See the [SMOL Language Reference](https://jwszolek.github.io/SMOLPy/language-reference/) for the complete syntax.

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

## Modbus TCP master/slave polling

SMOLPy also models Modbus TCP — a request/response protocol, unlike MQTT's fire-and-forget publish/subscribe. Because Modbus TCP already wraps its PDU in a standard Ethernet/IP/TCP frame (no serial bus, no gateway required), a `ModbusSlave` is a first-class network node wired into the same switch fabric as any `Adapter`.

### What is modelled

- **The master** is a regular `Adapter` — it calls `polls()` to periodically send Read Holding Registers (FC 03) requests toward a `ModbusSlave`.
- **Each slave** is addressed by its own `unit_id` (1–247); it replies only to requests matching its `unit_id` and ignores everything else. Multiple sensors can share one switch, each polled independently by the same master.
- **Every poll is a request/response round trip** — both frames traverse the network and contend for bandwidth, unlike MQTT where only the publisher-to-broker leg carries the payload.
- **`modbus_latency`** measures poll round-trip time (RTT): the time from when the master sends a request to when the matching response arrives back. This is the key metric for judging whether a polling interval is achievable on a given network.

### Frame size formula

```
request_size  = 65 bytes                                  (constant — Ethernet/IP/TCP + MBAP + FC + address + quantity)
response_size = 62 + 2 × register_count bytes              (grows with the number of registers read)
```

### Dashboard

`ModbusSlave` nodes appear as **coral red** circles in the topology panel.

---

## PROFINET RT cyclic I/O

PROFINET RT is the third industrial pattern SMOLPy models: **cyclic producer–consumer exchange**. Controller and device each send their process data every update time, unprompted, and each supervises the other's frames with a watchdog. RT frames are plain Layer-2 Ethernet frames (EtherType `0x8892`, no IP/TCP) tagged with 802.1Q priority 6.

```python
plc = net.adapter("plc", ip="10.0.0.1")
io1 = net.profinet_device("io1", ip="10.0.4.1")
plc.controls(io1, cycle=4, inputs=32, outputs=16, hold=3)

net.observe("pn_jitter",            on=plc, every=100)
net.observe("pn_watchdog_expiries", on=plc, every=100)
```

### What is modelled

- **The controller** is a regular `Adapter`; `controls()` sets up one Application Relationship (AR) with a `ProfinetDevice`.
- **Both directions are cyclic and independent** — the controller sends output frames, the device sends input frames, each every `cycle` ms.
- **Watchdog** — a consumer that receives no frame for `hold × cycle` counts an expiry (`pn_watchdog_expiries`). Real devices would drop the AR at that point; SMOLPy counts it and keeps exchanging, so the whole run stays measurable.
- **`pn_jitter`** — the largest deviation of the frame inter-arrival time from the cycle, per sampling window. **`pn_latency`** — mean send → receive time.
- **Prioritisation matters** — every egress queue serves 802.1Q priorities strictly, so RT frames (priority 6) overtake ordinary traffic (priority 0). Build the switch with `qos="fifo"` to see what happens without it: under bursty load the cyclic data is delayed by whole bursts and watchdogs start to expire.

### Frame size formula

```
frame_size = max(60, 14 (Ethernet) + 4 (802.1Q tag) + 2 (FrameID) + io_bytes + 4 (APDU status))
```

### Not modelled

IRT (isochronous, scheduled) traffic, AR establishment (DCP / RPC connect), acyclic record data, alarms, media redundancy (MRP). Switch buffers are unbounded, so frames are delayed but never dropped.

### Dashboard

`ProfinetDevice` nodes appear as **teal** circles in the topology panel.

---

## Simulation engine

- **MAC-learning switch** — each switch pre-seeds its forwarding table from the topology wiring, eliminating spurious flooding toward silent endpoints (e.g. a server that only receives).  Dynamic learning still operates for traffic through intermediate switches.
- **Store-and-forward model** — transmission delay + propagation delay per hop.
- **Queuing** — each link direction has its own egress queue with 8 strict 802.1Q priority classes (FIFO within a class, non-preemptive); `qos="fifo"` on a switch ignores priorities. `queue_depth` reports buffered frames at the switch's outbound ports.
- **Traffic shaping** — constant, Poisson, and Pareto-burst patterns; IMIX frame-size distribution.
- **Live mode** — simulation runs in 200 chunks (~8 s total wall time); the dashboard reads shared metric arrays between chunks via Python's GIL.

---

## Examples

See [`examples/README.md`](examples/README.md) for ten ready-to-run scenarios covering single-switch saturation, oversubscription, two-tier access bottlenecks, MQTT publish-subscribe, Modbus TCP polling, and PROFINET RT cyclic I/O.

```bash
uv run smolpy run examples/example.py
uv run smolpy run examples/example_two_tier.py
```
