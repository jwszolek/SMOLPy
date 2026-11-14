# Scenario Model

A `Scenario` is a complete description of one simulation: nodes, links, traffic, observations and the run configuration. It belongs to no engine. Both front ends produce it, and the engines will consume it ([ADR-0007](adr/0007-execution-independent-scenario-model.md)).

!!! note "Status"
    Migration step 2 of ADR-0007. `simulate()` lowers the network to a `Scenario`, and the SimPy engine runs that scenario. It no longer reads the `Network` object. Validation, the semantics contract and backend selection come in the next steps.

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

## Structure

`smolpy.model` holds frozen dataclasses. Nodes are referenced by **name**, so a scenario is a plain tree that can be compared with `==` and serialised directly.

| Field | Contents |
|---|---|
| `name` | network name |
| `nodes` | `AdapterNode`, `SwitchNode`, `HubNode`, `MqttBrokerNode`, `ModbusSlaveNode`, `ProfinetDeviceNode` |
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

Each node and traffic entry carries a `kind`: `adapter`, `switch`, `hub`, `mqtt_broker`, `modbus_slave`, `profinet_device`, `flow`, `publish`, `route`, `poll` or `profinet_ar`. A batch size is stored as `{"kind": "fixed" | "geom" | "pareto", "param": …}`.

**Reading is strict.** `from_json()` raises `ValueError` for:
- a different `format`;
- an unknown `version`;
- an unknown `kind` or field;
- a missing required field.

Optional fields that are left out take their defaults. Under ADR-0007, a new optional field does not change the version, and any change in meaning does.
