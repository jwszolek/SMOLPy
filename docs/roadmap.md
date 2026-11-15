# Roadmap: towards a multi-backend discrete-event simulation framework

SMOLPy started as a Python DSL on top of one SimPy engine. The goal is a **multi-backend discrete-event simulation (DES) framework**:

- **one model**, written in the Python API or in a `.smol` file;
- **several execution backends**, chosen per experiment:
    - **SimPy**, the reference implementation;
    - **Rust**, for large networks and many runs;
    - **ns-3**, when the network model needs higher fidelity;
- **one results format**, so results from different backends can be compared directly.

```
 Python API (Network) ─┐
                       ├─▶ Scenario IR (ADR-0007) ─▶ validator ─▶ backend ─▶ SimulationResult
 .smol file ───────────┘                                          ├─ SimPy  (reference)
                                                                  ├─ Rust   (ADR-0008)
                                                                  └─ ns-3   (ADR-0004)
                          batch runs, sweeps, cross-backend checks (ADR-0009)
```

Each step below is a separate Architecture Decision Record. Phases are ordered by dependency. **Every phase must meet its exit criteria before the next one starts.**

## Phase 0: protocol coverage (done)

Ethernet traffic patterns, MQTT, Modbus TCP, PROFINET RT with 802.1Q priority queues, the `.smol` language, seeded runs.
ADR-0001, 0002, 0003, 0005.

## Phase 1: measure ([ADR-0006](adr/0006-profiling-and-baseline-benchmark.md))

A reproducible benchmark suite at 100 k / 1 M / 10 M frames, measuring:

- events per second;
- frames per second;
- wall time;
- peak memory.

Profiling of the SimPy engine, then a round of low-cost Python optimisations, each measured separately.

**Exit criteria:**

- Baseline results are committed.
- Hotspot report is published.
- Decision gate for Rust is evaluated. Rust goes ahead only if optimised SimPy misses the targets defined in ADR-0006.

**Status:** done. Optimised SimPy met the original targets, so the gate was extended with research targets (B4 with 10 000 nodes, 1 000 replications of B3). Optimised SimPy misses both: B4 at 100 M frames takes 106 min against 30 min, and 1 000 × B3 takes ~3.2 h against 30 min. Phase 4 is therefore justified.

## Phase 2: separate the model from its execution ([ADR-0007](adr/0007-execution-independent-scenario-model.md))

- A serialisable, versioned Scenario IR, produced by both the Python API and `.smol`.
- A written semantics contract and a single validator.
- Per-traffic-source random number streams.
- The SimPy engine consumes the IR.

**Exit criteria:**

- All existing tests pass through the IR path.
- `Network.simulate()` and `smolpy run` both go through it.
- Benchmark results show no regression against Phase 1.

**Status:** done (steps 1–8).
- `Network.to_scenario()` and `interpret_to_scenario()` produce the IR, and it round-trips through JSON for every example and B1–B4.
- The SimPy engine runs the IR and reports `SimulationResult.stats`. The benchmark harness reads those statistics.
- B1–B4 give unchanged output hashes.
- Step 3: every semantic rule lives in `smolpy.model.validate`, and every run is validated first. Python API mistakes that used to be ignored silently now raise, e.g. a metric on the wrong node type, a second link on an end station, or more links than ports.
- Step 4: [Model Semantics](model-semantics.md) defines what every scenario element means, independently of the engine. `tests/test_semantics.py` checks it against SimPy. It lists five open issues, e.g. that `bursty` does not keep `rate` as its average.
- Step 5: every traffic statement has its own random stream, seeded from SHA-256 of the run seed and a stable source id. `bursty` now keeps `rate` as its long-run average. Both change the output of seeded runs with random patterns or IMIX sizes, once. B1–B4 are deterministic and give unchanged hashes.
- **Integer time** (ADR-0007 revision of 2026-11-14, steps 6–8): simulated time becomes integer nanoseconds in every engine, the same clock as ns-3 and Rust, so deterministic results can match exactly across engines.
    - Step 6 (done): the rules are written into [Model Semantics §1](model-semantics.md#1-time-and-units), §8 and §9, and implemented in `smolpy.model.timebase`. Validation now rejects a run over 2⁶³ − 1 ns and an `every` under 1 ns.
    - Step 7 (done): the SimPy engine runs on integer nanoseconds.
        - Events and sample times are unchanged in B1–B4. Values differ only in the last digits, e.g. float noise in `pn_jitter` of 10⁻¹¹ µs became exactly 0.
        - Run time is unchanged within noise ([Benchmarks](benchmarks.md#integer-nanoseconds-adr-0007-step-7)).
    - Step 8 (done): the equivalence suite compares deterministic scenarios exactly: the same sample times and values, bit for bit, with no tolerance. A change of one ulp in one value is a mismatch.

## Phase 3: many independent simulations ([ADR-0009](adr/0009-batch-execution-and-cross-backend-validation.md))

- Replications and parameter sweeps run in parallel.
- Results are aggregated with confidence intervals.
- Includes the cross-backend equivalence and performance comparison harness.
- Useful with SimPy alone, so it comes before Rust.

**Exit criteria:**

- `runs=` / `run_many()` are available with SimPy.
- Batch throughput scales with cores.
- The equivalence harness is in place, ready for a second backend.

**Status:** done (steps 1–5).
- Step 1: `run_many()` runs replications and sweeps on SimPy worker processes and returns a `BatchResult`. With 8 workers, 8 runs of B1 at 200 k frames take 6.5 s instead of 32.9 s, with identical results.
- Step 2: `BatchResult.summary()` gives the mean, standard deviation and Student's *t* confidence interval for every metric window. `scalar()` gives one number per run, and `export()` writes CSV or JSON. The *t* quantile is plain Python and matches scipy to 1e-11.
- Step 3: `.smol` gains `simulate ... runs=`, and `smolpy run` gains `--runs`, `--workers` and `-o` for batches. A batch prints `BatchResult.report()` (mean ± CI across runs, then failed runs) and exits with status 1 if a run failed. An unknown `simulate` argument is now an error instead of being ignored.
- Step 4: `Scenario.with_flow_rate()`, `with_cycle()`, `with_link_speed()`, `with_run()` and the general `with_traffic()` build the scenarios of a sweep. A selection that matches nothing is an error. The points of a sweep share their random streams.
- Step 5: the equivalence suite in `tests/equivalence/` compares every installed backend against SimPy. With SimPy alone it compares SimPy against itself in about 5 s.
    - **Scenarios.** Three deterministic scenarios are compared sample by sample, exactly (since ADR-0007 step 8). Two stochastic ones are compared with 30 replications per backend, using Welch's interval (`smolpy.stats.diff_ci`) and a 2 % limit. Together they cover every node kind, traffic kind and metric.
    - **Self-checks.** Tests check that the harness detects a 2 ns latency change, a 3 % load change and a slower link.
    - **False failures.** With 95 % per metric, two correct backends failed a stochastic scenario by chance about once in 10 seed blocks, since each scenario has 3–4 metrics. Since 2026-11-15 each metric gets the Bonferroni level `1 − 0.05/k`, and it is once in 40, measured over 20 blocks per scenario. A failure near the limit still deserves a second seed block before it counts as a difference.
- **Exit criteria:**
    - `runs=` and `run_many()` are available.
    - Batch throughput scales with cores: 8 workers are 5× faster.
    - The equivalence suite is ready for a second backend.
    - `benchmarks compare` waits for that backend, as ADR-0009 says.

## Before Phase 4: model revisions

Decided before the Rust engine, so that it implements the final model once:

- **[ADR-0010](adr/0010-ethernet-frames-on-the-wire.md): Ethernet frames on the wire (done).**
    - Frame sizes run from the destination MAC to the FCS, are at least 64 B, and carry an 802.1Q tag when the priority is above 0.
    - Every frame also takes a preamble and SFD (8 B) and an inter-frame gap (12 B) on the link, so a link carries what real Ethernet carries: 148 809 frames of 64 B per second at 100 Mb/s, not 195 312.
    - The IR is version 2.
- **ADR-0009, amended (done):** Bonferroni levels in the stochastic comparison.
- **ADR-0004, amended (done):** the ns-3 backend computes SMOLPy's own metric definitions. It does not use FlowMonitor's, and the ADR records how they differ.

## Phase 4: Rust DES engine, MVP ([ADR-0008](adr/0008-rust-des-engine-mvp.md))

- Native engine for the core model: links, switches with priorities, Ethernet flows, core metrics.
- Ships as an optional package (`smolpy[rust]`).
- Selected with `backend="rust"`.

**Exit criteria:**

- B1 runs at least 10× faster than optimised SimPy; otherwise the work stops (ADR-0008 stop criterion).
- Equivalence suite passes against SimPy.
- Research targets from ADR-0006 are met.
- Wheels are published for Linux, macOS and Windows.

**Status:** ADR-0008 accepted on 2026-11-15 and brought up to date: integer nanoseconds, IR version 2, frames on the wire (ADR-0010), bit-exact deterministic equivalence. Steps 1–6 of 7 are done.
- Step 2: `smolpy.backends` is the backend registry. `backend=` is available on `simulate()` and `run_many()`, and `--backend` on `smolpy run`. Each backend's capabilities are checked before the run, and the Rust backend reports clearly when it is not installed.
- Step 3: `rust/` holds the engine `smol-engine` and the Python module `smolpy-engine-rs`. The engine reproduces SimPy's event graph, so it gives SimPy's results bit for bit, down to the event count. It covers adapters, switches, links, constant flows and six metrics.
- **Step 4, the stop criterion: go.** B1 at 10 M frames runs in 4.93 s against 200.8 s on SimPy, 40.8× faster where 10× was required, with an identical output hash.

- CI: a `rust` job runs `cargo fmt`, `clippy` and `cargo test`, then the bit-exact equivalence suite with the Rust backend, on every push and pull request.

- Step 5: random patterns (`poisson`, `bursty`, `batch`) and IMIX in Rust. Each flow has its own generator, seeded from the same stream seed as SimPy. The engines agree statistically on random traffic and bit for bit on deterministic scenarios.

- **Step 6, the research targets: both met.**
    - B4 (10 201 nodes) at 100 M frames runs in 310 s, against a 30 min limit; SimPy took 106 min.
    - 1 000 × B3 at 1 M frames, on 8 cores, runs in 413 s, against a 30 min limit; SimPy took about 3.2 h.
    - B3 and B4 are bit-exact with SimPy.
- Releases: every tag on PyPI also gets a GitHub Release with generated notes and the package files.
- **Exit criteria:**
    - B1 10× faster: met, 40.8×.
    - Equivalence suite: met, bit-exact on deterministic scenarios and statistical on random ones.
    - Research targets: met.
    - Wheels on Linux, macOS and Windows: open, step 7.

Next is step 7: the `smolpy-engine-rs` wheels and their release.

## Phase 5: Rust protocol parity

Modbus TCP, MQTT and PROFINET RT in the Rust engine. These need no new architectural decisions, only work within ADR-0008.

**Exit criteria:** the Rust backend's capabilities equal SimPy's, apart from the live dashboard.

## Phase 6: ns-3 high-fidelity backend ([ADR-0004](adr/0004-execution-backends.md), revised)

A code-generated ns-3 scenario from the Scenario IR. Gives a realistic TCP/IP stack for Modbus and MQTT.

**Exit criteria:**

- Low-load scenarios agree with SimPy and Rust.
- Under load, the differences are quantified and documented.

## Candidates (not yet scheduled)

- Bounded switch buffers with frame loss.
- PROFINET IRT / TSN scheduling.
- PROFIBUS with a PROFINET gateway.
- EtherNet/IP and OPC UA.
- OMNeT++/INET backend.
- Modbus write function codes and slave processing time.
