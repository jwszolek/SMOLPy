# SMOL Language Reference

The `.smol` format is SMOLPy's external Network Description Language — a standalone way to describe a network topology, traffic, and a simulation run without writing Python. `smolpy run` executes `.smol` files directly, using the same simulation engine as the Python API (see [DSL Reference](dsl-reference.md) for the Python equivalent of every statement below).

```bash
uv run smolpy run topology.smol
uv run smolpy run topology.smol --text      # force text-mode dashboard
uv run smolpy run topology.smol -o out.csv  # export metrics after the run
uv run smolpy run topology.smol --runs 30 --workers 8 -o runs.csv   # 30 runs, mean ± CI
uv run smolpy run topology.smol --backend rust   # another execution backend, headless
```

A `.smol` file is **declarative**: it cannot express loops, conditionals, or parameterised topologies. Scenarios that need those still require the Python API.

## Structure

Every `.smol` file contains exactly one `network` block:

```
network "network-name" {
    # ... statements ...
}
```

- Comments start with `#` and run to end of line.
- Statements can appear in any order **except** that a node must be declared before anything else references it by name (links, flows, observations, ...) — declarations are processed top to bottom.
- Exactly one `simulate` statement is required per file.
- Arrows (`->`, `--`) and commas must be surrounded by whitespace.
- Identifiers may contain letters, digits, underscores, and hyphens (e.g. `host-A`, `core-sw1`).

## Statements

### `adapter` — NIC / host / server

```
adapter <name> ip=<ip> [mac=<mac>]
```

### `switch` — Layer-2 switch

```
switch <name> ports=<n> [mode=store-and-forward|cut-through] [qos=strict|fifo] [mac_table=learning|topology] [buffer=<limit>] [ingress_buffer=<limit>] [cell=<bytes>] [resume=<limit>] [ingress_resume=<limit>] [pool=<limit>] [ingress_reserve=<limit>] [ingress_drain=<limit>] [ingress_drop=port|switch] [ingress_drain_count=freed|net] [ingress_pool_count=excess|total] [ingress_alpha=<α>] [ingress_mark=<limit>]
```

`mode` defaults to `store-and-forward`. `qos` defaults to `strict` (egress queues serve 802.1Q priorities strictly); `fifo` ignores frame priorities on this switch. `mac_table` defaults to `learning`, where the switch learns addresses and floods unknown destinations. `topology` starts it with the port towards every end station already known.

`buffer` and `ingress_buffer` bound the switch's buffer, per port ([ADR-0011](adr/0011-bounded-switch-buffers.md), [ADR-0013](adr/0013-ingress-buffer-accounting.md)):
- **`buffer`** bounds the frames queued on each egress port.
- **`ingress_buffer`** bounds the frames that entered on each port and are still queued on any port. A flooded frame counts once.

A frame that does not fit is dropped, whatever its priority, and the frame being transmitted does not count. The ingress limit is checked first, then each copy's egress limit. Without either, the queues are unbounded and nothing is dropped.

A `<limit>` is in bytes, in frames or in cells:
- **Bytes:** a number, with the optional suffixes `kB` (10³), `MB` (10⁶), `KiB` (2¹⁰) and `MiB` (2²⁰). It must come out as a whole number of bytes, at least 1522 and at least the largest frame of the scenario.
- **Frames:** `<n>frames`, for switches that count descriptors, whatever the frame size. `<n>` is a whole number of at least 1.
- **Cells:** `<n>cells`, for switches that store frames in cells of `cell` bytes ([ADR-0014](adr/0014-buffer-cells-and-hysteresis.md)). A frame takes ⌈bytes / `cell`⌉ cells, so at `cell=128` a 64 B frame takes 1 and a 1518 B frame 12. `<n>` must hold the largest frame of the scenario, and `cell` is required with a limit in cells and an error without one.

**A resume threshold.** `resume` (for `buffer`) and `ingress_resume` (for `ingress_buffer`) are a level in the limit's unit, below the limit ([ADR-0014](adr/0014-buffer-cells-and-hysteresis.md)):
- **The drop state.** When a frame does not fit and the account holds more than its resume level, the account enters the drop state. It then drops every frame it decides, small ones too, until a transmitter taking a frame brings it to the resume level.
- **Runs.** So a full port loses frames in runs, as many switches do.
- **Without a resume level** the limit is plain tail drop.

```
switch sw ports=8 buffer=115kB
switch sw ports=8 ingress_buffer=60frames
switch sw ports=8 buffer=115kB ingress_buffer=60frames
switch sw ports=8 cell=128 ingress_buffer=740cells ingress_resume=668cells
switch sw ports=8 cell=128 pool=716cells ingress_reserve=54cells ingress_drain=72cells
switch sw ports=8 cell=128 pool=710cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch
switch sw ports=8 cell=128 pool=710cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch ingress_drain_count=net
switch sw ports=8 cell=128 pool=740cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch ingress_drain_count=net ingress_pool_count=total ingress_alpha=24 ingress_mark=204cells
```

**A shared pool** ([ADR-0015](adr/0015-shared-pool-and-drain-resume.md)):
- **`pool`** is a buffer shared by all the ports of the switch. Each port fills its own **`ingress_reserve`** first, and the pool beyond it.
- **A frame is admitted** if it fits in its port's reserve, or else if the pool has room for the part beyond the reserve.
- **`ingress_drain`** ends a port's drop state once the switch has freed that much of its buffer since the drop state began. With a drain a port enters the drop state on any refusal, and drops even frames that would fit in its reserve.
- **Units.** `ingress_buffer`, `pool`, `ingress_reserve`, `ingress_resume` and `ingress_drain` are all in one unit.
- **Rules:**
    - `ingress_reserve` needs `pool`;
    - `ingress_drain` needs `pool` or `ingress_buffer`;
    - `ingress_drain` and `ingress_resume` exclude each other;
    - `ingress_buffer`, if set, still caps each port.

**One drop state for the whole switch** ([ADR-0016](adr/0016-switch-wide-drop-state.md)). `ingress_drop` is the scope of the ingress drop state:
- **`port`,** the default: each ingress port has its own, as above.
- **`switch`:** one for the switch. Any port's refusal starts it. In it, a frame is admitted only if it fits in its own port's `ingress_reserve`, so a light port keeps its frames while the others lose theirs. It ends once the switch has freed `ingress_drain`.
- **Rules:** `switch` needs `ingress_drain`, and so excludes `ingress_resume`. With one sender `switch` gives the same results as `port`.
- **Metrics.** With `switch`, `ingress_drop_episodes_total` counts the switch's entries into its drop state, and `ingress_drop_time_total` its time in it.

**The count of the drain** ([ADR-0017](adr/0017-drain-counted-net.md)). `ingress_drain_count` is how the switch's drop state counts its drain:
- **`freed`,** the default: it ends once the switch has freed `ingress_drain` since the entry, as above.
- **`net`:** it ends once the switch's ingress occupancy, summed over its ports, is `ingress_drain` below its value at the entry. A frame admitted in the state, within its port's reserve, delays the end by its units. A refusal in the state does not restart the count.
- **Rules:** `ingress_drain_count` needs `ingress_drop=switch`. With one sender `net` gives the same results as `freed` when ADR-0016's condition holds.

**The protection of a light port** ([ADR-0017](adr/0017-drain-counted-net.md), Part 2). Three parameters, each with `ingress_drop=switch` and `ingress_drain_count=net`:
- **`ingress_pool_count`:** how the pool's use is counted. `excess`, the default, is each port's occupancy beyond its reserve, summed. `total` is the switch's whole ingress occupancy less one reserve. A port within its reserve is admitted either way, but under `total` its units count against the pool.
- **`ingress_alpha=<α>`:** a dynamic threshold, a positive number. Outside the state, a frame of u units of a port holding `occ` fits beyond the reserve R only if, besides the pool's room, `occ + u ≤ R + α·(P − use)`, the pool's use taken before the frame.
- **`ingress_mark=<limit>`:** who is refused in the state, in the ingress unit, at least 1. At the entry the refused port is marked, and so is every port holding more than the level. In the state a marked port is refused, even within its reserve. An unmarked port is admitted if its frame fits as outside the state, or is marked and refused. Without it, a frame in the state is admitted only within its reserve.
- **Rules:** `ingress_pool_count` and `ingress_alpha` need `pool`.
- **With one sender** and no `ingress_alpha`, the results are ADR-0016's. With `ingress_alpha` the sender holds at most `⌊(α·(P + R) + R − u)/(1 + α)⌋` units before a frame of u beyond its reserve. That is 769 cells for the LS108GP's α = 24, P = 740 and R = 60, so 780 cells after a frame of 12.

### `mqtt_broker` — MQTT message broker

```
mqtt_broker <name> ip=<ip>
```

### `modbus_slave` — Modbus TCP slave

```
modbus_slave <name> ip=<ip> unit_id=<n>
```

### `profinet_device` — PROFINET IO device

```
profinet_device <name> ip=<ip>
```

### `link` — physical connection between two nodes

```
link <a> -- <b> speed=<mbps> length=<metres>
```

### `flow` — Ethernet traffic (end station → end station)

```
flow <src> -> <dst> rate=<fps> [size=<bytes>|imix] [pattern=constant|poisson|bursty|batch] [batch=<k>|geom:<mean>|pareto:<shape>] [delay=<ms>] [priority=<0-7>]
```

`<src>` and `<dst>` are any two end stations: adapters, Modbus slaves, MQTT brokers or PROFINET devices ([ADR-0013](adr/0013-ingress-buffer-accounting.md)). So a host that serves Modbus and streams data is one `modbus_slave` with a `flow`. A station other than an adapter ignores the frames it receives.

`size` is the Ethernet frame in bytes, FCS included, at least `64` ([Model Semantics §2](model-semantics.md#2-frames)). It defaults to `512`, `pattern` to `constant`, `delay` to `0`, `priority` (802.1Q PCP) to `0`. `batch` (default `1`) sets the group size of the `batch` pattern, in which groups of frames arrive together at Poisson instants while `rate` stays the average frames/s.

### `publish` — MQTT PUBLISH (Adapter → MQTTBroker)

```
publish <src> -> <broker> topic="<topic>" [rate=<msgs/s>] [payload=<bytes>] [qos=0|1] [delay=<ms>]
```

`rate` defaults to `1.0`, `payload` to `20`, `qos` to `0`, `delay` to `0`.

### `route` — MQTT topic subscription

```
route <broker> topic="<topic>" -> [<adapter>, <adapter>, ...]
```

Must appear before the corresponding `publish` statement is simulated (declaration order matters — the interpreter processes the file top to bottom, exactly like the equivalent Python calls).

### `poll` — Modbus TCP poll (Adapter master → ModbusSlave)

```
poll <master> -> <slave> register=<addr> count=<n> [rate=<polls/s>] [delay=<ms>] [outstanding=1|unlimited] [timeout=<ms>]
```

`rate` defaults to `1.0`, `delay` to `0`.

`outstanding` sets how many requests of the pair may await a response ([ADR-0012](adr/0012-modbus-one-request-in-flight.md)). It defaults to `unlimited`: a request goes out at every poll instant, whatever happened to the previous one.

`outstanding=1` models the usual Modbus TCP master, which waits for each response:
- **Skipped instants.** A poll instant that comes while a request is awaiting its response is skipped and counted in `modbus_skipped`. The schedule does not drift.
- **The timeout.** `timeout` is the response timeout in ms, and needs `outstanding=1`. A request still unanswered after `timeout` counts in `modbus_timeouts`, and the pair is free again. A response that comes later is discarded.
- **Without `timeout`,** the master waits for ever. On SimPy and Rust, which do not retransmit, a lost request or response then stops that poll.
- **One poll per pair.** A master can have only one `poll` with `outstanding=1` towards a given slave.

```
poll plc -> meter register=0 count=10 rate=100 outstanding=1 timeout=1000
```

### `control` — PROFINET RT cyclic I/O (Adapter controller → ProfinetDevice)

```
control <controller> -> <device> cycle=<ms> inputs=<bytes> outputs=<bytes> [hold=<n>] [delay=<ms>]
```

Sets up one Application Relationship: the controller sends `outputs` bytes and the device sends `inputs` bytes, each every `cycle` ms. `cycle` must be 1, 2, 4, … 512; `inputs` and `outputs` 1–1440; `hold` (watchdog = `hold × cycle`) 1–255, default `3`; `delay` defaults to `0`. At most one `control` per controller–device pair.

### `observe` — register a metric to sample

```
observe <metric> on <node> every=<ms>
```

| Metric | Unit | Valid on |
|---|---|---|
| `throughput` | Mb/s | Adapter |
| `latency` | µs | Adapter |
| `bytes_sent` | MB | any end station |
| `bytes_received` | MB | Adapter |
| `modbus_latency` | µs | Adapter (Modbus master), ModbusSlave (its polls) |
| `pn_latency` | µs | Adapter (PROFINET controller), ProfinetDevice |
| `pn_jitter` | µs | Adapter (PROFINET controller), ProfinetDevice |
| `pn_watchdog_expiries` | count | Adapter (PROFINET controller), ProfinetDevice |
| `queue_depth` | frames | Switch |
| `broker_queue` | msgs | MQTTBroker |
| `utilization` | % | any node |
| `drops` | frames | Switch: dropped by its buffers in the window, ingress and egress |
| `drops_total` | frames | Switch: dropped by its buffers so far |
| `frames_sent` | frames | any end station |
| `frames_received` | frames | Adapter |
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

Observing a metric on the wrong node type (or a metric that doesn't exist) is a semantic error, caught before the simulation runs.

### `simulate` — run the simulation

```
simulate duration=<ms> [mode=headless|text|live] [seed=<int>] [runs=<int>]
```

`mode` defaults to `headless` (no dashboard, fastest). `text` shows a live terminal table; `live` opens the Dear PyGui desktop window. The `--text` CLI flag always overrides `mode` (same as `--text` does for Python scripts).

`seed` sets the random seed of the traffic generators (Poisson, bursty and batch patterns, `imix` frame sizes). It defaults to `42`, so a run without `seed` is reproducible; vary it to repeat an experiment with independent random draws. Each `flow` gets its own random stream derived from `seed`, so adding or removing one flow does not change the draws of the others (see [Model Semantics](model-semantics.md#6-traffic-sources)).

`runs` (default `1`) repeats the simulation with seeds `seed`, `seed + 1`, … `seed + runs - 1`, in parallel worker processes ([ADR-0009](adr/0009-batch-execution-and-cross-backend-validation.md)):

```
simulate duration=10000 seed=1 runs=30
```

- **Report.** With more than one run, `smolpy run` prints a summary instead of one run's result. For every metric it shows the mean across runs of each run's mean, its 95 % confidence interval, and the lowest and highest run mean. Then it lists every failed run with its seed and error.
- **Mode.** `runs` above 1 needs `mode=headless`: `text` and `live` show a single run.
- **Options.**
    - `--runs N` overrides `runs`, and `--runs 1` runs once.
    - `--workers N` limits the number of worker processes. The default is one per CPU.
    - `-o FILE` writes every run as CSV or JSON, with `scenario, run, seed, time_ms, metric, value, error` columns (see [DSL Reference — Statistics across runs](dsl-reference.md#statistics-across-runs)).
- **Exit status.** `smolpy run` exits with status 1 if any run failed, after printing the report and writing `-o`.
- **No loops.** Parameter sweeps stay in Python (`run_many` with a list of scenarios), since `.smol` has no loops (ADR-0003).

## Errors

Two error classes, both reported as `file:line: message`:

| Class | Raised for |
|---|---|
| `SMOLSyntaxError` | Malformed source: unknown keyword, missing `=`, unclosed `{`, missing required argument, unknown `simulate` argument |
| `SMOLSemanticError` | Well-formed but invalid: undeclared node reference, duplicate node name, wrong node type for a statement (e.g. `poll` targeting a `switch`), invalid metric for a node type, out-of-range parameter (e.g. `control cycle=3`, `priority=9`, `qos=wfq`), missing or duplicate `simulate` statement, `runs` that is not a positive integer or comes with `mode=text`/`live`, and the topology rules: more links than `ports`, a second link on an end station, a loop, traffic to a node without a link |

`.smol` files and the Python API share the same rules ([Scenario Model — Validation](scenario-model.md#validation)). Errors about the scenario as a whole point to the statement concerned, e.g. the link that exceeds a switch's ports.

The CLI prints these as a clean error panel with no Python traceback.

## Worked example

A combined Ethernet + MQTT + Modbus topology — a PLC that both generates bulk Ethernet traffic and polls a Modbus sensor, a pair of hosts talking to a server, and an MQTT sensor publishing through a broker:

```
network "plant-network" {

    adapter host-A   ip=10.0.0.1
    adapter server   ip=10.0.0.10
    adapter plc      ip=10.0.0.20
    switch  sw1      ports=8 mode=store-and-forward
    mqtt_broker  broker1  ip=10.0.2.1
    modbus_slave sensor1  ip=10.0.3.1  unit_id=1

    link host-A  -- sw1  speed=1000  length=5
    link server  -- sw1  speed=10000 length=2
    link plc     -- sw1  speed=100   length=5
    link broker1 -- sw1  speed=1000  length=2
    link sensor1 -- sw1  speed=100   length=5

    flow host-A -> server rate=8000 size=1518 pattern=constant

    publish server -> broker1 topic="plant/temp" rate=1.0 payload=20 qos=1
    route   broker1 topic="plant/temp" -> [server]

    poll plc -> sensor1 register=40001 count=10 rate=1.0

    observe throughput      on server every=100
    observe queue_depth     on sw1    every=50
    observe modbus_latency  on plc    every=500

    simulate duration=10000 mode=text
}
```

Run it:

```bash
uv run smolpy run topology.smol --text
```

See [`examples/example.smol`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example.smol) for a full runnable `.smol` translation of [`example.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example.py), and [Examples](examples.md) for more scenarios.
