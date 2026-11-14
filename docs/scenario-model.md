# Scenario Model

A `Scenario` is a complete description of one simulation: nodes, links, traffic, observations and the run configuration. It belongs to no engine. Both front ends produce it, and the engines will consume it ([ADR-0007](adr/0007-execution-independent-scenario-model.md)).

!!! note "Status"
    Migration step 4 of ADR-0007. `simulate()` lowers the network to a `Scenario`, which is validated and then run by the SimPy engine. What every element means is defined in [Model Semantics](model-semantics.md). Per-source random streams and backend selection come in the next steps.

## Producing a scenario

From the Python API:

```python
from smolpy import Network

net = Network("cell")
# ... adapters, switches, links, traffic, observations ...
scenario = net.to_scenario(duration=10_000, seed=7)
```

From a `.smol` file. The run configuration comes from its `simulate` statement, and the display mode is not part of the scenario:

```python
from pathlib import Path
from smolpy.lang import interpret_to_scenario

scenario = interpret_to_scenario(Path("examples/example_profinet.smol"))
```

Without `duration`, `to_scenario()` describes the network only, and `scenario.run` is `None`.

## Running a scenario

```python
from smolpy.sim.engine import run_simulation

result = run_simulation(Scenario.from_json(text))   # duration and seed from scenario.run
result.scenario     # the scenario that was run
result.stats        # RunStats(backend="simpy", events=..., wall_s=...)
```

A scenario read back from JSON gives exactly the same results as the `Network` it came from. The run fails with `ValueError` if the scenario has no `run` section or references an undeclared node.

## Validation

Every semantic rule lives in one module, `smolpy.model.validate`. Every engine runs `validate(scenario)` before simulating, so an invalid scenario never runs, and nothing is ignored silently. Errors are `ScenarioError`, a `ValueError` that names the element and records where it is in the scenario:

```
flow tx -> rx: priority 9 out of range 0-7
observe pn_jitter on sw: 'sw' is a Switch, expected Adapter or ProfinetDevice
link c -- sw: 'sw' has only 2 ports, all in use
```

**Rules about one element** are checked by the Python builder at the call that declares it, e.g. `sends()` or `observe()`:

| Element | Rules |
|---|---|
| nodes | IPv4 address; MAC address of six hex bytes; `ports` ≥ 1; switch `mode`, `qos`, `mac_table`; Modbus `unit_id` 0–255 |
| links | not to itself; `speed` > 0; `length` ≥ 0 |
| `Flow` | source and destination are adapters; `rate` > 0; `size` ≥ 1 or `imix`; known `pattern`; `priority` 0–7; a batch size exactly when `pattern="batch"` |
| `Publish` | adapter → MQTT broker; non-empty topic; `rate` > 0; `qos` 0 or 1 (QoS 2 is not modelled) |
| `Route` | broker → adapters, each subscriber once |
| `Poll` | adapter → Modbus slave; `register` 0–65535; `count` 1–125; `rate` > 0 |
| `ProfinetAR` | adapter → PROFINET device; `cycle` 1, 2, 4 … 512 ms; `inputs`/`outputs` 1–1440 B; `hold` 1–255 |
| observations | an implemented metric (`frame_loss` is not); a node type the metric applies to; `every` > 0 |
| run | `duration` > 0; integer `seed` |

Delays must be zero or positive.

**Rules about the whole scenario** are checked before the run starts:
- **Unique names and MACs.** Node names are unique, and so are end-station MAC addresses.
- **Links:**
    - every link references declared nodes;
    - an end station has at most one link;
    - a switch has no more links than `ports`;
    - the topology has no loops, because no spanning tree is modelled.
- **Traffic:**
    - every node a statement involves has a link;
    - source and destination are different nodes in the same connected part of the network;
    - a controller has at most one AR per device;
    - a broker routes each topic once.
- **Observations:** every observed node has a link, and no metric is observed twice on the same node.

A node without links and without traffic is allowed.

**Backend capabilities** are checked after validation. The SimPy backend does not model half-duplex links, so it rejects `duplex=False`.

## Structure

`smolpy.model` holds frozen dataclasses. Nodes are referenced by **name**, so a scenario is a plain tree that can be compared with `==` and serialised directly.

| Field | Contents |
|---|---|
| `name` | network name |
| `nodes` | `AdapterNode`, `SwitchNode`, `MqttBrokerNode`, `ModbusSlaveNode`, `ProfinetDeviceNode` |
| `links` | `Link(a, b, speed_mbps, length_m, duplex)` |
| `traffic` | `Flow`, `Publish`, `Route`, `Poll`, `ProfinetAR` |
| `observations` | `Observation(metric, target, every_ms)` |
| `run` | `RunConfig(duration_ms, seed)` or `None` |

**Order matters:**
- Link order defines each node's port numbers.
- `traffic` lists each source's statements in the order the engine starts them: PROFINET ARs, then flows, publishes and polls. Broker routes follow their broker.

**Units are the ones users give:** ms, m, Mb/s, bytes, and frames, messages or polls per second.

**Defaults are resolved when the scenario is built.** For example, a MAC address derived from the IP is stored explicitly. Quantities that follow from the model, such as wire frame sizes, are not stored.

## JSON

```python
text = scenario.to_json()            # indent=2 by default
same = Scenario.from_json(text)
assert same == scenario
```

```json
{
  "format": "smol-ir",
  "version": 1,
  "name": "cell",
  "nodes": [{"kind": "adapter", "name": "plc", "ip": "10.0.0.1", "mac": "02:00:0a:00:00:01"}, ...],
  "links": [{"a": "plc", "b": "sw", "speed_mbps": 100, "length_m": 5, "duplex": true}, ...],
  "traffic": [{"kind": "profinet_ar", "controller": "plc", "device": "io-1", "cycle_ms": 2, ...}, ...],
  "observations": [{"metric": "pn_latency", "target": "plc", "every_ms": 100}],
  "run": {"duration_ms": 10000, "seed": 42}
}
```

Each node and traffic entry carries a `kind`: `adapter`, `switch`, `mqtt_broker`, `modbus_slave`, `profinet_device`, `flow`, `publish`, `route`, `poll` or `profinet_ar`. A batch size is stored as `{"kind": "fixed" | "geom" | "pareto", "param": …}`.

**Reading is strict.** `from_json()` raises `ValueError` for:
- a different `format`;
- an unknown `version`;
- an unknown `kind` or field;
- a missing required field.

Optional fields that are left out take their defaults. Under ADR-0007, a new optional field does not change the version, and any change in meaning does.
