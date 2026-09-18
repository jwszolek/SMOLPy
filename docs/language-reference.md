# SMOL Language Reference

The `.smol` format is SMOLPy's external Network Description Language — a standalone way to describe a network topology, traffic, and a simulation run without writing Python. `smolpy run` executes `.smol` files directly, using the same simulation engine as the Python API (see [DSL Reference](dsl-reference.md) for the Python equivalent of every statement below).

```bash
uv run smolpy run topology.smol
uv run smolpy run topology.smol --text      # force text-mode dashboard
uv run smolpy run topology.smol -o out.csv  # export metrics after the run
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
switch <name> ports=<n> [mode=store-and-forward|cut-through]
```

`mode` defaults to `store-and-forward`.

### `hub` — Layer-1 hub

```
hub <name> ports=<n>
```

### `mqtt_broker` — MQTT message broker

```
mqtt_broker <name> ip=<ip>
```

### `modbus_slave` — Modbus TCP slave

```
modbus_slave <name> ip=<ip> unit_id=<n>
```

### `link` — physical connection between two nodes

```
link <a> -- <b> speed=<mbps> length=<metres>
```

### `flow` — Ethernet traffic (Adapter → Adapter)

```
flow <src> -> <dst> rate=<fps> [size=<bytes>|imix] [pattern=constant|poisson|bursty] [delay=<ms>]
```

`size` defaults to `512`, `pattern` to `constant`, `delay` to `0`.

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
poll <master> -> <slave> register=<addr> count=<n> [rate=<polls/s>] [delay=<ms>]
```

`rate` defaults to `1.0`, `delay` to `0`.

### `observe` — register a metric to sample

```
observe <metric> on <node> every=<ms>
```

| Metric | Unit | Valid on |
|---|---|---|
| `throughput` | Mb/s | Adapter |
| `latency` | µs | Adapter |
| `bytes_sent` | MB | Adapter |
| `bytes_received` | MB | Adapter |
| `modbus_latency` | µs | Adapter (Modbus master) |
| `queue_depth` | frames | Switch |
| `collision_rate` | /s | Hub |
| `broker_queue` | msgs | MQTTBroker |
| `utilization` | % | any node |

Observing a metric on the wrong node type (or a metric that doesn't exist) is a semantic error, caught before the simulation runs.

### `simulate` — run the simulation

```
simulate duration=<ms> [mode=headless|text|live]
```

`mode` defaults to `headless` (no dashboard, fastest). `text` shows a live terminal table; `live` opens the Dear PyGui desktop window. The `--text` CLI flag always overrides `mode` (same as `--text` does for Python scripts).

## Errors

Two error classes, both reported as `file:line: message`:

| Class | Raised for |
|---|---|
| `SMOLSyntaxError` | Malformed source: unknown keyword, missing `=`, unclosed `{`, missing required argument |
| `SMOLSemanticError` | Well-formed but invalid: undeclared node reference, duplicate node name, wrong node type for a statement (e.g. `poll` targeting a `switch`), invalid metric for a node type, missing or duplicate `simulate` statement |

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
