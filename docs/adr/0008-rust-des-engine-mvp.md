# ADR-0008: Rust DES Engine (MVP)

**Status:** Accepted  
**Date:** 2026-11-13; accepted 2026-11-15 and brought up to date with ADR-0007 (revision of 2026-11-14), ADR-0009 and ADR-0010  
**Depends on:** ADR-0006 (benchmark targets, decision gate with research targets), ADR-0007 (Scenario IR, semantics contract), ADR-0010 (Ethernet frames on the wire)

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

This ADR applies **only if the ADR-0006 decision gate is met**, i.e. if optimised SimPy misses the performance targets. The original targets were met by optimised SimPy (ADR-0006 §6, evaluation of 2026-11-13). The case for Rust rests on the **research targets** added there: B4 (10 000 nodes) at 100 M frames, and 1 000 replications of B3. Both must first be measured on SimPy. *They were, on 2026-11-13, and optimised SimPy missed both: B4 took 106 min against 30, and 1 000 × B3 about 3.2 h against 30 min ([Benchmarks](../benchmarks.md#research-targets)). The gate is open.* The engine is an optional accelerator for research use; SimPy remains the reference implementation. It also requires the Scenario IR (ADR-0007), so that the Rust engine reads exactly the same model as SimPy and is checked against the same semantics contract.

### Options considered

**Language / approach**
- **A — Cython or Numba acceleration of the existing engine.** Keeps one codebase, but the SimPy event model (Python generators) is itself the bottleneck, so the achievable speed-up is limited.
- **B — C++ engine.** Fast, but bindings, memory safety and packaging cost more than with Rust, and the team has no C++ module to build on.
- **C — Rust engine with PyO3 bindings.** **Chosen.**
    - Fast, memory-safe, easy to parallelise.
    - PyO3 + maturin is the standard way to ship Python extension modules written in Rust.

**Distribution**
- **A — one `smolpy` package that includes the native extension.** Everyone needs a platform-specific wheel. Platforms without one would need a Rust toolchain to install SMOLPy at all.
- **B — separate optional package `smolpy-engine-rs`, installed with `pip install smolpy[rust]`.** **Chosen.** *(Until the first release of `smolpy-engine-rs` it is installed as `pip install smolpy-engine-rs`. The `smolpy[rust]` extra is added once the package is on PyPI, because `uv.lock` cannot refer to a package that does not exist yet. Both were done with v1.8.0.)*
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
│       ├── scenario.rs       # deserialises Scenario IR JSON (format "smol-ir", version 2)
│       ├── timebase.rs       # the time rules of Model Semantics §1 (ADR-0007 revision)
│       ├── frames.rs         # frame sizes and wire overhead (ADR-0010)
│       ├── sim.rs            # event list, clock, main loop
│       ├── net/              # links, switches, NIC egress queues
│       ├── traffic.rs        # flow generators
│       └── metrics.rs        # streaming window aggregation
└── smolpy-engine-rs/         # PyO3 bindings, built with maturin → Python module smolpy_engine_rs
```

*As built in step 3:* links, switches, flow generators and metric samplers all live in `sim.rs`, which reproduces SimPy's event graph (below). `net/`, `traffic.rs` and `metrics.rs` are split out when Phase 5 makes the file large enough to need it.

`smol-engine` has no Python dependency, so it can be tested and benchmarked with `cargo test` / `cargo bench` on its own, and later reused by other front ends.

### Engine design

- **Time:** `u64` nanoseconds. This is exact, has no floating-point drift, and gives a deterministic order for simultaneous events.
    - *Since the ADR-0007 revision of 2026-11-14, SimPy uses the same clock.* The engine implements the rules of `smolpy.model.timebase` exactly, and `timebase.rs` is tested against the same vectors:
        - a JSON number means its shortest decimal value;
        - every quantity is computed exactly and rounded once, ties to even;
        - periodic instants are `start + round(k · period)`;
        - metric values are one rounded quotient of integers.
- **Frames on the wire** follow ADR-0010:
    - frame sizes from destination MAC to FCS, at least 64 B, with an 802.1Q tag above priority 0;
    - a preamble and SFD before every frame and an inter-frame gap after it.
- **Order of simultaneous events.** Events at the same instant are processed in the order they were scheduled ([Model Semantics §9](../model-semantics.md#9-simultaneous-events)).
    - The engine must also schedule them in the same order as SimPy: start-up order, a sample before a frame arriving at the same instant, a batch group in draw order.
    - Bit-exact equivalence depends on this more than on anything else. Every case the equivalence suite finds is either written into §9 or fixed as a bug.
    - *As built in step 3:* the engine reproduces SimPy's event graph exactly as `smolpy.sim.engine` creates it.
        - Events are keyed `(time, priority, sequence)`. Process starts are URGENT and everything else NORMAL, as in SimPy.
        - A store `put` is an event of its own, and a waiting `get` is triggered when that event is processed. A `get` on a non-empty store is scheduled at once.
        - Processes are created in SimPy's order.

      It therefore even processes the same number of events: B1 at 200 k frames gives 2 600 156 in both engines, with the same output hash.
- **Event list:** a binary heap keyed by `(time, sequence)`. The sequence number makes the processing order of same-time events deterministic. If ADR-0006 profiling shows that heap operations dominate, a calendar queue can replace the heap later without changing behaviour.
- **Frames:** small structs held in an arena and referenced by index. No copy per hop; the information that varies per hop (ingress port) lives in the event, not in the frame.
- **Links and egress queues:** each link direction has 8 priority classes, each a `VecDeque` of frame indices, and a "busy until" timestamp that includes the inter-frame gap. Transmission completion and delivery after propagation are ordinary scheduled events, with no per-frame process.
- **Switches:** a MAC table per switch (`HashMap<MacId, Port>`), pre-seeded from topology as in SimPy. MACs and node names are interned to integer ids when the scenario is loaded.
- **Randomness:** one generator per traffic source, seeded from the derived stream seed defined in ADR-0007. The generator is the engine's own: a small PCG or xoshiro implementation, so no `rand` dependency is needed. Its draws differ from SimPy's, which is why stochastic scenarios are compared statistically (ADR-0009).
- **Metrics:** streaming aggregation per sampling window (sum/count for means, running max, counters). Memory is constant in the number of frames, which is the main memory fix identified in ADR-0006, applied from day one.
- **Results:** the engine returns the same `(time_ms, value)` series per metric key, wrapped into a regular `SimulationResult`. `report()`, `export()` and `--output` work unchanged.

### Python integration

```python
result = net.simulate(duration=60_000, backend="rust")
```

```bash
smolpy run plant.smol --backend rust
```

- The backend is registered in the ADR-0004/0007 backend registry. When `smolpy_engine_rs` is not installed, selecting it raises a clear error that names the install command. *Implemented in step 2 as `smolpy.backends`.* Each backend declares its `Capabilities`. The Rust backend takes them from the engine module itself, so Python never claims more than the engine supports.
- `run()` serialises the Scenario to IR JSON, passes it to Rust and releases the GIL for the whole simulation.
- The live dashboard and text mode are **not supported** in the MVP; the Rust backend runs headless. This is declared in its capabilities, so `live=True` is rejected before the run.

### MVP scope

| In the MVP | Later (Phase 5 of the roadmap) |
|---|---|
| Adapters, switches (`strict` / `fifo` QoS), links | — *(hubs were removed from the model)* |
| Ethernet flows: constant, Poisson, bursty, batch; fixed sizes and `imix`; `priority` | MQTT (publish, route, broker) |
| Metrics: `throughput`, `latency`, `bytes_sent`, `bytes_received`, `queue_depth`, `utilization` | Modbus TCP |
| Seeded runs | PROFINET RT and its metrics |
| Headless mode | Text/live dashboards |

Anything outside this scope is rejected by the capability check with a message such as `rust backend does not support 'poll' (Modbus) yet`. Nothing is silently skipped.

### Correctness

- **Rust unit tests** in `smol-engine` cover each mechanism: queue discipline, MAC learning, flooding, timing arithmetic.
- **Cross-engine equivalence** against SimPy uses the harness from ADR-0009:
    - **Deterministic scenarios** (constant patterns, fixed sizes) must agree with SimPy ~~within a tight tolerance on every metric window. Differences come only from float µs (SimPy) versus integer ns (Rust).~~ *Amended by the ADR-0007 revision of 2026-11-14:* exactly, on every metric window. Both engines use integer nanoseconds and the same rounding rules ([ADR-0007](0007-execution-independent-scenario-model.md#simulated-time-integer-nanoseconds)).
    - **Stochastic scenarios** must agree statistically across replications.
- **Benchmark acceptance:** B1, B3 and B4 of the ADR-0006 suite, run with `--backend rust`, must meet the research targets defined there on the reference machine. B2 follows with protocol parity (Phase 5). *Met on 2026-11-15; see step 6.*

### Stop criterion

The MVP is built first for **B1 only** (one flow, one switch) and measured before the rest of the scope:

- **B1 at 10 M frames, metrics off**, must run **at least 10× faster** than optimised SimPy on the reference machine.
    - ~~(198 s, ADR-0006), so **under 20 s**~~ *Amended 2026-11-15:* the 198 s predate integer time and ADR-0010. SimPy is measured again on the current model at implementation step 4, on the same machine and in the same session as the Rust engine. The limit is a tenth of that.
- It must also pass the equivalence check against SimPy: B1 with metrics on must match bit for bit.

If it does not, work on the Rust engine **stops**. The result, with its measurements, is recorded in this ADR. A negative result is still a publishable finding: it bounds what a native engine buys for this model.

**Result (2026-11-15): go.** B1 at 10 M frames, metrics off, was run on the same machine (Intel Core Ultra 7 255H) in one session, with 3 repetitions per engine alternating:

| Engine | Run time (median of 3) | Events | Output hash | Peak RSS |
|---|---|---|---|---|
| SimPy | 200.8 s (197.0, 201.7, 200.8) | 129 999 996 | `9eda076fe235df25` | 38 MB |
| Rust | 4.93 s (4.70, 4.98, 4.93) | 129 999 996 | `9eda076fe235df25` | 38 MB |

- **Speed-up: 40.8×** against the required 10×; the limit was 20.1 s. The Rust time is the whole backend call, including the IR's JSON and building the model.
- **Bit-exact.** The output hash (event count and every metric sample) is identical. The equivalence suite passes bit for bit on B1, `line` and `switching` (flooding, learning, converged tables, FIFO QoS, priorities).
- **Memory.** The peak RSS is the Python process in both cases. The Rust engine adds nothing measurable at this size, because B1 holds only a few frames at a time.

Raw results: `benchmarks/results/2026-11-15-1255-rust-jaws-zbook-stop-criterion.json`.

### Implementation steps

*Added 2026-11-15.*

1. **Bring this ADR up to date and accept it.** *(Done.)*
2. **A backend registry in Python, no Rust yet.** *(Done: `smolpy.backends`.)*
    - A `Backend` protocol with `capabilities` (ADR-0007).
    - `backend=` on `Network.simulate()` and `run_many()`, and `--backend` on `smolpy run`.
    - The capability check before a run.
    - `rust` is registered and reports clearly when it is not installed.
3. **The Rust skeleton for B1:** *(Done; see the results under the stop criterion.)*
    - `smol-engine` with the IR reader, `timebase.rs`, `frames.rs` and the event loop;
    - adapters, switches, links and constant flows, with B1's metrics;
    - the PyO3 module, built with maturin.
4. **Stop criterion.** B1 at 10 M frames on both engines, and B1's bit-exact equivalence. The result, go or stop, is recorded here. *(Done: go, 40.8×.)*
5. **If go, the rest of the MVP scope:** random patterns, priorities and `qos`, MAC learning and flooding, `utilization`. *(Done.)*
    - **Already in step 3:** priorities, both `qos` modes, learning and converged MAC tables, flooding and `utilization`.
    - **Added in step 5:**
        - the `poisson`, `bursty` and `batch` patterns, with fixed, geometric and Pareto group sizes, and IMIX;
        - one xoshiro256** generator per traffic statement, seeded from the same SHA-256 stream seed as SimPy (checked against Python);
        - the distributions as Python's `random` module computes them.
    - **Result.** Against SimPy with 30 replications each, means agree within 0.7 % on `random-load` (Poisson, bursty, IMIX) and pass the comparison on the new `groups` scenario (all three batch sizes, IMIX, priorities). Deterministic scenarios stay bit-exact.
6. **The research targets B3 and B4** with `--backend rust`. *(Done, 2026-11-15: both are met. B4 at 100 M frames runs in 310 s against a 30 min limit, and 1 000 × B3 at 1 M frames in 413 s on 8 cores against 30 min. Details are in [Benchmarks](../benchmarks.md#research-targets-on-the-rust-engine-adr-0008-step-6).)*
7. **Wheels and CI,** as below. *(CI done early, with step 4: a `rust` job in `ci.yml` runs `cargo fmt`, `clippy -D warnings` and `cargo test`. It builds the module with maturin and runs the equivalence suite against SimPy, and it fails if the Rust backend is not installed, so the suite cannot skip it silently. Since 2026-11-15 every release also gets a GitHub Release, created by `github-release.yml` once the tag is on PyPI, with generated notes and the `smolpy` sdist and wheel.)*

    *Done on 2026-11-15, and released with v1.8.0. `smolpy` and `smolpy-engine-rs` 1.8.0 are on PyPI with wheels for all five platforms, and the GitHub Release carries every file:*
    - **`rust-wheels.yml`** builds one abi3 wheel per platform, for every Python from 3.10: Linux x86_64 and aarch64 (manylinux), macOS arm64 and x86_64, and Windows x86_64. It also builds an sdist.
        - The wheels the runner can execute (Linux x86_64, macOS arm64, Windows) are installed and smoke-tested: a small scenario must be bit-exact with SimPy.
        - It runs on every pull request that touches `rust/`.
    - **`publish.yml`** builds the engine with the release's version and publishes it to PyPI next to `smolpy`, then attaches its files to the GitHub Release. `smolpy` does not wait for the engine: if the engine fails to publish, `smolpy` is released all the same, and the release lacks only the engine's files.
    - **A test run.** Run by hand with `target: testpypi`, `publish.yml` publishes only the engine to TestPyPI, as a dev version after the latest tag.
    - **Versions.** The engine has `smolpy`'s version, set from the tag in both `pyproject.toml` and `Cargo.toml`. At import, `smolpy` also checks that the module reads the Scenario IR version it writes (`IR_VERSION`). A mismatch makes the backend unavailable, with a message naming both versions.
    - **Done by hand, once:** a pending trusted publisher for `smolpy-engine-rs` on PyPI (workflow `publish.yml`, environment `pypi`) and on TestPyPI (workflow `publish.yml`, environment `testpypi`).

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
- ~~**Cross-engine results agree statistically, not bit for bit** (ADR-0007), so they need tolerance-based comparison.~~ *Amended by the ADR-0007 revision of 2026-11-14:* deterministic scenarios agree bit for bit, and only stochastic ones are compared statistically, because the engines' generators differ.
