# Benchmarks

Performance baseline and profiling of the SimPy engine, as defined in [ADR-0006](adr/0006-profiling-and-baseline-benchmark.md). This page is the reference point for later performance work: optimisations (ADR-0006 §5), the Scenario IR refactor (ADR-0007) and the Rust engine (ADR-0008) are all measured against it.

## Running the suite

The suite lives in `benchmarks/` at the repository root. It is not part of the PyPI package.

```bash
# full suite: B1-B3 × 100k / 1M / 10M frames × metrics off/on (~3 h, sequential)
uv run python -m benchmarks run --label baseline

# a subset
uv run python -m benchmarks run --scenario B1 B2 --frames 100k 1M --metrics off

# B4 (10 000 nodes) at research scale — one run, ~1.5-2 h on SimPy
uv run python -m benchmarks run --scenario B4 --frames 100M --metrics off --timeout 14400

# batch throughput: N independent runs, J at a time
uv run python -m benchmarks batch --scenario B3 --frames 1M --runs 16 --jobs 8

# markdown summary of a results file
uv run python -m benchmarks summarize benchmarks/results/<file>.json

# profiling (reports go to benchmarks/profiles/, not committed)
uv run python -m benchmarks profile --scenario B3 --frames 1M --tool cprofile
uv run python -m benchmarks profile --scenario B1 --frames 1M --metrics on --tool tracemalloc
uv run python -m benchmarks profile --scenario B1 --frames 1M --tool py-spy   # needs py-spy installed
```

Every measurement runs in a **fresh subprocess**, so that its peak RSS belongs to one run. Measurements run **one at a time**, because parallel runs would compete for CPU and distort the timings.

Results are written as JSON to `benchmarks/results/`, one record per run. Each record includes:
- the machine, Python, SimPy and SMOLPy versions, and the git commit;
- an **output hash** of the event count and all metric samples. It confirms that a run computed exactly the same thing as the runs it is compared against.

## Scenarios

All traffic sources are deterministic (constant rates, periodic protocols), so the frame and event counts do not depend on the seed. Each scenario is sized by the number of **injected frames**, which is rate × simulated duration, exact up to ±1 frame per periodic source.

| Scenario | Topology | Injected frames / simulated s | Simulated time for 1 M frames |
|---|---|---|---|
| **B1 — single path** | sender → switch → receiver, one 50 000 fps constant flow (512 B, 1 Gb/s links) | 50 000 | 20 s |
| **B2 — industrial cell** | 1 switch, 35 nodes: PLC with 10 PROFINET ARs (2 ms), 10 Modbus slaves (10 Hz), 10 MQTT publishers (10 Hz) → broker → historian, plus a 2 000 fps bulk flow | 12 400 | 80.6 s |
| **B3 — large plant** | core switch + 50 access switches × 20 nodes (1 051 nodes); paired access switches, every node exchanges 20 fps with its counterpart across the core (4 hops) | 20 000 | 50 s |
| **B4 — very large plant** | as B3 with 200 access switches × 50 nodes (10 201 nodes); switches start converged (`mac_table="topology"`) | 200 000 | 5 s |

With **metrics on**, each scenario adds a representative set of `observe()` statements, sampled every 100 ms. B3 has 151 observations.

B1–B3 make up the regular suite (`run` without `--scenario`). B4 is the research-scale scenario for the extended Rust decision gate (ADR-0006 §6). It is run on its own, at 100 M frames.

**Why B4 starts converged.** With address learning, every switch floods the first frames to each of the 10 000 destinations. Each flooded frame reaches ~200 access switches × ~50 ports. Queues are unbounded, so all these copies wait in memory. At 1 M frames the result is 329 events per frame instead of 25, 45 min instead of 1 min, and 12 GB peak memory. That cost grows with stations × ports, so it is a start-up artefact of the model, not the scale effect B4 is meant to measure. With `mac_table="topology"`, B4 at 1 M frames takes 60 s and 203 MB, at 24.8 events per frame.

## Baseline

*Measured on Intel Core Ultra 7 255H (16 threads), 31 GB RAM, Linux, Python 3.14, SimPy 4.1.2.*

<!-- BASELINE-TABLE -->
Raw results: [`benchmarks/results/2026-11-13-0921-simpy-jaws-zbook-baseline.json`](https://github.com/jwszolek/SMOLPy/blob/main/benchmarks/results/2026-11-13-0921-simpy-jaws-zbook-baseline.json). 78 runs, no failures or timeouts; every repetition of every variant produced the same output hash.

| Scenario | Frames | Metrics | Runs | Wall time (median, min–max) | Events | Events/s | Frames/s | Peak RSS | Output |
|---|---|---|---|---|---|---|---|---|---|
| B1 | 100k | off | 5 | 2.74 s (2.69–2.76) | 1,699,992 | 620,312 | 36,489 | 41 MB | identical |
| B1 | 100k | on | 5 | 2.95 s (2.93–2.98) | 1,700,072 | 576,497 | 33,910 | 40 MB | identical |
| B2 | 100k | off | 5 | 4.43 s (4.41–4.47) | 1,940,224 | 437,845 | 22,567 | 46 MB | identical |
| B2 | 100k | on | 5 | 4.50 s (4.49–4.56) | 1,940,872 | 430,894 | 22,201 | 46 MB | identical |
| B3 | 100k | off | 5 | 15.80 s (14.46–15.93) | 7,380,145 | 467,159 | 6,330 | 70 MB | identical |
| B3 | 100k | on | 5 | 16.05 s (15.93–16.82) | 7,387,695 | 460,244 | 6,230 | 71 MB | identical |
| B1 | 1M | off | 5 | 27.06 s (26.98–27.53) | 16,999,992 | 628,175 | 36,952 | 75 MB | identical |
| B1 | 1M | on | 5 | 29.74 s (23.71–29.93) | 17,000,792 | 571,707 | 33,628 | 75 MB | identical |
| B2 | 1M | off | 5 | 44.20 s (43.01–45.04) | 19,403,129 | 438,950 | 22,623 | 123 MB | identical |
| B2 | 1M | on | 5 | 44.84 s (43.60–45.00) | 19,409,585 | 432,848 | 22,301 | 124 MB | identical |
| B3 | 1M | off | 5 | 79.03 s (78.57–79.66) | 37,080,145 | 469,197 | 12,654 | 105 MB | identical |
| B3 | 1M | on | 5 | 81.93 s (80.35–82.58) | 37,155,645 | 453,509 | 12,206 | 115 MB | identical |
| B1 | 10M | off | 3 | 271.97 s (271.49–272.23) | 169,999,992 | 625,076 | 36,769 | 419 MB | identical |
| B1 | 10M | on | 3 | 300.90 s (298.90–305.07) | 170,007,992 | 565,002 | 33,234 | 420 MB | identical |
| B2 | 10M | off | 3 | 443.04 s (439.23–443.28) | 194,032,022 | 437,953 | 22,571 | 880 MB | identical |
| B2 | 10M | on | 3 | 448.43 s (448.10–448.51) | 194,096,542 | 432,836 | 22,300 | 896 MB | identical |
| B3 | 10M | off | 3 | 704.60 s (704.08–718.26) | 334,080,145 | 474,141 | 14,192 | 451 MB | identical |
| B3 | 10M | on | 3 | 752.29 s (750.99–752.88) | 334,835,145 | 445,087 | 13,293 | 550 MB | identical |

What the baseline shows:

- **Throughput is flat in run length.** Each scenario keeps its event rate from 100 k to 10 M frames: B1 ~625 k events/s, B2 ~438 k events/s, B3 ~470 k events/s. Wall time grows linearly, with no slowdown in long runs.
- **Frames/s depend on events per frame**, not on the event rate. Those ratios are 17 for B1 (2 hops), ~19.4 for B2 and ~33–37 for B3 (4 hops).
- **Metrics cost 1–10 % of run time.** The largest share is in B1, where the engine itself does the least work per frame.
- **Peak memory grows with the number of frames.** From 1 M to 10 M frames: B1 75 → 420 MB, B2 123 → 896 MB, B3 105 → 550 MB. This matches the `latency_us` finding below. B2 grows fastest, because Modbus RTT and PROFINET latency and jitter lists grow alongside it.
- **B3 at 100 k is inflated by start-up flooding** (~74 instead of ~37 events per frame, see *Findings outside performance*). Compare B3 at 1 M and 10 M.
- One B1 1 M metrics-on repetition ran in 23.7 s against a median of 29.7 s, with an identical hash. This is most likely CPU frequency or core scheduling; the median is the reported value.

**Baseline against the Rust decision gate (ADR-0006 §6).** The gate is evaluated formally after the §5 optimisations. The baseline starting point is:

| Target | Baseline | Status |
|---|---|---|
| B3 at 10 M frames, metrics on, under 10 min | 752 s (12.5 min) | missed by 25 % |
| Peak memory at 10 M frames under 4 GB | max 896 MB (B2, metrics on) | met |
| 100 × B2 at 1 M frames under 1 h on 8 cores | ~13 × 45 s ≈ 10 min estimated | not measured yet (needs ADR-0009 batch runner) |
<!-- /BASELINE-TABLE -->

## Profiling: where the time and memory go

Profiled at 1 M frames, metrics off. CPU times below are **under cProfile**, which slows the run 3–5×; the shares are what matter, not the absolute seconds.

### CPU: SimPy machinery dominates

| | B1 (2 hops) | B3 (4 hops) |
|---|---|---|
| Events processed | 17.0 M | 37.1 M |
| Events per injected frame | 17.0 | 37.1 |
| Python function calls | 297 M | 649 M |

**One hop costs about 8.5 SimPy events.** The events are, in order:
1. a put into the link's egress queue (`Store` put event);
2. the get by the link process (`Store` get event);
3. the transmission timeout;
4. **the start of a new SimPy process just to wait out the propagation delay**: a `Process` object, its `Initialize` event and the propagation timeout;
5. the put into the next node's inbound store;
6. that node's get.

On top of that, every injected frame costs one timeout in its traffic generator.

In both profiles, most of the run time is spent inside SimPy itself:
- the event loop (`Environment.step`, `Process._resume`, `schedule`, heap push/pop);
- `Store` bookkeeping (`_trigger_put`, `_trigger_get`, event constructors).

SMOLPy's own code is only about **12–15 % of own time**: the link process `_LinkChannel._run`, `_propagate_and_deliver`, `enqueue` and the traffic generator. Switch forwarding and receivers do not appear among the top consumers. The cost is per event, not per protocol feature.

### Memory: one list grows with every frame

| B1, 1 M frames | metrics off | metrics on |
|---|---|---|
| Python heap at end of run | 39.1 MiB | 39.3 MiB |
| of which `_AdapterCounters.latency_us` (`engine.py`, `self.latency_us.append(...)`) | **30.9 MiB** (1 M floats) | **30.9 MiB** |
| everything else (topology, queues, metric samples) | < 1 MiB | < 1 MiB |

Every adapter appends the latency of **every received frame** to a list that is never trimmed, **whether or not anything observes it**. That is 32 B per received frame:
- about 310 MiB at 10 M frames;
- about 3 GB at 100 M frames.

`modbus_rtt_us` and the PROFINET latency and jitter lists behave the same way for their traffic. Sampling windows only ever read the newest slice of these lists.

### ADR-0006 hypotheses

| Hypothesis | Result |
|---|---|
| Several SimPy events per frame per hop | **Confirmed**: ~8.5 per hop, plus 1 per injected frame |
| A new process per frame per hop for propagation | **Confirmed**: one `Process` + `Initialize` event + timeout per hop; the single largest avoidable cost |
| A new `_Frame` object per hop | **Confirmed**: one copy per delivery. Cheaper than the SimPy events, but still one allocation per hop. |
| Memory grows with frames, not network size | **Confirmed**: `latency_us` holds ~79 % of the Python heap at 1 M frames, with metrics on or off |

## Findings outside performance

Building the benchmarks exposed two behaviours of the model that matter for results, not just for speed:

- **One-way traffic floods.** A switch learns a MAC address only from frames the station *sends*. If a destination never sends, every access switch on the path keeps flooding frames for it to all its ports for the whole run. This is real Ethernet behaviour. It was the reason B3 was changed to paired, two-way traffic, because the one-way version measured 116 instead of ~35 events per frame.
- **Adapters accepted frames not addressed to them (fixed).** `_adapter_receiver` counted every frame that arrived, without checking `dst_mac`. Under flooding, `throughput`, `bytes_received` and `latency` of *every* node in the flood domain included traffic meant for someone else. A real NIC drops these frames. All end stations (adapters, Modbus slaves, MQTT brokers, PROFINET devices) now accept only frames addressed to their own MAC. B1 and B2 are unaffected. In B3 the change is confined to the first 100 ms sampling window, where start-up flooding used to show up, for example as 20.48 Mb/s instead of 0.04 Mb/s on an observed node. From 200 ms on, every metric is identical.

A third, smaller effect: B3 at 100 k frames shows ~74 events per frame instead of ~37. At start-up no switch knows any of the 1 000 destinations, so the first frame to each one is flooded through the core and the access switches. This one-off cost is ~4 M events; it is significant at 100 k frames and negligible at 1 M and above.

## Optimisation log

Each optimisation is checked with the **metrics hash**, a hash of all metric samples without the event count. Removing engine events changes the output hash by design. The metrics hash must stay identical to the baseline engine in every scenario with metrics on.

Timings below are single runs, metrics on, on the reference machine. The full suite was then re-run on the result; see *Optimised engine vs baseline*.

### 1. Propagation as a timed callback

`_LinkChannel` no longer starts a SimPy process for every frame on every hop. Each frame is now a single timeout that carries it, with delivery attached as a callback.

| Scenario, 1 M frames | Events before → after | Run time before → after | Metrics hash |
|---|---|---|---|
| B1 | 17.0 M → 13.0 M (−24 %) | 30.4 s → 24.7 s (−19 %) | identical |
| B2 | 19.4 M → 15.4 M (−21 %) | 44.8 s → 37.6 s (−16 %) | identical |
| B3 | 37.2 M → 28.1 M (−24 %) | 86.9 s → 68.4 s (−21 %) | identical |

One hop now costs ~6.5 events instead of ~8.5.

### 2. Keep only the current window of per-frame values

Latency, Modbus RTT and PROFINET latency and jitter used to be appended to lists that lived for the whole run, observed or not. Now each sampler registers its own window buffer with the node and clears it after every sample. With no observer, the value is not even computed.

The window keeps the raw values and aggregates them as before, rather than using a running sum. Since Python 3.12 `sum()` over floats uses compensated summation, and a running sum could differ in the last bits.

| Scenario, metrics on | Peak RSS before → after | Run time before → after | Metrics hash |
|---|---|---|---|
| B1, 1 M | 75 MB → 36 MB | 24.7 s → 23.9 s | identical |
| B2, 1 M | 124 MB → 37 MB | 37.6 s → 35.0 s | identical |
| B3, 1 M | 115 MB → 56 MB | 68.4 s → 66.9 s | identical |
| B2, 10 M | 896 MB → 45 MB | 448 s → 323 s (optimisations 1 + 2) | — |

Peak memory no longer grows with run length. It is now set by the topology and the number of observations.

### 3. `@dataclass(slots=True)` for `_Frame`

Optimisations 3 and 4 do not change the order of events, so the full output hash, event count included, has to match as well. It does in all six scenario and size combinations. Their effect is a few per cent, which is about the noise of a single run, so they were measured A/B, alternating the two variants three times. The table shows medians.

| Scenario, 1 M frames, metrics on | Without → with slots |
|---|---|
| B1 | 24.4 s → 23.9 s (−2 %) |
| B3 | 71.4 s → 69.8 s (−2 %) |

### 4. No frame copy per hop

Delivery used to build a new `_Frame` on every hop, so that flooded copies would not share `in_port`. A frame object is only ever in one place, because every sender builds a new frame per transmission. So delivery now sets `in_port` in place. Only flooding (switch, unknown destination) and hub broadcast copy the frame, once per output port.

| Scenario, 1 M frames, metrics on | Copy → no copy |
|---|---|
| B1 | 23.9 s → 22.4 s (−6 %) |
| B3 | 70.2 s → 66.8 s (−5 %) |

B3 at 100 k frames, which floods heavily at start-up, also gives an identical output hash.

## Optimised engine vs baseline

The full suite was re-run after all four optimisations, at commit `d3eb8a2`, on the same machine. Raw results are in [`benchmarks/results/2026-11-13-1527-simpy-jaws-zbook-optimised.json`](https://github.com/jwszolek/SMOLPy/blob/main/benchmarks/results/2026-11-13-1527-simpy-jaws-zbook-optimised.json). All 78 runs completed, and every repetition of every variant produced the same output hash. The metrics hash with metrics on matches the baseline engine at 100 k and 1 M frames.

| Scenario | Frames | Metrics | Wall time baseline → optimised | Speed-up | Events/frame | Peak RSS baseline → optimised |
|---|---|---|---|---|---|---|
| B1 | 100k | off | 2.74 s → 1.59 s | 1.73× | 17.0 → 13.0 | 41 MB → 36 MB |
| B1 | 100k | on | 2.95 s → 2.21 s | 1.34× | 17.0 → 13.0 | 40 MB → 37 MB |
| B2 | 100k | off | 4.43 s → 3.35 s | 1.32× | 19.4 → 15.4 | 46 MB → 37 MB |
| B2 | 100k | on | 4.50 s → 3.53 s | 1.27× | 19.4 → 15.4 | 46 MB → 37 MB |
| B3 | 100k | off | 15.80 s → 11.55 s | 1.37× | 73.8 → 55.6 | 70 MB → 47 MB |
| B3 | 100k | on | 16.05 s → 13.30 s | 1.21× | 73.9 → 55.7 | 71 MB → 48 MB |
| B1 | 1M | off | 27.06 s → 19.71 s | 1.37× | 17.0 → 13.0 | 75 MB → 36 MB |
| B1 | 1M | on | 29.74 s → 22.03 s | 1.35× | 17.0 → 13.0 | 75 MB → 37 MB |
| B2 | 1M | off | 44.20 s → 33.40 s | 1.32× | 19.4 → 15.4 | 123 MB → 37 MB |
| B2 | 1M | on | 44.84 s → 35.19 s | 1.27× | 19.4 → 15.4 | 124 MB → 38 MB |
| B3 | 1M | off | 79.03 s → 61.60 s | 1.28× | 37.1 → 28.1 | 105 MB → 47 MB |
| B3 | 1M | on | 81.93 s → 64.59 s | 1.27× | 37.2 → 28.1 | 115 MB → 56 MB |
| B1 | 10M | off | 271.97 s → 198.06 s | 1.37× | 17.0 → 13.0 | 419 MB → 36 MB |
| B1 | 10M | on | 300.90 s → 220.48 s | 1.36× | 17.0 → 13.0 | 420 MB → 38 MB |
| B2 | 10M | off | 443.04 s → 331.66 s | 1.34× | 19.4 → 15.4 | 880 MB → 37 MB |
| B2 | 10M | on | 448.43 s → 353.43 s | 1.27× | 19.4 → 15.4 | 896 MB → 45 MB |
| B3 | 10M | off | 704.60 s → 543.93 s | 1.30× | 33.4 → 25.3 | 451 MB → 47 MB |
| B3 | 10M | on | 752.29 s → 566.59 s | 1.33× | 33.5 → 25.4 | 550 MB → 145 MB |

- **The Python-level work gave about 1.3× across the board** and removed ~24 % of engine events. The event rate itself barely moved (B1 ~655 k events/s, B2 and B3 ~440–480 k events/s). What remains is SimPy's per-event cost.
- **Memory is now flat in run length** for engine state: 36–47 MB with metrics off, at every size. The 145 MB of B3 at 10 M frames with metrics on is the result itself: 151 observations × ~5 000 samples each, held in `SimulationResult.metrics`.
- **Runs of 1–3 s are noisy.** B1 at 100 k frames ranges 0.96–2.09 s, probably due to CPU frequency scaling. Compare at 1 M frames and above.

## Rust decision gate (ADR-0006 §6)

| Target | Optimised SimPy | Result |
|---|---|---|
| B3 at 10 M frames, metrics on, under 10 min | 566.6 s (9.4 min); baseline 752 s | **met**, by a 6 % margin |
| Peak memory at 10 M frames under 4 GB | max 145 MB | **met** |
| 100 × B2 at 1 M frames under 1 h on 8 cores | not measured; 100 × 35.2 s / 8 ≈ 7–8 min if runs scale, ~15 min allowing 2× for shared cores | **met (estimate)**, to be confirmed by the ADR-0009 batch runner |

By the rule in ADR-0006 §6, optimised SimPy meets all three targets, so the Rust engine (ADR-0008) **does not proceed on these targets**. They measure whether SimPy is sufficient for today's scenarios, not for larger networks or many independent runs. Revising them is a decision to be recorded in ADR-0006.
