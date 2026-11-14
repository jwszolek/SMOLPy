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

By default a switch knows only its directly attached stations and learns the rest from source addresses. Until it has learned a destination, it floods frames for it to all other ports. In large networks this start-up flooding costs a lot of events and memory. Queues are unbounded, so nothing is dropped. `mac_table="topology"` starts the switch with the port towards every end station already known, i.e. in the converged state. Use it when you study steady-state behaviour rather than start-up:

```python
core = net.switch("core", ports=202, mac_table="topology")
```

## Traffic

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

### `sends()` parameters

| Parameter | Type | Description |
|---|---|---|
| `to` | Adapter | Destination adapter |
| `rate` | float | Frames per second |
| `size` | int \| `"imix"` | Frame size in bytes, or Internet Mix distribution |
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
| integer | Fixed size in bytes (e.g. `512`, `1_518`) |
| `"imix"` | 40 % × 64 B, 57 % × 594 B, 3 % × 1 518 B |

## Observations

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

## Simulation

```python
result = net.simulate(duration=30_000)              # headless — silent, fastest
result = net.simulate(duration=30_000, text=True)   # rich text dashboard in terminal
result = net.simulate(duration=30_000, live=True)   # full Dear PyGui desktop window
result = net.simulate(duration=30_000, seed=7)      # seed of the traffic generators (default 42)

result.report()   # print summary table (avg / min / max per metric)
result.stats      # RunStats(backend="simpy", events=..., wall_s=...)
result.scenario   # the Scenario IR that was run
result.plot()     # open static dashboard for a completed result

# Export metric time-series (format inferred from extension)
result.export("results.csv")    # long CSV: time_ms, metric, value
result.export("results.json")   # JSON dict of lists-of-pairs
result.export("out.csv", format="csv")   # explicit format override
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
