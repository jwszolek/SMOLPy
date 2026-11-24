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
- Ships as an optional package: `pip install smolpy[rust]`, which installs `smolpy-engine-rs`.
- Selected with `backend="rust"`.

**Exit criteria:**

- B1 runs at least 10× faster than optimised SimPy; otherwise the work stops (ADR-0008 stop criterion).
- Equivalence suite passes against SimPy.
- Research targets from ADR-0006 are met.
- Wheels are published for Linux, macOS and Windows.

**Status:** ADR-0008 accepted on 2026-11-15 and brought up to date: integer nanoseconds, IR version 2, frames on the wire (ADR-0010), bit-exact deterministic equivalence. Done (steps 1–7), released in v1.8.0.
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
    - Wheels on Linux, macOS and Windows: met. They are published since v1.8.0 and smoke-tested against SimPy at every build.

- Step 7: `rust-wheels.yml` builds abi3 wheels for Linux, macOS and Windows and smoke-tests them against SimPy. `publish.yml` publishes them with each release, and a manual run publishes to TestPyPI first.

**Phase 4 is done.** v1.8.0 published `smolpy-engine-rs`, so `pip install smolpy[rust]` installs it. Phase 5 then brought protocol parity.

## Phase 5: Rust protocol parity

Modbus TCP, MQTT and PROFINET RT in the Rust engine. These need no new architectural decisions, only work within ADR-0008.

**Exit criteria:** the Rust backend's capabilities equal SimPy's, apart from the live dashboard.

**Status: done.** The exit criterion is met: the Rust backend's capabilities equal SimPy's, apart from the live dashboard.
- **Modbus TCP.**
    - The Rust engine runs slaves, `polls()` and `modbus_latency`. It reproduces SimPy's poll generator and slave process, including requests flooded to other slaves and dropped there.
    - The new `polling` scenario is bit-exact with SimPy, down to the event count.
- **MQTT.**
    - The Rust engine runs brokers, `publishes()` with QoS 0 and 1, `routes()` and `broker_queue`. It reproduces SimPy's publish generator and broker: copies keep the publisher's origin time, QoS 1 is acknowledged with a PUBACK, and a topic nobody subscribes to is only acknowledged.
    - The new `messaging` scenario (two brokers, non-ASCII topics) and the existing `protocols` scenario (Modbus and MQTT) are bit-exact with SimPy, down to the event count.
- **PROFINET RT.**
    - The Rust engine runs devices, `controls()`, `pn_latency`, `pn_jitter` and `pn_watchdog_expiries`.
    - The watchdog waits on "a frame or a timeout", a SimPy `AnyOf` condition. The engine reproduces it event by event: the condition is an event of its own, and only an arrival processed before it counts. So the watchdog expires exactly when SimPy's does, also when a frame arrives in the same nanosecond as the timeout.
    - The new `cyclic` scenario (two controllers, three ARs, a watchdog that expires in some cycles) and the existing `priorities` scenario are bit-exact with SimPy, down to the event count.
- **Benchmarks.** B2, the plant with PROFINET, Modbus and MQTT, now runs on Rust as well: all four benchmarks give SimPy's output and metric hashes and event counts at 200 k frames. B2 runs about 34× faster there.

## Phase 6: ns-3 high-fidelity backend ([ADR-0004](adr/0004-execution-backends.md), revised)

A code-generated ns-3 scenario from the Scenario IR. Gives a realistic TCP/IP stack for Modbus and MQTT.

ns-3 cannot be installed with `pip`, so the backend ships as a container image, `ghcr.io/jwszolek/smolpy:<version>-ns3`. It holds the pinned ns-3 release built once, and CI runs the ns-3 equivalence suite in it ([ADR-0004 — Container images](adr/0004-execution-backends.md#container-images)). A lighter image with SMOLPy and the Rust engine, without ns-3, can be published from the first release of `smolpy-engine-rs`.

**Status: done.** All 3 steps are done, and so are the exit criteria. The ns-3 images are built for `arm64` too, on a native runner.
1. **The image without ns-3: done.** `ghcr.io/jwszolek/smolpy:<version>` holds SMOLPy and the Rust engine, for `linux/amd64` and `linux/arm64`. It is built, tested and pushed at each release.
2. **The ns-3 base image: done.** `ghcr.io/jwszolek/smolpy-ns3-base:3.49` holds ns-3.49, built once, for `linux/amd64` and `linux/arm64`, each on a runner of its own architecture.
3. **The ns-3 backend.**
    - **Ethernet: done.** `backend="ns3"` runs adapters, switches and flows with every pattern, IMIX and 802.1Q priorities, with the six Ethernet metrics, on ns-3's IPv4/UDP stack.
        - It runs over SMOL's own full-duplex Ethernet device, because ns-3's CSMA is half duplex (ADR-0004).
        - Random traffic is drawn from the reference engine's streams.
        - Every Ethernet scenario of the suite is bit-exact with SimPy. For the stochastic ones that holds with the same seed.
        - CI runs the backend in the base image.
    - **Modbus and MQTT: done.** They run over ns-3's TCP, with one persistent connection per master and slave, or per client and broker (ADR-0004).
        - Their frames have SMOL's sizes. What TCP adds is the handshake, acknowledgements and, under load, its flow and congestion control.
        - At low load, after the first messages, every Modbus round trip and MQTT delivery is SimPy's plus 2 ns. Tests check this to the nanosecond.
        - The suite checks that the TCP scenarios sample the same metrics at the same instants as SimPy, and reports how their values differ.
    - **PROFINET: done.** PROFINET RT runs on layer 2, on SMOL's Ethernet device, with the three PROFINET metrics.
        - Every PROFINET scenario of the suite is bit-exact with SimPy, watchdog expiries included.
        - A frame that arrives exactly at the watchdog's deadline is too late on both engines.
    - **The image: done.** `ghcr.io/jwszolek/smolpy:<version>-ns3` and `latest-ns3`, for `linux/amd64` and `linux/arm64`, hold the base image, SMOLPy and the Rust engine, and `smol-ns3` built in.
        - Each release builds it from the release's wheels.
        - Before it is pushed, CI runs a `.smol` file on ns-3 through the CLI, and the ns-3 suite against the SMOLPy installed in it.
    - **Under load: done.** `python -m benchmarks load` compares Modbus and MQTT on ns-3's TCP with SimPy on a plant cell whose uplink loads up to 150 % ([Execution backends](backends.md#modbus-and-mqtt-over-tcp)).
        - Below saturation they agree within 0.4 %.
        - At saturation, TCP's acknowledgements tip the link over.
        - Heavily overloaded, TCP's windows hold MQTT back while UDP takes the link: up to +43 % latency and −41 % delivered.

**Exit criteria:**

- Low-load scenarios agree with SimPy and Rust.
- Under load, the differences are quantified and documented.
- The `-ns3` image is published at each release, and CI runs the ns-3 suite in it.

## Bounded switch buffers ([ADR-0011](adr/0011-bounded-switch-buffers.md))

**Status: done.** A switch's `buffer` bounds each of its egress queues, in frame bytes, with tail drop, on all three backends.

1. **The scenario: done.** `buffer` in the Python API and `.smol`, validated. It is an optional IR field, written only when set, so the IR stays version 2.
2. **SimPy: done.** Tail drop, and the metrics `drops`, `drops_total`, `frames_sent`, `frames_received` and `modbus_unanswered`. One port matches M/D/1/K's blocking probability and mean time in system.
3. **Rust: done.** Bit for bit with SimPy, drops and event counts included.
4. **ns-3: done.** Ethernet and PROFINET bit for bit with SimPy, drops included. Modbus and MQTT run over TCP, which retransmits what the other engines lose ([Execution backends](backends.md#bounded-switch-buffers)).
5. **Documentation and an example: done.** [`example_bounded_buffer.smol`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_bounded_buffer.smol).

**Why.** A hardware testbed showed a capped RTT and loss at ρ ≈ 1, where unbounded queues predicted delays without limit. With a 115 kB buffer the model caps the RTT at 9.4 ms; the testbed measured about 9.8 ms. The model loses more UDP frames than measured (0.5 % against 0.09 % at ρ = 0.996); that is left open.

**Possible extensions:** a buffer pool shared by a switch's ports, limits per priority class, and a limit in frames.

## One Modbus request in flight ([ADR-0012](adr/0012-modbus-one-request-in-flight.md))

**Status: done.** A poll with `outstanding=1` keeps one request of its master–slave pair in flight, as the usual Modbus TCP master does, with an optional response `timeout`, on all three backends.

1. **The scenario: done.** `outstanding` and `timeout` in the Python API and `.smol`, validated. They are optional IR fields, written only when set, so the IR stays version 2.
2. **SimPy: done.** Skipped instants, MBAP transaction ids, the timeout, late responses discarded, and the metrics `modbus_timeouts` and `modbus_skipped`.
3. **Rust: done.** Bit for bit with SimPy, with and without loss.
4. **ns-3: done.** The same rules above ns-3's TCP, whose minimum RTO, clock granularity, delayed-ACK timeout and SYN timeout are now Linux's (`smolpy.ns3.TcpSettings`). Format 3 of `smol-ns3`.
5. **Documentation and an example: done.** [`example_modbus_timeout.smol`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_modbus_timeout.smol).

**Why.** On the testbed of ADR-0011 a lost Modbus response came back after about 215 ms: the master waits for each response, so TCP has nothing more to send and retransmits only after its timeout. ns-3 showed a fast retransmit after 30 to 48 ms instead, because its polls were open loop, and its minimum RTO of 1 s would have given about 1 s. With one request in flight and Linux's settings, ns-3 gives 210 to 217 ms. The share of polls affected is higher than measured (0.28–0.61 % against 0.18 %), as the model loses more frames than the testbed; that is left open.

**Possible extensions:**
- **The slave's processing time** (`response_delay`), the natural next step: a slave now answers at once, where a real one takes time to read its registers. With one request in flight, that time adds to every round trip and bounds the poll rate.
- A master that defers a poll instead of skipping it, reconnecting after a timeout, and windows of more than one request on a connection.

## Ingress buffer accounting ([ADR-0013](adr/0013-ingress-buffer-accounting.md))

**Status: done.** A switch can bound what each ingress port holds, as well as each egress port, and either limit can be in bytes or in frames. Every end station can send flows. On all three backends.

1. **The scenario: done.** `ingress_buffer`, and `Frames(n)` / `<n>frames`, validated. Optional IR fields, so the IR stays version 2.
2. **Flows between any end stations: done.** With `frames_sent` and `bytes_sent` on every station, and the Modbus metrics on a slave.
3. **SimPy: done.** Ingress accounts, the admission and release rules, and `ingress_drops_total` and `egress_drops_total`. One port with a frame limit matches M/G/1/K.
4. **Rust: done.** Bit for bit with SimPy.
5. **ns-3: done.** Ethernet and PROFINET bit for bit with SimPy, drops included. `smol-ns3` format 4.
6. **Documentation, the testbed and an example: done.** [`example_ingress_buffer.smol`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_ingress_buffer.smol).

**Why.** The testbed switch lost the responses of the server on the congesting port almost as often as its UDP frames, and none of another server's responses to the same port. One egress buffer in bytes can do neither. With 60 frames per ingress port, the model gives both on every engine.

**Possible extensions:**
- **A shared buffer pool with dynamic thresholds,** the next one, once a run with two sources measures how the switch shares its memory.
- **Per-port metrics:** the occupancy and drops of one port, which need a port address in `observe`.
- **Limits in cells,** and both units for one account.
- **Limits per priority class.**

## Buffer limits in cells, with a resume threshold ([ADR-0014](adr/0014-buffer-cells-and-hysteresis.md))

**Status: done.** A switch limit can be in cells of `cell` bytes, and each account can have a resume level: once full, it drops every frame until it has drained to that level. On all three backends.

1. **The scenario: done.** `cell`, `Cells(n)` / `<n>cells`, `resume` and `ingress_resume`, validated. Optional IR fields, so the IR stays version 2.
2. **SimPy: done.** Cells, the drop state, drop episodes and drop time. One port with a resume level matches the exact chain on (frames queued, drop state).
3. **Rust: done.** Bit for bit with SimPy.
4. **ns-3: done.** Ethernet and PROFINET bit for bit with SimPy. `smol-ns3` format 5.
5. **The testbed, calibrated: done.** `example_ingress_buffer.smol` with 740 cells resuming at 668, the burst tests and the batch case ([Execution backends](backends.md#cells-and-a-resume-threshold)).

**Why.** Burst tests with 512 B and 64 B frames showed that the testbed switch counts 128 B cells, not frames. Its losses come in runs of 6.4–6.7 frames, never one, and take small frames with them, which tail drop cannot do.

**Still open:**
- **Whether the LS108GP also limits its egress port, or shares a pool:** a testbed run with two Pis sending to the laptop at once is planned. Then a shared pool with dynamic thresholds, if the run shows one.
- **Per-port metrics,** and a per-run metric: the run distribution now needs an audit of SimPy.
- **A per-frame header in cells,** and limits per priority class.

## Candidates (not yet scheduled)

- PROFINET IRT / TSN scheduling.
- PROFIBUS with a PROFINET gateway.
- EtherNet/IP and OPC UA.
- OMNeT++/INET backend.
- Modbus write function codes, and the slave's processing time (`response_delay`, above).
