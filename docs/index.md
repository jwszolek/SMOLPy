# SMOLPy

Python rewrite of SMOL — a Network Description Language and Discrete-Event Simulator for industrial Measurement-Diagnostics-Control (MDC) networks.

SMOLPy lets you describe a network topology in pure Python, define traffic flows, and run a discrete-event simulation (powered by SimPy) that produces real metric time-series. A built-in Dear PyGui desktop dashboard shows the topology and live metric charts as the simulation runs.

## Supported protocols

| Protocol | Runs over | Communication pattern | Key metrics | Details |
|---|---|---|---|---|
| **Ethernet** (generic traffic) | Layer 2 frames | One-way flows: constant, Poisson, bursty, batch | `throughput`, `latency`, `bytes_sent`, `bytes_received` | [DSL Reference](dsl-reference.md) |
| **MQTT** | TCP/IP | Publish / subscribe through a broker, QoS 0 and 1 | `broker_queue`, `latency` | [MQTT](mqtt.md) |
| **Modbus TCP** | TCP/IP | Request / response polling (Read Holding Registers) | `modbus_latency` | [Modbus TCP](modbus.md) |
| **PROFINET RT** | Layer 2, 802.1Q priority 6 | Cyclic producer / consumer exchange with watchdog | `pn_latency`, `pn_jitter`, `pn_watchdog_expiries` | [PROFINET RT](profinet.md) |

Infrastructure: MAC-learning switches with 802.1Q strict-priority egress queues (or plain FIFO with `qos="fifo"`) and shared-medium hubs.

**Not supported (yet):** PROFINET IRT, PROFIBUS, EtherNet/IP, EtherCAT, OPC UA, Modbus RTU (serial), and Modbus write function codes.

## Install

```bash
uv sync          # installs all runtime + dev dependencies
```

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

## Quick demo

```bash
smolpy demo          # built-in 3-client scenario, text mode, no script needed
```

## Where to go next

- **[DSL Reference](dsl-reference.md)** — topology builders, traffic generators, observations, and simulation control
- **[SMOL Language Reference](language-reference.md)** — describe a network in a standalone `.smol` file, no Python required
- **[MQTT](mqtt.md)** — publish/subscribe messaging model
- **[Modbus TCP](modbus.md)** — master/slave polling model
- **[PROFINET RT](profinet.md)** — cyclic I/O with watchdog supervision and 802.1Q prioritisation
- **[Simulation Engine](simulation-engine.md)** — how the discrete-event model works under the hood
- **[Dashboard](dashboard.md)** — the live Dear PyGui desktop view
- **[Examples](examples.md)** — ten ready-to-run scenarios
- **[Architecture Decision Records](adr/0001-protocol-package-structure.md)** — the design decisions behind SMOLPy's structure
