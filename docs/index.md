# SMOLPy

Python rewrite of SMOL — a Network Description Language and Discrete-Event Simulator for industrial Measurement-Diagnostics-Control (MDC) networks.

SMOLPy lets you describe a network topology in pure Python, define traffic flows, and run a discrete-event simulation (powered by SimPy) that produces real metric time-series. A built-in Dear PyGui desktop dashboard shows the topology and live metric charts as the simulation runs.

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
- **[MQTT](mqtt.md)** — publish/subscribe messaging model
- **[Modbus TCP](modbus.md)** — master/slave polling model
- **[Simulation Engine](simulation-engine.md)** — how the discrete-event model works under the hood
- **[Dashboard](dashboard.md)** — the live Dear PyGui desktop view
- **[Examples](examples.md)** — nine ready-to-run scenarios
- **[Architecture Decision Records](adr/0001-protocol-package-structure.md)** — the design decisions behind SMOLPy's structure
