# DSL Reference

## Topology builders

```python
adapter = net.adapter("name", ip="10.0.0.1")          # NIC / host / server
switch  = net.switch("name",  ports=16, mode="store-and-forward")   # qos="strict" (default) or "fifo"
                                                       # mac_table="learning" (default) or "topology"
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

Every egress queue serves the eight 802.1Q priority classes strictly (higher first, FIFO within a class). `qos="fifo"` makes a switch ignore frame priorities on its outbound ports.

By default a switch knows only its directly attached stations and learns the rest from source addresses. Until it has learned a destination, it floods frames for it to all other ports. In large networks this start-up flooding costs a lot of events and memory; without a `buffer`, every flooded copy waits in memory. `mac_table="topology"` starts the switch with the port towards every end station already known, i.e. in the converged state. Use it when you study steady-state behaviour rather than start-up:

```python
core = net.switch("core", ports=202, mac_table="topology")
```

**Bounded buffers.** By default egress queues are unbounded, so nothing is ever dropped and delay grows without limit under overload. `buffer` bounds each egress queue of a switch, in frame bytes ([ADR-0011](adr/0011-bounded-switch-buffers.md)):

```python
sw = net.switch("sw", ports=8, buffer=115_000)    # bytes per port
```

- **Tail drop.** A frame that would take the bytes waiting on its port beyond `buffer` is dropped on arrival, whatever its priority. The frame being transmitted does not count.
- **Effect.** The queueing delay of a port is capped at about `buffer / speed`, and frames are lost instead. `drops` and `drops_total` count them, and `frames_sent` and `frames_received` give a flow's loss.
- **Where.** The limit is per egress port, on switches only: end stations never drop a frame.
- **Validation.** `buffer` must be a whole number of bytes, at least 1522 and at least the largest frame of the scenario.

**Ingress limits and limits in frames** ([ADR-0013](adr/0013-ingress-buffer-accounting.md)). Many unmanaged switches bound what each ingress port may hold, and count frames rather than bytes. `ingress_buffer` bounds the frames that entered on each port and are still queued on any port, and `Frames(n)` gives a limit in frames, for either limit:

```python
from smolpy import Frames

sw = net.switch("sw", ports=8, ingress_buffer=Frames(60))
sw = net.switch("sw", ports=8, buffer=115_000, ingress_buffer=Frames(60))
```

- **Admission.** A frame whose ingress port's account is full is dropped, before any egress port is looked at (`ingress_drops_total`). Otherwise each copy is admitted by its egress port (`egress_drops_total`). `drops` and `drops_total` count both.
- **Release.** A frame leaves its ingress account when the transmitter takes its last queued copy. A flooded frame counts once.
- **The port that congests the switch loses its frames,** and another port's frames to the same egress port do not. With a limit in frames, a 64 B frame is dropped as often as a 1518 B one.
- **One unit per limit.** An `int` is bytes and `Frames(n)` is frames. `n` is a whole number of at least 1.

**Limits in cells, and a resume threshold** ([ADR-0014](adr/0014-buffer-cells-and-hysteresis.md)). Many switch chips store frames in fixed-size cells, and once a port's buffer is full they drop everything arriving on it until it has drained to a lower level:

```python
from smolpy import Cells

sw = net.switch("sw", ports=8, cell=128, ingress_buffer=Cells(740), ingress_resume=Cells(668))
```

- **Cells.** `Cells(n)` is a limit in cells of `cell` bytes. A frame takes ⌈bytes / `cell`⌉ of them: 1, 4 and 12 for 64 B, 512 B and 1518 B frames at `cell=128`.
- **The drop state.** `resume` and `ingress_resume`, in the unit of their limit, below it, are the resume levels. A frame that does not fit puts the account in the drop state if it holds more than that level. It then drops every frame, whatever its size, until a release brings it to the level. So losses come in runs, and small frames are lost with large ones.
- **Metrics.** `ingress_drop_episodes_total` and `egress_drop_episodes_total` count the runs of drops, so the mean run is drops over episodes. `ingress_drop_time_total` and `egress_drop_time_total` give the time spent in the drop state, in ms.
- **Tail drop** is the default, without a resume level. In frames, `resume = limit − 1` is tail drop too.
- **Validation.** `cell` is a whole number of bytes, required with a limit in cells and an error otherwise. A limit in cells holds the largest frame. A resume level is in its limit's unit: `int`, `Frames` or `Cells`, as the limit.

**A shared pool, reserves and a drain** ([ADR-0015](adr/0015-shared-pool-and-drain-resume.md)). Many switch chips have one packet memory for all ports, with a little guaranteed to each:

```python
sw = net.switch(
    "sw", ports=8, cell=128,
    pool=Cells(716), ingress_reserve=Cells(54), ingress_drain=Cells(72),
)
```

- **`pool`** is shared by the ingress ports, for what each holds beyond its **`ingress_reserve`**. A frame is admitted if it fits in its port's reserve, or else in the pool.
- **`ingress_drain`** ends a port's drop state once the switch has freed that much of its buffer, whatever port it came from. In the drop state the port's frames are dropped, even those that would fit in its reserve.
- **`pool_occupancy`** samples the pool's use.
- **Units and rules.** The ingress scope, `ingress_buffer`, `pool`, `ingress_reserve`, `ingress_resume` and `ingress_drain`, has one unit. `ingress_reserve` needs `pool`. `ingress_drain` needs `pool` or `ingress_buffer`, and excludes `ingress_resume`.

**One drop state for the whole switch** ([ADR-0016](adr/0016-switch-wide-drop-state.md)). On the testbed switch, two senders lose their frames together: any port's refusal stops every port that is beyond its reserve.

```python
sw = net.switch(
    "sw", ports=8, cell=128,
    pool=Cells(710), ingress_reserve=Cells(60), ingress_drain=Cells(72),
    ingress_drop="switch",
)
```

- **`ingress_drop="switch"`:** any ingress refusal starts the switch's drop state. In it a frame is admitted only if it fits in its own port's reserve, even if the pool has room. It ends once the switch has freed `ingress_drain`, and a refusal in it does not restart the count.
- **`"port"`,** the default, is a drop state per ingress port, as above. With one sender the two give the same results.
- **Rules.** `"switch"` needs `ingress_drain`.
- **Metrics.** With `"switch"`, `ingress_drop_episodes_total` counts the switch's entries into its drop state and `ingress_drop_time_total` its time in it, so drops over episodes is the sum of every port's run in an episode, not one port's run.

**The drain counted net** ([ADR-0017](adr/0017-drain-counted-net.md)). On the testbed switch, a light port's frames admitted in the drop state lengthen it: the state ends only once the occupancy, net of them, has fallen by the drain.

```python
sw = net.switch(
    "sw", ports=8, cell=128,
    pool=Cells(710), ingress_reserve=Cells(60), ingress_drain=Cells(72),
    ingress_drop="switch", ingress_drain_count="net",
)
```

- **`ingress_drain_count="net"`:** the switch's drop state ends at the release at which its ingress occupancy, summed over its ports, is `ingress_drain` below its value at the entry. That is what it has freed since the entry, less what it has admitted in the state. A refusal in the state still does not restart the count.
- **`"freed"`,** the default, is ADR-0016's rule above: the state ends once the switch has freed `ingress_drain`. With one sender the two give the same results, if nothing is admitted in the state (ADR-0016's condition).
- **Rules.** `ingress_drain_count` needs `ingress_drop="switch"`, either value.
- **Metrics** keep their meaning. With `"net"`, `ingress_drop_time_total` grows by the time the admitted frames add.

**The protection of a light port** ([ADR-0017](adr/0017-drain-counted-net.md), Part 2). On the testbed switch a light port is protected far beyond its reserve, while the heavy one is refused:

```python
sw = net.switch(
    "sw", ports=8, cell=128,
    pool=Cells(740), ingress_reserve=Cells(60), ingress_drain=Cells(72),
    ingress_drop="switch", ingress_drain_count="net",
    ingress_pool_count="total", ingress_alpha=24, ingress_mark=Cells(204),
)
```

- **`ingress_pool_count="total"`:** the pool's use is the switch's whole ingress occupancy less one reserve, `max(0, Σ occ − R)`, so the switch holds about P + R however its ports share it. `"excess"`, the default, is `Σ max(0, occ − R)`, each port's reserve set aside.
- **`ingress_alpha=α`:** outside the state, beyond its reserve, a frame of u units fits only while `occ + u ≤ R + α·(P − use)`, with the pool's use before the frame. A heavy port meets its threshold before the pool is full, so a light port's frame still finds room.
- **`ingress_mark=Cells(n)`:** at the entry of the state the refused port, and each port holding more than n, are marked. In the state a marked port is refused, even within its reserve; an unmarked one is admitted if its frame fits, with α, or is marked at its first misfit. The marks last until the state ends. Without a mark, the state admits a frame only within its reserve.
- **Rules.** Each needs `ingress_drop="switch"` and `ingress_drain_count="net"`; `ingress_pool_count` and `ingress_alpha` need `pool`.
- **With one sender,** without α, the results are ADR-0016's. With α the sender's limit moves: it holds at most `⌊(α(P + R) + R − u)/(1 + α)⌋` before a frame of u beyond its reserve, 769 cells for the values above.
- **On loss there is no retransmission** on SimPy and Rust. A poll whose request or response is lost stays unanswered (`modbus_unanswered`), or times out with `outstanding=1` and a timeout (below). On ns-3, Modbus and MQTT run over TCP, which retransmits ([Execution backends](backends.md#bounded-switch-buffers)).

## Traffic

```python
# Basic Ethernet send
src.sends(to=dst, rate=8_000, size=1_518, pattern="constant")

# Any end station sends flows: a Modbus server that also streams data (ADR-0013)
pi.sends(to=laptop, rate=8_100, size=1_518, pattern="poisson")

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
# ...waiting for each response, with a timeout of 1 s
plc.polls(meter, register=0, count=10, rate=100, outstanding=1, timeout_ms=1_000)

# PROFINET RT cyclic I/O (controller ↔ device), 4 ms update time
plc.controls(device, cycle=4, inputs=32, outputs=16, hold=3)
```

### `sends()` parameters

`sends()` is a method of every end station: `Adapter`, `ModbusSlave`, `MQTTBroker` and `ProfinetDevice` ([ADR-0013](adr/0013-ingress-buffer-accounting.md)).

| Parameter | Type | Description |
|---|---|---|
| `to` | end station | Destination: an adapter, Modbus slave, MQTT broker or PROFINET device |
| `rate` | float | Frames per second |
| `size` | int \| `"imix"` | Ethernet frame size in bytes, FCS included, at least 64; or Internet Mix distribution |
| `pattern` | str | `"constant"`, `"poisson"`, `"bursty"`, or `"batch"` |
| `delay_ms` | float | Simulation time before this flow starts (default 0) |
| `batch` | int \| str | Group size for `pattern="batch"`: `k`, `"geom:<mean>"`, or `"pareto:<shape>"` (default 1) |
| `priority` | int | 802.1Q priority (PCP) 0–7 (default 0) |

### `publishes()` parameters

| Parameter | Type | Description |
|---|---|---|
| `to` | MQTTBroker | Target broker |
| `topic` | str | MQTT topic string |
| `rate` | float | Messages per second (default 1.0) |
| `payload` | int | Payload bytes (default 20) |
| `qos` | int | 0 = fire-and-forget, 1 = PUBACK acknowledgement |
| `delay_ms` | float | Simulation time before publishing starts (default 0) |

### `polls()` parameters

| Parameter | Type | Description |
|---|---|---|
| `slave` | ModbusSlave | Target slave to poll |
| `register` | int | Starting holding-register address |
| `count` | int | Number of registers to read |
| `rate` | float | Polls per second (default 1.0) |
| `delay_ms` | float | Simulation time before polling starts (default 0) |
| `outstanding` | int \| None | `1`: at most one request of the pair awaits a response; `None`: unlimited (default) |
| `timeout_ms` | float \| None | Response timeout, with `outstanding=1` (default `None`: no timeout) |

**One request in flight** ([ADR-0012](adr/0012-modbus-one-request-in-flight.md)). By default a poll is open loop: a request goes out at every instant of its schedule, whatever happened to the previous one. `outstanding=1` models the usual Modbus TCP master, which waits for each response:
- **Skipped instants.** An instant that comes while a request is outstanding is skipped and counted in `modbus_skipped`. So is an instant at which the previous request completes. The schedule does not drift.
- **The timeout.** A request without a response after `timeout_ms` counts in `modbus_timeouts`, and the pair is free again. A response that comes later is discarded: it counts as a frame received, not as a response.
- **Without `timeout_ms`,** the master waits for ever. On SimPy and Rust, which do not retransmit, a lost request or response then stops the poll. On ns-3, TCP retransmits it ([Execution backends](backends.md#one-request-in-flight)).
- **Validation.** `outstanding` is `1` or `None`, and `timeout_ms` needs `outstanding=1`. A master can poll a given slave only once with `outstanding=1`.

The exact rules are in [Model Semantics](model-semantics.md#6-traffic-sources).

### `controls()` parameters

| Parameter | Type | Description |
|---|---|---|
| `device` | ProfinetDevice | Target IO device |
| `cycle` | int | Update time in ms: 1, 2, 4, … 512 |
| `inputs` | int | Input data bytes per frame (device → controller), 1–1440 |
| `outputs` | int | Output data bytes per frame (controller → device), 1–1440 |
| `hold` | int | Data hold factor: watchdog = `hold × cycle` (default 3) |
| `delay_ms` | float | Simulation time before cyclic exchange starts (default 0) |

### Traffic patterns

| Pattern | Description |
|---|---|
| `"constant"` | Fixed inter-frame gap — models a saturated link |
| `"poisson"` | Exponentially distributed gaps — models random/bursty traffic |
| `"bursty"` | Pareto-distributed burst lengths — models ON/OFF sources |
| `"batch"` | Compound Poisson: groups of `batch` frames arrive together at Poisson instants; `rate` stays the average frames/s, so the group size changes burstiness without changing the load |

With `pattern="batch"`, the group size is fixed (`batch=8`), geometric with a given mean (`batch="geom:4"`), or `floor` of a Pareto variable with a given shape (`batch="pareto:2.5"`; its second moment is infinite for shape ≤ 2). Example: `plc.sends(to=historian, rate=4_000, size=1_518, pattern="batch", batch=16)`.

### Frame sizes

| Value | Description |
|---|---|
| integer | Fixed size in bytes (e.g. `512`, `1_518`), at least 64 |
| `"imix"` | 40 % × 64 B, 57 % × 594 B, 3 % × 1 518 B |

A size is the Ethernet frame from the destination MAC to the FCS, as switch counters count it. A flow with a `priority` above 0 also carries a 4 B 802.1Q tag. On the link every frame also takes a preamble and SFD (8 B) and an inter-frame gap (12 B), so a link carries what real Ethernet carries ([ADR-0010](adr/0010-ethernet-frames-on-the-wire.md)).

## Observations

```python
net.observe(metric, on=node, every=interval_ms)
```

| Metric | Unit | Observed on |
|---|---|---|
| `throughput` | Mb/s | Adapter |
| `latency` | µs | Adapter |
| `bytes_sent` | MB | any end station (sender) |
| `bytes_received` | MB | Adapter (receiver) |
| `queue_depth` | frames | Switch |
| `utilization` | % | Any node |
| `broker_queue` | msgs | MQTTBroker |
| `modbus_latency` | µs | Adapter (master), ModbusSlave (its polls) |
| `pn_latency` | µs | Adapter (controller), ProfinetDevice |
| `pn_jitter` | µs | Adapter (controller), ProfinetDevice |
| `pn_watchdog_expiries` | count | Adapter (controller), ProfinetDevice |
| `drops` | frames | Switch: dropped by its buffers in the window, ingress and egress |
| `drops_total` | frames | Switch: dropped by its buffers so far |
| `frames_sent` | frames | any end station (sender) |
| `frames_received` | frames | Adapter (receiver) |
| `modbus_unanswered` | polls | Adapter (master), ModbusSlave (its polls): polls without a response so far, lost or in flight |
| `modbus_timeouts` | polls | Adapter (master), ModbusSlave (its polls): requests that timed out so far, with `outstanding=1` |
| `modbus_skipped` | polls | Adapter (master), ModbusSlave (its polls): poll instants skipped so far, because a request was outstanding |
| `ingress_drops_total` | frames | Switch: dropped by its ingress accounts so far |
| `egress_drops_total` | frames | Switch: dropped by its egress accounts so far |
| `ingress_drop_episodes_total` | episodes | Switch: runs of drops by its ingress accounts so far |
| `egress_drop_episodes_total` | episodes | Switch: runs of drops by its egress accounts so far |
| `ingress_drop_time_total` | ms | Switch: time its ingress accounts spent in the drop state so far |
| `egress_drop_time_total` | ms | Switch: time its egress accounts spent in the drop state so far |
| `pool_occupancy` | pool's unit | Switch: what its ingress ports hold beyond their reserves, in its shared pool |

## Simulation

```python
result = net.simulate(duration=30_000)              # headless — silent, fastest
result = net.simulate(duration=30_000, text=True)   # rich text dashboard in terminal
result = net.simulate(duration=30_000, live=True)   # full Dear PyGui desktop window
result = net.simulate(duration=30_000, seed=7)      # seed of the traffic generators (default 42)
result = net.simulate(duration=30_000, backend="rust")  # another execution backend

result.report()   # print summary table (avg / min / max per metric)
result.stats      # RunStats(backend="simpy", events=..., wall_s=...)
result.scenario   # the Scenario IR that was run
result.plot()     # open static dashboard for a completed result

# Export metric time-series (format inferred from extension)
result.export("results.csv")    # long CSV: time_ms, metric, value
result.export("results.json")   # JSON dict of lists-of-pairs
result.export("out.csv", format="csv")   # explicit format override
```

### Backends

`backend=` selects the execution backend, in `simulate()` and in `run_many()`:

| Backend | What it is |
|---|---|
| `"simpy"` | The reference implementation. It is the default, always installed, and runs the whole model, including the live and text dashboards. |
| `"rust"` | The native engine of [ADR-0008](adr/0008-rust-des-engine-mvp.md), from the optional package `smolpy-engine-rs` (`pip install smolpy[rust]`). It runs headless. |
| `"ns3"` | ns-3 ([ADR-0004](adr/0004-execution-backends.md)), the whole model. Ethernet (flows with every pattern, IMIX and 802.1Q priorities) runs on ns-3's IPv4/UDP stack, with random traffic drawn from SimPy's streams. PROFINET RT runs on layer 2. Both give SimPy's results bit for bit. Modbus TCP and MQTT run on ns-3's TCP, set to Linux's in how it recovers from a loss ([Execution backends](backends.md#modbus-and-mqtt-over-tcp)); its connection setup, acknowledgements and retransmissions make those results differ from SimPy's. It runs in the image `ghcr.io/jwszolek/smolpy:<version>-ns3`. Elsewhere it needs `NS3_HOME`, naming a built ns-3 3.49 tree, and a C++ compiler. It runs headless. |

- **What a backend can run is checked before the run.** Every backend declares what it supports: node kinds, traffic kinds, patterns and metrics. A scenario that uses anything else is rejected with a `ScenarioError` naming the element. A dashboard (`live=True` or `text=True`) on a headless backend is a `ValueError`.
- **A missing backend is an error.** When a backend is not installed, selecting it raises `BackendUnavailable` with the install command.
- **`smolpy.backends.installed()`** lists the backends that can run here.

**The Rust backend** runs the whole model:
- adapters, switches (`strict` and `fifo` QoS, `learning` and `topology` MAC tables, egress and ingress limits in bytes, frames or cells, with or without resume levels, a shared pool with reserves and drains, a drop state per port or for the whole switch, its drain counted freed or net, and the protection of a light port), links and flows between any end stations with every pattern (`constant`, `poisson`, `bursty`, `batch`) and with fixed or IMIX sizes;
- Modbus TCP: slaves, `polls()`, open loop or with one request in flight, and `modbus_latency`;
- MQTT: brokers, `publishes()` with QoS 0 and 1, `routes()` and `broker_queue`;
- PROFINET RT: devices, `controls()` and its watchdog, `pn_latency`, `pn_jitter` and `pn_watchdog_expiries`;
- every metric.

It runs headless: the live and text dashboards need SimPy.

- **Same results as SimPy.** Deterministic scenarios give SimPy's results bit for bit. Random traffic uses the same streams and distributions with the engine's own generator, so there the results agree statistically.
- **Install** it with `pip install smolpy[rust]`, or `pip install smolpy-engine-rs` next to an installed `smolpy`. It is released together with `smolpy`, with the same version, since v1.8.0. Wheels exist for Linux x86_64 and aarch64, macOS arm64 and x86_64, and Windows x86_64. To build it from the repository instead, with a Rust toolchain:

```bash
uvx maturin build --release -m rust/smolpy-engine-rs/Cargo.toml -o dist/
uv pip install dist/smolpy_engine_rs-*.whl
```

**Text mode** (`text=True`) displays a live updating table in the terminal — no display server or GUI toolkit required. Ideal for headless servers, SSH sessions, and CI environments.

## Many runs

`run_many` runs replications and parameter sweeps in parallel worker processes ([ADR-0009](adr/0009-batch-execution-and-cross-backend-validation.md)).

```python
from smolpy import run_many
from smolpy.stats import mean_ci

# Replications: the same network with seeds 42, 43, ... 71
batch = run_many(net, runs=30, duration=10_000)
batch.raise_on_failure()               # raise if any run failed
for result in batch.results():         # one SimulationResult per successful run
    print(result.scenario.run.seed, result.metrics["latency:plc"][-1])

# Sweep: a list of scenarios, each run with the same seeds
base = net.to_scenario(duration=10_000)
rates = [1_000, 2_000, 4_000, 8_000]
sweep = [base.with_flow_rate(r, source="bulk") for r in rates]
batch = run_many(sweep, runs=10, workers=8)
batch.results(3)                       # the runs of the fourth scenario
batch.report()                         # per metric: mean ± CI across runs, then failures
for i, rate in enumerate(rates):
    print(rate, mean_ci(batch.scalar("pn_jitter:plc", "max", scenario=i)))
```

A `.smol` file runs many times with `simulate ... runs=30` or `smolpy run plant.smol --runs 30 --workers 8 -o runs.csv`. See [SMOL Language Reference — simulate](language-reference.md#simulate-run-the-simulation).

### Sweep helpers

Each helper returns a changed copy of the scenario, so one base gives every point of a sweep:

| Helper | Changes |
|---|---|
| `with_flow_rate(rate, source=, destination=)` | rate of every flow between the given ends; a missing end matches any node |
| `with_cycle(cycle_ms, controller=, device=)` | cycle time of every PROFINET AR between the given ends |
| `with_link_speed(speed_mbps, between=(a, b))` | speed of the link between `a` and `b`, in either order, or of every link |
| `with_run(duration_ms=, seed=)` | run duration and/or seed |
| `with_traffic(kind, where, **changes)` | any field of the traffic statements of `kind` whose fields equal `where`, e.g. `with_traffic("poll", {"slave": "meter"}, rate=20)` |

- **Nothing matched is an error.** A selection that matches nothing raises `ValueError` and lists what there is, so a misspelt name never gives a sweep in which nothing varies. Unknown kinds and field names raise too.
- **Values are checked when the batch starts.** `run_many` validates every scenario before running any, so `with_flow_rate(-5)` raises `ScenarioError` before anything runs.
- **Random draws are shared.** A traffic source's random stream depends only on its kind and its ends ([Model Semantics §6](model-semantics.md#6-traffic-sources)). Every point of a sweep therefore starts from the same draws, which makes the differences between points less noisy.
- **Several scenarios in a report.** `report()` prints each scenario as `scenario i (name)`. The helpers keep the name, so use `dataclasses.replace(sc, name=...)` to tell the points apart.

### Statistics across runs

```python
from smolpy.stats import mean_ci

summary = batch.summary()              # same shape as result.metrics
for t, ci in summary["latency:plc"]:   # ci: MeanCI(n, mean, std, low, high, level)
    print(t, ci.mean, ci.half_width)

peaks = batch.scalar("pn_jitter:plc", "max")   # one number per run
print(mean_ci(peaks))                          # 95 % CI of the mean peak

batch.export("runs.csv")    # scenario, run, seed, time_ms, metric, value, error
batch.export("runs.json")   # every run with its metrics, or its traceback
```

- **`summary(scenario=0, level=0.95)`**: for every metric and sample time, the mean across the successful runs, the sample standard deviation and a confidence interval from Student's *t*.
    - A window metric such as `latency` has no sample for a window without frames, so `n` counts only the runs that have the sample.
    - With `n = 1` the standard deviation and the interval are NaN.
- **`scalar(metric, reduce="mean", scenario=0)`**: one value per successful run. `reduce` is `"mean"`, `"min"`, `"max"`, `"last"`, `"sum"` or a function of the run's values. A run without samples gives NaN.
- **`export(path, format=None)`**: CSV or JSON, from the extension unless `format` is given. A failed run is one CSV row with only its `error`.
- **`smolpy.stats`** (`mean_ci`, `diff_ci` for Welch's interval of a difference of two means, `t_quantile`) is plain Python, with no numpy or scipy.

| Parameter | Default | Meaning |
|---|---|---|
| `source` | — | a `Network`, a `Scenario`, or a list of scenarios |
| `runs` | `1` | runs per scenario |
| `seeds` | `base, base + 1, …` | one seed per run; *base* is the scenario's seed (42 for a `Network`) |
| `duration` | the scenario's | run length in ms; required for a `Network` |
| `workers` | one per CPU | worker processes; `1` runs everything in the calling process |
| `backend` | `"simpy"` | the only backend so far |

- **No `__main__` guard needed.** Each worker is a fresh `python -m smolpy._worker` process that receives the scenario as IR JSON, so your script is never imported again.
- **Invalid scenarios raise before anything runs.** A run that fails while running (an exception, or a worker process that dies) does not stop the batch: it is kept in `batch.runs` with its seed and traceback, and listed by `batch.failures`.
- **Results are the same as single runs.** Run *k* of a batch gives exactly what `simulate(seed=...)` gives with that run's seed, whatever the number of workers.

## Errors

Mistakes raise `ValueError` at the call that makes them, e.g. `a.sends(b, rate=10, priority=9)` or `net.observe("pn_jitter", on=switch, every=10)`. Rules about the whole network, such as the number of switch ports or a link to a node with traffic, are checked when `simulate()` starts. See [Scenario Model — Validation](scenario-model.md#validation) for the full list.

## Scenario IR

`net.to_scenario()` returns the network as a `smolpy.model.Scenario`, a declarative description that no engine depends on ([ADR-0007](adr/0007-execution-independent-scenario-model.md)). It can be saved as versioned JSON and archived next to results. See [Scenario Model](scenario-model.md).

```python
scenario = net.to_scenario(duration=30_000, seed=7)   # run section is optional
Path("scenario.json").write_text(scenario.to_json())
```
