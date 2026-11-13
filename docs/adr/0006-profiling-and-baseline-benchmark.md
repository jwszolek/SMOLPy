# ADR-0006: Profiling and Baseline Benchmark

**Status:** Proposed  
**Date:** 2026-11-13

---

## Context

SMOLPy is moving towards larger industrial networks and towards an optional native (Rust) engine (ADR-0008). Neither should start without numbers: today nobody knows how fast the SimPy engine is, where its time goes, or how much memory a long run needs.

Reading the current engine (`sim/engine.py`) already suggests where to look, but these are **hypotheses to measure, not conclusions**:

- **Several SimPy events per frame per hop.** Each hop costs:
    - a queue put and a queue get;
    - a transmission timeout;
    - a new process per frame for propagation (`env.process(self._propagate_and_deliver(frame))`), with its own initialisation event and timeout;
    - a store put at the destination.
- **One new `_Frame` object per hop.** The frame is copied on every delivery so that flooding does not share `in_port`.
- **Memory grows with the number of frames, not with the network size.**
    - Per-node counters (`_AdapterCounters.latency_us`, `modbus_rtt_us`, the PROFINET latency and jitter lists) keep *every* value for the whole run.
    - Samplers only read the newest slice of these lists, but nothing ever frees the old values.
    - At 10 M frames this alone may dominate peak memory.
- **One shared random generator.** Not a performance issue, but it affects benchmark reproducibility: adding one flow changes the random draws of every other flow. This is addressed in ADR-0007.

Without a baseline, a Rust engine cannot be justified, and its speed-up cannot be shown.

---

## Decision

### 1. Benchmark suite

A `benchmarks/` directory at the repository root holds the suite. It is not shipped in the PyPI package. Scenarios are defined **by the number of frames injected by traffic generators** (100 k, 1 M, 10 M), not by simulated time: the harness computes the simulated duration needed to reach the target frame count from the configured rates.

| Scenario | Topology | Purpose |
|---|---|---|
| **B1 — single path** | 1 sender → 1 switch → 1 receiver, constant-rate Ethernet flow | Pure per-frame engine cost, no protocol logic |
| **B2 — industrial cell** | 1 PLC, 10 PROFINET devices (2 ms cycle), 10 Modbus slaves, 1 MQTT broker, bulk Ethernet traffic, 1 switch | Mixed-protocol cost at realistic proportions |
| **B3 — large plant** | 50 access switches × 20 nodes (~1 000 nodes), 2-tier core, a mix of flows | Cost of network size: processes, MAC tables, channels |

Each scenario is run at **100 k, 1 M and 10 M frames**, in two variants:

- **metrics off** — no `observe()` statements; measures the engine alone;
- **metrics on** — a representative set of observations; measures realistic use, including the memory held by metric collection.

### 2. Measured quantities

| Quantity | How |
|---|---|
| Wall time | `time.perf_counter()` around `simulate()` only (model building measured separately) |
| Events processed | count of engine events. SimPy has no counter, so the harness uses an `Environment` subclass that counts `step()` calls. A native engine reports its own count. |
| Events / s, frames / s | events processed and frames delivered, divided by wall time |
| Peak memory | peak resident set size of the run (`resource.getrusage(...).ru_maxrss`; kB on Linux, bytes on macOS — normalised by the harness). The reference machine runs Linux; Windows has no `resource` module and is not a benchmark platform. |
| Model build time and memory | time and RSS increase for building the `Network`, reported separately from the run |

Rules for reproducible numbers:
- **One run per subprocess**, so peak RSS belongs to one scenario and allocator state does not carry over.
- **Repetitions:** 5 at 100 k and 1 M; 3 at 10 M. Report the median and the min–max range.
- **Fixed seed.** Each run's metric output is hashed and recorded, so a "faster" run that silently computes something different is caught.
- **Environment recorded with every result:** CPU model and core count, RAM, OS, Python and SimPy versions, SMOLPy git commit.
- **Time limit:** runs above a configurable wall-time limit (default 1 h) are reported as *timed out*, not left out.

### 3. Results and reports

- **Results** are JSON files in `benchmarks/results/<date>-<backend>-<host>.json`, one record per scenario, size, variant and repetition.
- **Baseline:** a summary table goes into `docs/benchmarks.md`. The first committed result set is the **baseline** that all later work is compared against.
- **Command:** `python -m benchmarks run [--scenario B1] [--frames 1M] [--backend simpy]`. Once ADR-0007 is in place, `--backend` selects the engine.

### 4. Profiling

Profiling runs separately from benchmarking, because profilers distort timing:

- **CPU:** `py-spy` (sampling profiler, low overhead) produces flame graphs for B1 and B3 at 1 M frames. `cProfile` provides function-level call counts.
- **Memory:** `tracemalloc` snapshots taken during a run, to find which structures grow with the number of frames.
- **Report:** `docs/benchmarks.md` gets a hotspot section listing the top CPU and memory consumers with their share of the total. Each hypothesis from the Context section is marked as confirmed or rejected.

### 5. Low-cost Python optimisations, measured one by one

Before any native code, the obvious Python-level fixes are tried. **Each is a separate change with its own benchmark delta.** Candidates:

- `__slots__` / `@dataclass(slots=True)` for `_Frame`;
- scheduling propagation as a timed delivery instead of starting a process per frame;
- avoiding the per-hop frame copy where the frame is not flooded;
- **streaming window aggregation** in samplers (keep only the current window), so memory no longer grows with run length.

Every optimisation must keep the metric-output hash unchanged, or explain why it changes (e.g. an intentional bug fix).

### 6. Decision gate for the Rust engine (ADR-0008)

After the optimisations, measure B1–B3 at 1 M and 10 M frames. **Rust proceeds only if optimised SimPy misses at least one of these targets:**

| Target | Value |
|---|---|
| B3 at 10 M frames, metrics on | completes in under 10 min on the reference machine |
| Peak memory, any scenario at 10 M frames | under 4 GB |
| Batch of 100 replications of B2 at 1 M frames | under 1 h on 8 cores |

These targets are deliberately concrete. They may be revised once the baseline is known, but any revision has to be recorded in this ADR.

---

## Consequences

**Positive**
- Performance work and the Rust decision rest on measurements, not assumptions.
- The baseline makes regressions visible: ADR-0007 must not slow SimPy down, and ADR-0008 must show its speed-up on the same scenarios.
- Published, reproducible numbers (environment and output hashes included) can be cited in articles.
- Streaming aggregation alone may remove the main memory problem for long runs, whatever backend is used.

**Negative / trade-offs**
- 10 M-frame runs on SimPy may take a long time; the full suite is not part of regular CI. CI runs only B1 at 100 k as a smoke test.
- Results depend on the machine. Only results from the same reference machine are compared directly.
- Counting SimPy events through an `Environment` subclass adds a small overhead to the measured runs (it applies to every repetition equally and is documented).
- Some optimisations change internal structure that ADR-0007 will rework anyway. Only measured, low-risk changes are worth doing before the IR exists.
