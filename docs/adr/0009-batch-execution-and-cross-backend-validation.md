# ADR-0009: Batch Execution and Cross-Backend Validation

**Status:** Accepted  
**Date:** 2026-11-13  
**Depends on:** ADR-0007 (Scenario IR). Used by ADR-0008 (Rust) and ADR-0004 (ns-3).

---

## Context

A single simulation run is rarely enough:

- **Replications.** Scenarios with random traffic (Poisson, bursty, batch, `imix`) need many runs with different seeds to report a mean with a confidence interval rather than one sample.
- **Parameter sweeps.** Typical questions are "how does PROFINET jitter change as bulk load goes from 10 % to 100 %?" or "which cycle time still has no watchdog expiries?". Answering them means running the same scenario with one parameter varied.
- **Cross-backend comparison.** Once there is more than one backend (ADR-0004, ADR-0008), the same scenarios must be run on each and the results compared, both for correctness and for performance.

Today none of this is supported:
- `simulate()` runs once;
- `.smol` has a `seed`, but no way to request replications;
- there is no aggregation across runs;
- there is no harness comparing two engines.

Users write their own loops, run everything sequentially on one core, and compute statistics by hand.

All three needs are the same mechanism: **run many independent Scenarios and aggregate the results**. They are independent of which backend executes the runs, and they are useful with SimPy alone. That is why this ADR comes before the Rust engine in the roadmap.

---

## Decision

### Python API

```python
from smolpy import run_many

# Replications: same scenario, different seeds
batch = run_many(net, runs=30, backend="simpy", workers=8)
batch.summary()            # per metric: mean, std, 95 % CI across runs
batch.export("runs.csv")   # long format with a run column: run, seed, time_ms, metric, value

# Parameter sweep: a list of scenarios (built from the IR, ADR-0007)
base = net.to_scenario()
scenarios = [base.with_flow_rate("host", "plc", r) for r in range(1_000, 10_001, 1_000)]
sweep = run_many(scenarios, runs=10, backend="rust")
```

- `run_many(source, runs=1, seeds=None, backend="simpy", workers=None)`:
    - `source` is a `Network`, a `Scenario`, or a list of `Scenario`s.
    - **Seeds:** by default the seeds are `base_seed, base_seed + 1, …`, so a batch is reproducible. Explicit `seeds=` overrides them.
    - **Workers:** `workers=None` uses all available cores.
- **Return value:** a `BatchResult` holding every individual `SimulationResult`, so nothing is lost, plus:
    - `summary()`: per metric key and window, the mean, standard deviation and a 95 % confidence interval (Student's *t* over runs);
    - `scalar(metric, reduce=...)`: one number per run (e.g. the max of `pn_jitter:plc`) for plots and sweeps;
    - `export()`: long CSV / JSON with `run` and `seed` columns.
- **Sweeps:** scenarios for sweeps are produced by copying and modifying the IR (`dataclasses.replace` plus a few convenience helpers such as `with_flow_rate`). The model stays declarative and backend-independent.

### `.smol` and CLI

```
simulate duration=10000 seed=1 runs=30
```

```bash
smolpy run plant.smol --runs 30 --workers 8 --backend rust -o runs.csv
```

- `runs` (default 1) and `--runs` request replications. `--workers` limits parallelism.
- With `runs > 1` the report prints the summary (mean ± CI) instead of one run's table.
- The `text` and `live` modes require `runs=1`.
- Sweeps stay in Python. `.smol` remains declarative, without loops, as decided in ADR-0003.

### Execution per backend

| Backend | Parallelism |
|---|---|
| SimPy | One `python -m smolpy._worker` process per worker, fed over a pipe by one thread each. Each worker gets the Scenario as IR JSON once, so no builder objects are pickled. Not `ProcessPoolExecutor`: with `spawn` and `forkserver` it re-imports the user's script in every worker, which breaks scripts without an `if __name__ == "__main__"` guard. |
| Rust | One process; runs are executed on a Rust thread pool (rayon) with the GIL released for the whole batch. |
| ns-3 | One subprocess per run, limited by `workers`. |

A failing run does not abort the batch. It is recorded in `BatchResult` with its seed and error, and reported at the end.

### Cross-backend equivalence suite

The suite lives in `tests/equivalence/`. It runs a fixed set of reference scenarios on every installed backend and compares the results against SimPy, the reference implementation (ADR-0007).

| Scenario class | Comparison |
|---|---|
| **Deterministic** (constant patterns, fixed sizes, no randomness) | ~~Per metric window: \|a − b\| ≤ abs_tol + rel_tol·\|b\|. The tolerance only covers time representation (SimPy float µs vs Rust integer ns); default `rel_tol = 1e-6`, `abs_tol` = 1 ns-equivalent in the metric's unit.~~ *Amended by the ADR-0007 revision of 2026-11-14 ([integer nanoseconds](0007-execution-independent-scenario-model.md#simulated-time-integer-nanoseconds)):* every metric window must match exactly. The tolerance applies until ADR-0007 step 8. |
| **Stochastic** | 30 replications per backend. For each metric's per-run scalar, the 95 % confidence interval of the difference of means must contain 0, and the relative difference of means must be under 2 %. |
| **High-fidelity backends** (ns-3) | Low-load scenarios: same rules as stochastic. Loaded scenarios: differences are **reported, not asserted** (ADR-0004). Quantifying them is a research result. |

- Backends that are not installed are skipped.
- **CI:** SimPy against itself (a sanity check of the harness) and Rust against SimPy when the Rust package is available.

### Performance comparison

`python -m benchmarks compare` (extending ADR-0006) runs the benchmark scenarios on each backend and reports:

- **Single-run performance:** events/s, frames/s, wall time and peak RSS for one run at 100 k / 1 M / 10 M frames.
- **Batch throughput:** completed replications per hour for B2 at 1 M frames, with **the same core budget** for every backend:
    - SimPy runs *k* processes;
    - Rust runs *k* threads.
- **Fairness rules:**
    - same machine and the same scenario JSON;
    - same metric configuration (both metrics-off and metrics-on variants);
    - result hashes recorded;
    - medians over repetitions, as in ADR-0006.

Peak RSS for SimPy batches is reported per worker process and in total. Comparing one SimPy process to one Rust process with *k* threads would be misleading.

Results go to `docs/benchmarks.md`, next to the ADR-0006 baseline.

### Implementation steps

1. `run_many` and `BatchResult` with SimPy worker processes. Failed runs are recorded, invalid scenarios raise first.
2. `summary()`, `scalar()` and `export()`, with a Student's *t* quantile in plain Python (no new dependency).
3. `.smol` `runs=` and the CLI options `--runs`, `--workers`, `-o`.
4. Sweep helpers on `Scenario`, such as `with_flow_rate`.
5. The equivalence harness in `tests/equivalence/`, SimPy against itself.

The performance comparison (`benchmarks compare`) waits for a second backend.

---

## Consequences

**Positive**
- Replications with confidence intervals and parameter sweeps are built in, and they work immediately with SimPy.
- Multi-core execution for every backend through one API. SimPy alone gains roughly a ×cores speed-up for batches.
- A clear, automated definition of "the Rust engine is correct" (equivalence suite) and "the Rust engine is worth it" (fair performance comparison).
- The same harness covers ns-3 later, for the model-fidelity questions in ADR-0004.

**Negative / trade-offs**
- Batches can produce a lot of data. `BatchResult` keeps every run in memory unless the runs are exported in a streaming way. Large sweeps may need a `keep_runs=False` mode that keeps only summaries.
- Statistical equivalence tests are probabilistic. With 30 replications and a 95 % level, an occasional false failure is possible. The suite uses fixed seeds, so CI results are stable, but a seed change can flip a borderline case.
- Process-based parallelism for SimPy pays a start-up and serialisation cost per run, which is noticeable for very short runs.
- `.smol` gains `runs`, and the CLI gains `--runs` / `--workers`, so the language and CLI surface grows a little.
