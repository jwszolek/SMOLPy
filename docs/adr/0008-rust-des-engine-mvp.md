# ADR-0008: Rust DES Engine (MVP)

**Status:** Proposed  
**Date:** 2026-11-13  
**Depends on:** ADR-0006 (benchmark targets, decision gate), ADR-0007 (Scenario IR, semantics contract)

---

## Context

The SimPy engine is the reference implementation and stays that way. But two needs are hard to meet in pure Python:

- **Large industrial networks.** Thousands of nodes and tens of millions of frames per run. In SimPy every frame on every hop is several Python-level events, each going through generator switching, heap operations and object allocation.
- **Many runs.** Replications for confidence intervals and parameter sweeps multiply the cost of one run by tens or hundreds.

A native engine written in Rust can remove most of this per-event overhead:
- events are compact structs in a binary heap, not Python generators;
- frames are stored without per-object allocation;
- there is no interpreter overhead per event;
- runs execute without holding Python's GIL, so independent simulations can use all cores in one process.

This ADR applies **only if the ADR-0006 decision gate is met**, i.e. if optimised SimPy misses the performance targets. It also requires the Scenario IR (ADR-0007), so that the Rust engine reads exactly the same model as SimPy and is checked against the same semantics contract.

### Options considered

**Language / approach**
- **A — Cython or Numba acceleration of the existing engine.** Keeps one codebase, but the SimPy event model (Python generators) is itself the bottleneck, so the achievable speed-up is limited.
- **B — C++ engine.** Fast, but bindings, memory safety and packaging cost more than with Rust, and the team has no C++ module to build on.
- **C — Rust engine with PyO3 bindings.** **Chosen.**
    - Fast, memory-safe, easy to parallelise.
    - PyO3 + maturin is the standard way to ship Python extension modules written in Rust.

**Distribution**
- **A — one `smolpy` package that includes the native extension.** Everyone needs a platform-specific wheel. Platforms without one would need a Rust toolchain to install SMOLPy at all.
- **B — separate optional package `smolpy-engine-rs`, installed with `pip install smolpy[rust]`.** **Chosen.**
    - `smolpy` stays pure Python and installs everywhere.
    - Its build and release pipeline stays unchanged.
    - The Rust backend simply reports *not installed* when absent.

---

## Decision

### Repository layout

```
rust/
├── Cargo.toml                # workspace
├── smol-engine/              # pure Rust DES library — no Python dependency
│   └── src/
│       ├── scenario.rs       # deserialises Scenario IR JSON (format "smol-ir", version 1)
│       ├── sim.rs            # event list, clock, main loop
│       ├── net/              # links, switches, NIC egress queues
│       ├── traffic.rs        # flow generators
│       └── metrics.rs        # streaming window aggregation
└── smolpy-engine-rs/         # PyO3 bindings, built with maturin → Python module smolpy_engine_rs
```

`smol-engine` has no Python dependency, so it can be tested and benchmarked with `cargo test` / `cargo bench` on its own, and later reused by other front ends.

### Engine design

- **Time:** `u64` nanoseconds. This is exact, has no floating-point drift, and gives a deterministic order for simultaneous events.
- **Event list:** a binary heap keyed by `(time, sequence)`. The sequence number makes the processing order of same-time events deterministic. If ADR-0006 profiling shows that heap operations dominate, a calendar queue can replace the heap later without changing behaviour.
- **Frames:** small structs held in an arena and referenced by index. No copy per hop; the information that varies per hop (ingress port) lives in the event, not in the frame.
- **Links and egress queues:** each link direction has 8 priority classes, each a `VecDeque` of frame indices, and a "busy until" timestamp. Transmission completion and delivery after propagation are ordinary scheduled events, with no per-frame process.
- **Switches:** a MAC table per switch (`HashMap<MacId, Port>`), pre-seeded from topology as in SimPy. MACs and node names are interned to integer ids when the scenario is loaded.
- **Randomness:** one generator per traffic source, seeded from the derived stream seed defined in ADR-0007.
- **Metrics:** streaming aggregation per sampling window (sum/count for means, running max, counters). Memory is constant in the number of frames, which is the main memory fix identified in ADR-0006, applied from day one.
- **Results:** the engine returns the same `(time_ms, value)` series per metric key, wrapped into a regular `SimulationResult`. `report()`, `export()` and `--output` work unchanged.

### Python integration

```python
result = net.simulate(duration=60_000, backend="rust")
```

```bash
smolpy run plant.smol --backend rust
```

- The backend is registered in the ADR-0004/0007 backend registry. When `smolpy_engine_rs` is not installed, selecting it raises a clear error that names the install command.
- `run()` serialises the Scenario to IR JSON, passes it to Rust and releases the GIL for the whole simulation.
- The live dashboard and text mode are **not supported** in the MVP; the Rust backend runs headless. This is declared in its capabilities, so `live=True` is rejected before the run.

### MVP scope

| In the MVP | Later (Phase 5 of the roadmap) |
|---|---|
| Adapters, switches (`strict` / `fifo` QoS), links | Hubs |
| Ethernet flows: constant, Poisson, bursty, batch; fixed sizes and `imix`; `priority` | MQTT (publish, route, broker) |
| Metrics: `throughput`, `latency`, `bytes_sent`, `bytes_received`, `queue_depth`, `utilization` | Modbus TCP |
| Seeded runs | PROFINET RT and its metrics |
| Headless mode | Text/live dashboards |

Anything outside this scope is rejected by the capability check with a message such as `rust backend does not support 'poll' (Modbus) yet`. Nothing is silently skipped.

### Correctness

- **Rust unit tests** in `smol-engine` cover each mechanism: queue discipline, MAC learning, flooding, timing arithmetic.
- **Cross-engine equivalence** against SimPy uses the harness from ADR-0009:
    - **Deterministic scenarios** (constant patterns, fixed sizes) must agree with SimPy within a tight tolerance on every metric window. Differences come only from float µs (SimPy) versus integer ns (Rust).
    - **Stochastic scenarios** must agree statistically across replications.
- **Benchmark acceptance:** the ADR-0006 suite, run with `--backend rust`, must meet the targets defined there on the reference machine.

### Build and release

- `maturin` builds the wheels.
- A separate GitHub Actions workflow builds them for:
    - Linux x86_64 and aarch64 (manylinux);
    - macOS arm64 and x86_64;
    - Windows x86_64.
- The workflow publishes `smolpy-engine-rs` to PyPI. Its version is kept in lock-step with `smolpy`: the same tag triggers both releases.
- The existing `ci.yml`, `release.yml` and `publish.yml` of the main package stay unchanged, apart from an extra CI job that runs the equivalence tests with the Rust backend.
- The Rust toolchain is needed only by contributors who work on the engine.

---

## Consequences

**Positive**
- **Faster single runs and much lower memory** for large networks. The target figures come from ADR-0006 and are verified on the same benchmark suite.
- **Many independent simulations in one process** on all cores, because runs do not hold the GIL (used by ADR-0009).
- **SMOLPy stays pure Python by default.** The native engine is opt-in and fails clearly when missing.
- **Rust gives an independent check of the model.** A second implementation of the semantics contract exposes ambiguities and bugs in both engines.

**Negative / trade-offs**
- **Two implementations of the model must stay equivalent.** Every new protocol or metric needs Rust work (or an explicit capability exclusion) plus equivalence tests.
- **Rust skills become a requirement** for engine work on the project.
- **More expensive releases:** a multi-platform wheel matrix, a second PyPI package, and lock-step versioning.
- **No live dashboard on the Rust backend in the MVP.** Rust is a batch and experiment engine; interactive use stays on SimPy.
- **Cross-engine results agree statistically, not bit for bit** (ADR-0007), so they need tolerance-based comparison.
