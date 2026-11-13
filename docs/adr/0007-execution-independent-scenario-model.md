# ADR-0007: Execution-Independent Scenario Model (Scenario IR)

**Status:** Proposed  
**Date:** 2026-11-13  
**Amends:** ADR-0004 (backends consume the Scenario IR instead of the `.smol` AST)

---

## Context

Today the description of a network and its execution are tangled together:

- **The Python API builds a model that only one engine understands.** `Network` builds live objects (`Adapter`, `Switch`, specs such as `TrafficSpec`, `ProfinetSpec`), and `sim/engine.py` reads them directly — including private attributes (`network._nodes`, `node._routes`).
- **Validation is spread over three places.** Some checks live in the Python API (`Adapter.controls()` cycle values, `sends()` priority range), some in the `.smol` interpreter (metric/node-type compatibility, declaration order), and some nowhere at all (the engine silently ignores an observation on the wrong node type when called from Python).
- **ADR-0004 attaches backends to the `.smol` AST** and keeps the Python API on SimPy only. This no longer fits. Large industrial networks — the main motivation for the Rust engine (ADR-0008) — are built programmatically in Python, with loops over cells and devices, which `.smol` deliberately cannot express. Those users must be able to choose a backend too.
- **Semantics exist only as code.** Frame-size formulas, queueing discipline, how a sampler aggregates its window, which sample falls on the simulation boundary — all of it is defined only by the SimPy implementation. A second engine has nothing to be checked against except that code.
- **One shared random generator** (`random.Random(seed)` in `run_simulation`) serves every traffic source in creation order. Adding or reordering one flow changes the random draws of all others, which breaks controlled experiments and makes cross-engine comparison impossible.

### Options considered

- **A — keep ADR-0004: backends consume the `.smol` AST.** Python-API users get no backend choice; generated topologies would have to be printed as `.smol` first.
- **B — backends consume the live `Network` object.** Every backend reverse-engineers intent from runtime objects, and those objects carry SimPy-specific state.
- **C — a separate, declarative, serialisable Scenario IR that both front ends lower into.** **Chosen.** The `.smol` AST already showed that a clean declarative description is possible; the IR generalises it and makes it the single contract between model and engines.

---

## Decision

### Layers

```
front ends            model                         execution
──────────            ─────                         ─────────
Python API ─┐
            ├──▶  smolpy.model.Scenario  ──▶  validate()  ──▶  Backend.run(scenario)  ──▶  SimulationResult
.smol ──────┘     (frozen dataclasses,                         simpy | rust | ns3
                   JSON-serialisable)
```

- **`smolpy.model`** (new package): frozen dataclasses describing a complete scenario.
    - `Scenario`: the top-level object, with fields `name`, `nodes`, `links`, `traffic`, `observations`, `run`.
    - **Node types:** `AdapterNode`, `SwitchNode`, `HubNode`, `MqttBrokerNode`, `ModbusSlaveNode`, `ProfinetDeviceNode`.
    - **Traffic types:** `Flow`, `Publish`, `Route`, `Poll`, `ProfinetAR`.
    - **Other:** `Link`, `Observation`, and `RunConfig` (duration, seed).
    - Nodes are referenced by **name**, never by object, so the IR is a plain tree with no cycles and can be serialised directly.
- **Python API:**
    - `Network` becomes a builder of the IR. Its public methods (`adapter()`, `sends()`, `controls()`, …) are unchanged.
    - `Network.to_scenario()` returns the IR.
    - `Network.simulate(duration, ..., backend="simpy")` gains the `backend` argument.
- **`.smol`:** the interpreter stops building a `Network` and lowers the AST straight to a `Scenario`. `smolpy run file.smol --backend <name>`.
- **Validation:** one module, `smolpy.model.validate`, holds every semantic rule:
    - references and declaration order;
    - node-type compatibility of statements and metrics;
    - value ranges: PROFINET cycle and data sizes, priority, `qos`, unit_id;
    - duplicate ARs, exactly one run configuration.

    Errors carry a source location when one is known (`.smol` line; for the Python API the builder call that created the element). The Python API and `.smol` raise the same rules: `ValueError` from Python, `SMOLSemanticError` from `.smol`.
- **Backends:** the `Backend` protocol of ADR-0004 takes a `Scenario`:

    ```python
    class Backend(Protocol):
        name: str
        capabilities: Capabilities      # supported node kinds, traffic kinds, metrics, live/text modes
        def run(self, scenario: Scenario) -> SimulationResult: ...
    ```

    A capability check runs after validation and rejects anything the chosen backend cannot execute, before any simulation starts.
- **SimPy engine:** `sim/engine.py` is refactored to read the `Scenario` instead of the `Network`'s private attributes. Its behaviour stays the same; this is checked against the ADR-0006 output hashes and the full test suite.

### Serialisation

- `Scenario` serialises to versioned JSON: `{"format": "smol-ir", "version": 1, ...}`, with `Scenario.to_json()` and `Scenario.from_json()`.
- **Why JSON:**
    - the Rust engine can receive a scenario without depending on Python object layouts;
    - the ns-3 code generator reads the same input;
    - the exact scenario can be archived next to published results.
- **Compatibility:**
    - A new **optional** field does not bump the version.
    - Any change in meaning does bump it.
    - Backends reject versions they do not know.

### Semantics contract

A normative document, `docs/model-semantics.md`, defines what every IR element means independently of any engine:
- frame-size formulas for every protocol;
- egress queueing: 8 strict 802.1Q classes, FIFO within a class, non-preemptive, `qos=fifo` override;
- MAC learning and flooding rules;
- **timing:**
    - time units: integer nanoseconds as the canonical unit;
    - propagation constant: 2·10⁸ m/s;
    - when generators first fire;
- **metric definitions:** what each metric aggregates over a window, and the boundary rule that a sample at exactly `t = duration` is not taken (current SimPy behaviour);
- **ordering of simultaneous events**, to the extent it is observable.

The SimPy engine remains the **reference implementation**. Where the code and the document disagree, that is a bug in one of them, to be resolved explicitly rather than by whatever the code happens to do.

### Random number streams

- Each traffic source gets its **own random stream**, derived from `(run seed, stable source id)`. The source id is the source node name, the destination and the statement's index in that source's traffic list.
- Adding, removing or reordering one source no longer changes the draws of the others.
- The **seed derivation** is specified in the semantics contract and is the same for every engine: the stream seed is the first 8 bytes of SHA-256 of `"<run seed>:<source id>"`. Each engine seeds **its own** generator from it — SimPy uses `random.Random`; Rust uses a native generator (e.g. PCG64). No new Python dependency is needed.
- **Cross-engine target: statistical equivalence, not bit-identical output.** Bit-identical results would require the same generator, the same sampling algorithms and the same tie-breaking of simultaneous events in every engine. That is left open, not promised; within one engine, a given seed is fully reproducible.
- **Note:** per-source streams change the exact numbers produced by existing seeded scenarios that use random patterns. This is an intentional, one-time break, recorded in the changelog and in the ADR-0006 baseline hashes.

### Migration

1. **Introduce the IR.**
    - Add `smolpy.model` and `Network.to_scenario()`.
    - The engine still reads `Network` internally.
    - Tests: round-trip `Network` → `Scenario` → JSON → `Scenario`.
2. **Switch the SimPy engine to read the IR.** Refactor, then verify against the ADR-0006 benchmark hashes and the full test suite.
3. **Move validation into `smolpy.model.validate`.** Python API errors that used to be silent (e.g. observing `pn_jitter` on a switch) now raise.
4. **Make `.smol` lower straight to the IR.**
5. **Introduce per-source random streams.** A deliberate output change, with refreshed baselines.

Each step ships separately and keeps the public API unchanged, except for the new `backend=` argument and the stricter validation in step 3.

### Amendment to ADR-0004

ADR-0004's "Where backends attach" decision changes from *Option B (the `.smol` AST)* to the Scenario IR, and its statement that "the Python API stays SimPy-only" no longer applies. Everything else in ADR-0004 is unchanged: the ns-3 code-generation approach, the mapping table, results interchange and capability checks.

---

## Consequences

**Positive**
- Python-API and `.smol` users get the same backends, validation and error messages.
- The semantics contract makes a second engine verifiable, and documents the model itself, which is useful for publications.
- Scenarios become data: they can be saved, compared, generated by other tools, and archived with results.
- Per-source random streams make controlled experiments possible: changing one source no longer changes the randomness of the others.
- The SimPy engine loses its dependency on private builder attributes.

**Negative / trade-offs**
- A substantial refactor of the core: builder, interpreter and engine all change, even though the public API stays.
- Two representations to keep in sync, the builder objects and the IR. The builder must stay a thin layer over the IR.
- Stricter validation turns some previously silent Python-API mistakes into errors, which is a visible behaviour change.
- Per-source random streams change the output of existing seeded scenarios that use random patterns, once.
- The semantics contract must be maintained along with every model change, or it loses its value.
