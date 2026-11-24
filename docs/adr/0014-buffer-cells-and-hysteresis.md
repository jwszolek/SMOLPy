# ADR-0014: Switch Buffer Limits in Cells, with a Resume Threshold

**Status:** Accepted  
**Date:** 2026-11-24  
**Amends:**
- ADR-0013: the unit of the testbed switch's limit is cells, not frames. ADR-0013's scope, accounting per ingress port, stands.
- ADR-0011: tail drop becomes the special case of a drop decision with a resume threshold.
- ADR-0007: five new optional IR fields. The IR stays version 2.
- ADR-0004: the `smol-ns3` line format becomes version 5.
- [Model Semantics](../model-semantics.md) §3, §8, §9 and the open issues.

**Roadmap:** ADR-0013's possible extension "limits in cells".

---

## Context

> *Note, 2026-11-24:* [ADR-0015](0015-shared-pool-and-drain-resume.md) revises the switch model this ADR fits. With two senders the testbed shows one shared pool, with a reserve per port, and a drop state that ends after an amount of the buffer has been freed, not at a resume level of the port's own account. With one sender the two are equivalent, which is why this ADR fits every single-source measurement. The text below is as it was decided.

ADR-0013 was checked on the testbed the same day.

### The scope is confirmed

The testbed scenario has the Pi as one `modbus_slave` that also sends the UDP flow, and the Waveshare on another port. At 8 099 frames/s, 3 seeds × 60 s:

| | UDP loss | Pi polls retransmitted or unanswered | Waveshare |
|---|---|---|---|
| Hardware (310 s) | 0.66 % | 0.44 % | 0 / 15 000 |
| ns-3, `ingress_buffer=60frames` | 0.67 % | 0.55 % | 0 / 9 000 |
| SimPy / Rust, same | 0.66 / 0.74 % | 0.39 / 0.38 % | 0 |
| ns-3, `buffer=91080` (ADR-0011's model) | 0.76 % | 0.46 % | 47 / 9 000 |
| `ingress_buffer=94720` (bytes) | 0.64 % | 0 % | 0 |

Per-ingress-port accounting is the only model in which the Waveshare loses nothing.

### Measurement 1: the limit is in cells of 128 B

ADR-0013 counts frames and rules out cells. A burst test repeated with small frames refutes that.
- **The test.** The Pi sends N frames back to back, and the laptop counts what it receives. The switch holds the frames received less those its 100 Mb/s port drained while the burst lasted.
- **The burst's pace on the wire.** That is the Pi's send rate, or the 1 Gb/s line rate if that is lower:
    - 1 518 B and 512 B frames are sent faster than 1 Gb/s can carry, so they leave at line rate;
    - 64 B frames leave at the generator's pace, 63 000 to 340 000 frames/s, far below the line rate of 1.49 million.

| Frame | Frames held | If frames | If bytes | If 128 B cells |
|---|---|---|---|---|
| 1 518 B | about 60–62 | 60 | 60 | 60, at 12 cells each |
| 512 B | about 190 | 60 | 178 | 185, at 4 cells each |
| 64 B | about 740 | 60 | about 1 400 | 740, at 1 cell each |

So the switch has about **740 cells of 128 B per ingress port**, about 95 kB, and a frame takes ⌈frame bytes / 128⌉ cells.
- **Data:** `stanowisko/pomiary/wyniki/2026-11-23_bufor_paczki.csv`, `2026-11-24_bufor_paczki_512B.csv` and `2026-11-24_bufor_paczki_64B.csv`, in the paper's repository.
- **Fits per frame size:** about 726 cells (1 518 B), 740 (64 B) and 760 (512 B, the noisiest).

### Measurement 2: a full port drops everything for a while

With cells, a 75 B response almost always fits beside full-size frames: only a limit that is an exact multiple of 12 cells leaves no room. Yet the hardware drops the Pi's responses at 0.44 %.

The loss pattern shows why. Sequence numbers of Poisson UDP at 8 100 frames/s were recorded over 2 × 30 s (`2026-11-24_serie_strat/`):
- **Loss:** 0.55–0.59 %.
- **Losses come in runs:** a mean length of 6.4–6.7 frames, from 2 to 15 with a mode at 5–8, and **no single-frame loss** among about 210 runs per recording.
- **Tail drop at the same loss** (M/D/1/K) gives a mean run of 1.34, with 72 % of runs a single frame.
- **Not a generator burst.** The frames of a lost run were sent at the normal Poisson pace: their sending time over the expected time has a median of 0.90. The generator's gaps follow Poisson: 11.6 % under 15 µs, against 11.4 % expected.

So when an arriving frame does not fit, the port enters a **drop state**. It drops every frame that arrives on it, whatever its size, until its occupancy falls to a lower threshold. A run then lasts while the queue drains by the gap between the thresholds, and its length is about 1 + Poisson(λT).

### Cells with hysteresis fit every observation at once

A quick Python simulation, not SMOLPy, used 1 518 B Poisson at 8 089 frames/s, a limit of about 740–760 cells, and a resume level about 72 cells (6 full frames) lower. The frame in transmission was not counted, as in ADR-0011.

| | Simulated | Hardware |
|---|---|---|
| UDP loss | 0.62 % | 0.55–0.66 % |
| Mean run | 6.3 | 6.4–6.7 |
| Single-frame runs | 0–1 % | 0 % |
| A small response from the same port lost | 0.51 % | 0.44 % |

Without hysteresis there are only two outcomes:
- the limit is a multiple of 12 cells: responses are lost at the UDP rate, and runs average 1.35;
- any other limit: responses are never lost.

It also explains the batch family of the overnight campaign: Poisson groups of K = 4 and 8 back-to-back frames. The ratio is response loss per response sent over UDP loss:

| | Ratio |
|---|---|
| Hardware | 0.55–0.72 |
| Cells with hysteresis | 0.63–0.74 |
| Tail drop | 0.27–0.37 |
| ns-3 today | 0.16–0.26 |

`60frames` works for 1 518 B traffic only because cells and frames are then proportional. For 512 B frames it sets the buffer about 3× too small.

### Options considered

- **A: keep frames** (ADR-0013). Refuted by the 512 B and 64 B burst tests.
- **B: cells, with tail drop.** It gets the burst tests right, but either loses no responses or loses them at the UDP rate, with single-frame runs. Refuted by measurement 2.
- **C: cells, with a resume threshold on each account.** **Chosen.** It fits the burst tests, the runs and the response loss with the same two parameters, and every engine can reproduce it exactly, with no new event.
- **D: a per-frame header in cells,** e.g. a descriptor counted with each frame. Not needed: the data fit without one. It stays a possible extension.

---

## Decision

### Cells as a unit

A switch takes an optional **cell size** in bytes, and either limit may be in cells:

```
switch sw ports=8 cell=128 ingress_buffer=740cells
switch sw ports=8 cell=128 buffer=600cells ingress_buffer=740cells
```

```python
from smolpy import Cells

sw = net.switch("sw", ports=8, cell=128, ingress_buffer=Cells(740))
```

- **A frame's cells:** ⌈frame bytes / cell⌉, with the frame bytes of Model Semantics §2. So at `cell=128` a 64 B frame takes 1 cell, a 512 B frame 4 and a 1 518 B frame 12.
- **No per-frame header.** A switch chip keeps a descriptor per frame, but where it is stored is chip-internal, and the burst tests fit without one.
- **The units of ADR-0013 stay:** bytes, frames, and now cells, one unit per account.
- **Validation:**
    - `cell` is a whole number of bytes, at least 1;
    - `cell` is required when a limit is in cells, and is an error when none is;
    - a limit in cells holds at least the cells of the largest frame of the scenario, as a limit in bytes holds its bytes.
- **IR:** `SwitchNode.cell_bytes`, `buffer_cells` and `ingress_buffer_cells`, written only when set.

### A resume threshold: the drop state

Each account may have a **resume level**, in the unit of its limit and less than it:

```
switch sw ports=8 cell=128 ingress_buffer=740cells ingress_resume=668cells
switch sw ports=8 buffer=40frames resume=34frames
```

```python
sw = net.switch("sw", ports=8, cell=128, ingress_buffer=Cells(740), ingress_resume=Cells(668))
```

An account with a resume level is either **admitting** or in the **drop state**:
- **Entering.** An arriving frame that does not fit is dropped. If the account then holds more than its resume level, it enters the drop state.
- **In the drop state** every frame decided by that account is dropped, whatever its size, even one that would fit.
- **Leaving.** The account leaves the drop state when a release brings what it holds to the resume level or below. Releases are ADR-0013's: a transmitter taking a frame. So leaving needs no new event.
- **Without a resume level** an account only ever drops a frame that does not fit, which is tail drop. Every scenario without `resume` or `ingress_resume` means exactly what it meant: the same results, event counts and benchmark hashes.

**Why the entry condition.** Right after a release that ended a drop state, the account holds at most its resume level. A frame that then does not fit is dropped as by tail drop, and does not start a new drop state. Without the condition, an account whose resume level is within one frame of its limit could enter a drop state that no release ends until another frame is taken, while it has room. With a resume level at least one largest frame below the limit, as in every calibrated case, the condition always holds when a frame is refused.

**Same instant.** Admissions and releases at the same instant are decided in event order, as ADR-0013's occupancy is (Model Semantics §9):
- a frame decided before the release sees the drop state;
- a frame decided after it sees the account admitting, if the release brought it to the resume level.

All engines order a take and a forward at the same instant the same way, through ADR-0011's idle-port rule, so they agree on every drop.

**The tail-drop identity.** In frames, `resume = limit − 1` is tail drop:
- a frame is refused only when the account holds `limit` frames, which is above `limit − 1`, so it enters the drop state;
- the next release brings it to `limit − 1`, so it leaves at once;
- in between the account is full, so every frame it drops, it would have dropped by tail drop too.

That is a test on every engine. No such identity holds in bytes or cells, because a frame smaller than the one taken may fit.

**Both scopes.** ADR-0013's admission order stands:
- **Ingress first.** A frame refused by its ingress account, for lack of room or by the drop state, is one ingress drop, and no egress account sees it.
- **Then each copy's egress account,** which may be in its own drop state.
- **The ingress account decided "admit"** when the frame passed it, even if every copy was then dropped at egress.

**Flooding.** The ingress account decides once per frame and is charged once (ADR-0013). An egress account in the drop state drops only the copy for its port.

**Egress gets hysteresis too.** The two scopes stay symmetric: `resume` for `buffer`, `ingress_resume` for `ingress_buffer`. The pending two-Pi measurement may show an egress limit, and it should not need another change of the model.

**IR:** `SwitchNode.buffer_resume` and `ingress_buffer_resume`, in the unit of their account's limit, written only when set.

**Validation:**
- a resume level needs a limit in its scope, and is given in that limit's unit: in Python, an `int` with a limit in bytes, `Frames` with `Frames`, `Cells` with `Cells`;
- it is a whole number, at least 0 and less than the limit.

### Metrics

| Metric | On | Value at a sample | Unit |
|---|---|---|---|
| `ingress_drop_episodes_total` | switch | drop episodes of its ingress accounts so far | episodes |
| `egress_drop_episodes_total` | switch | drop episodes of its egress accounts so far | episodes |
| `ingress_drop_time_total` | switch | time its ingress accounts have spent in the drop state so far, summed over its ports, up to the sample | ms |
| `egress_drop_time_total` | switch | the same for its egress accounts | ms |

**An episode** is a maximal run of consecutive drops by one account: drops among the frames that account decides, with no frame admitted between them.
- **For tail drop,** that is each run of refusals.
- **With a resume level,** it is one drop state, with any refusal that directly follows it.
- **The mean run** is then `drops / episodes` in both cases: `ingress_drops_total / ingress_drop_episodes_total`, and likewise for egress.
- **Why this definition.** Counting entries into the drop state would leave tail drop with no episodes at all. Counting runs gives both models the measure the hardware gives: runs of lost sequence numbers.

**The drop time** is what a light stream through the same account sees: a frame of it is lost while the account is in the drop state, and otherwise almost never, if it is small. Without a resume level an account has no drop state, so its drop time stays 0.

Per-port metrics stay open: `observe` addresses a node. A sum over the ports is exact when only one port is ever full, as on the testbed.

### Capabilities

| Feature | Rejects without it |
|---|---|
| `"buffer_cells"` | a limit in cells |
| `"buffer_hysteresis"` | a resume level |

The four metrics are capabilities as usual. All three backends get both features in this change.

### The IR stays version 2

- **Five new optional fields:** `cell_bytes`, `buffer_cells`, `ingress_buffer_cells`, `buffer_resume` and `ingress_buffer_resume`. A switch without them means what it meant.
- **Older readers** reject the fields, and `check()` rejects the scenario first, for the missing capability.

### ns-3

The switch is SMOL's own (ADR-0011, ADR-0013):
- the account of `SmolDevice` and `SmolSwitch` gains the cell unit, the resume level, the drop state, and the run and time counters;
- the `switch` line becomes `switch <cell B> <egress limit> <unit> <egress resume> <ingress limit> <unit> <ingress resume>`, with the unit `b`, `f` or `c`, and 0 for no limit or no resume level;
- the format becomes `smol-ns3 5`;
- **expected agreement:** Ethernet and PROFINET stay bit-exact with SimPy; Modbus and MQTT differ by TCP, as in ADR-0011.

### Scope

- **In scope:** limits in cells, and resume levels for both scopes, with their metrics.
- **Still open, not part of this decision:**
    - **whether the LS108GP also limits its egress port, or shares a pool.** A testbed run with two Pis sending to the laptop at once is planned;
    - **per-port metrics;**
    - **a per-frame header in cells,** and both units in one account;
    - **limits per priority class.**

### Validation and tests

- **Unit tests:**
    - **a frame's cells:** 64 B → 1, 512 B → 4, 1 518 B → 12, at `cell=128`, and the rounding up at cell boundaries;
    - **admission:** the drop state entered exactly when a frame does not fit and the account holds more than its resume level;
    - **a small frame dropped in the drop state** although it fits;
    - **leaving exactly at the resume level,** at the release that reaches it, and not at the one before;
    - **same instant:** an arrival at the instant of the release that ends a drop state, admitted when the forward comes after the take;
    - **validation:** each rule above;
    - **the tail-drop identity** on every engine: in frames, `resume = limit − 1` gives tail drop's run, every sample and the event count;
    - **cells as frames:** `cell` equal to the only frame size of a scenario gives the run that `frames` gives, on every engine.
- **Run lengths:**
    - **deterministic:** an audit of every decision checks that each episode's drops are its first refusal plus the frames that arrive before the release that reaches the resume level;
    - **statistical:** one frame size, Poisson arrivals. The mean run is 1 + λ·E[T], where T is the time from the refusal to that release: the residual of the frame in transmission, then the services that bring the account down to the resume level. The run distribution is the mixture of 1 + Poisson(λT) over T. Both are compared with the engine over several seeds.
- **Analytical.** One port, one frame size, Poisson arrivals, a limit and a resume level in frames: an M/D/1/K queue with hysteresis.
    - **The chain.** A Markov chain on (frames queued, drop state), embedded at the instants the transmitter takes a frame. Given the state and the k arrivals during a service, both what is admitted and when the drop state is entered follow, and so do the next state and the drops.
    - **The results.** Its stationary distribution gives the loss, the mean run and the run distribution, exactly. The time-average queue gives the mean wait: arrivals in a service are uniform given k, so its area is exact too.
    - **The frames** are 9 000 B at 1 Gb/s. The gap is then 0.13 % of a service, as in ADR-0011's M/D/1/K test, which bounds the effect of the first frame of a busy period.
- **Cross-engine:**
    - deterministic scenarios join the bit-exact set of SimPy and Rust, event counts included: mixed frame sizes in cells, a drop state entered and left at the instants of arrivals, and flooding during a drop state;
    - the same Ethernet and PROFINET scenarios are compared exactly on ns-3;
    - the full existing suite passes unchanged.

### The testbed, calibrated

- **The example.** `examples/example_ingress_buffer.smol` becomes `cell=128 ingress_buffer=740cells ingress_resume=668cells`. The `60frames` variant stays as a comment, with one line on why cells are the better model.
- **The testbed scenario,** in `docs/backends.md` next to ADR-0013's: T3 with cells and hysteresis, on all three engines, at 8 100 frames/s. Reported against the hardware:
    - **UDP loss:** 0.55–0.66 %;
    - **the mean run:** 6.4–6.7, from `ingress_drops_total / ingress_drop_episodes_total`, on every engine;
    - **single-frame runs:** none on the hardware. Their share needs each run's length, which no metric gives, so it comes from SimPy, by an audit of its decisions in the benchmark, and is reported as SimPy's;
    - **the Pi's polls** lost (SimPy, Rust) or retransmitted (ns-3): 0.44 %;
    - **the Waveshare's polls lost:** 0.
- **The burst test in the model.**
    - **The burst:** N frames of 64, 512 and 1 518 B at the pace the Pi put them on the wire: the measured send rate of each row, or the 1 Gb/s line rate if that is lower.
    - **The count:** the frames dropped among the first N, at the instant the N-th has been decided. The model's received count is N less those, against each row of the three CSVs.
- **The batch case.** Poisson groups of K = 8 back-to-back 1 518 B frames at 7 500 frames/s, with the Pi's polls. Reported: response loss over UDP loss, against the hardware's 0.55.
- **Calibration.** 740 and 668 are first estimates:
    - 740 comes from the 64 B burst test;
    - 668, 72 cells or 6 full frames below, comes from the quick fit to the run lengths.

  So the run lengths and the UDP loss are not an independent check of 668. The burst tests, the response loss and the batch ratio are. If they point to other values, those are reported beside the scenario's, not put into it.

*As built (2026-11-24):*
- **Nothing changes without the new parameters.**
    - The benchmark hashes of B1–B4 are unchanged on SimPy and on Rust, event counts included.
    - Every earlier test passes. One validation message changed, for three units instead of two, and the `switch` line of format 5.
- **The `switch` line** has −1, not 0, for no resume level, since a resume level of 0 is valid.
- **The identities hold exactly,** every sample and the event count, on SimPy, Rust and ns-3:
    - in frames, `resume = limit − 1` gives tail drop's run, only the drop time being new;
    - `cell` equal to the only frame size gives the run that `frames` gives, with and without a resume level.
- **The exact chain,** one port, 9 000 B frames, five seeds of 40 000 frames:

    | ρ, limit, resume | Loss: chain / simulated | Mean run | Time in system (services) |
    |---|---|---|---|
    | 1.1, 8, 4 | 0.1193 / 0.1190 ± 0.0016 | 4.672 / 4.636 ± 0.022 | 5.030 / 5.048 ± 0.023 |
    | 0.95, 10, 5 | 0.0340 / 0.0332 ± 0.0007 | 5.114 / 5.235 ± 0.055 | 4.456 / 4.436 ± 0.039 |

    The run distribution agrees within three times the sampling error expected of that many runs. The audit also checks every decision: each drop state is entered at a refusal above the resume level and left at the release that reaches it.
- **Bit for bit across engines.** `cells-mixed`, `hysteresis-tie` and `flood-drop-state` are bit-exact on SimPy, Rust and ns-3, event counts included on Rust. On ns-3, random IMIX and bursty traffic through cells with resume levels gives SimPy's run with the same seed.
    - `cells-mixed`: mixed sizes in cells, both scopes with resume levels, PROFINET.
    - `hysteresis-tie`: arrivals at the instant of the release that ends a drop state. There the take comes first, so the frame is admitted.
    - `flood-drop-state`: flooding during egress and ingress drop states.
- **The testbed** ([Execution backends](../backends.md#cells-and-a-resume-threshold), `benchmarks/results/2026-11-24-1006-simpy-rust-ns3-jaws-zbook-buffer-cells.json`). At 8 100 frames/s, 5 seeds of 30 s:
    - mean runs of 6.23–6.28, with 0.5 % single-frame runs on SimPy, against 6.4–6.7 and none;
    - the Pi's polls lost or retransmitted at 0.40–0.55 %, against 0.44 %, and none of the Waveshare's;
    - UDP loss of 0.64–0.74 %, against 0.55–0.66 %;
    - in the batch case, a ratio of 0.56–0.65, against 0.55; tail drop gives 0, and frames 0.29–0.37.
- **The burst test** puts the model's held frames a little below the measured ones, by up to 72 cells: a burst can end in a drop state. 770 cells resuming at 698 fit the bursts better and lower the UDP loss to 0.69 % on SimPy, with the same runs. That is reported beside the scenario, which keeps 740 and 668.
- **The `smolpy-engine-rs` floor** is `>=1.14.0`, the release that carries this ADR. 1.13 refuses these scenarios by its capabilities. A floor of `1.13.2.dev0` would have let pip install development builds, which are published to PyPI, since a pre-release in a specifier admits pre-releases.

### Steps

1. **The model:**
    - the five optional IR fields, written only when set;
    - `cell`, `Cells`, `<n>cells`, `resume` and `ingress_resume` in the Python API and in `.smol`;
    - validation;
    - the two capability features, which no backend has yet, so every backend rejects a scenario that uses them.
2. **SimPy:**
    - cells, the drop state, the episode and time counters, and the four metrics;
    - unit, identity, run-length and analytical tests;
    - Model Semantics updated.
3. **Rust:**
    - the same;
    - the bit-exact scenarios;
    - the `smolpy-engine-rs` floor raised to the release that carries this ADR, at that release.
4. **ns-3:**
    - format 5 and the accounts in `SmolDevice` and `SmolSwitch`;
    - the exact comparisons.
5. **Documentation and the testbed:**
    - the example, the testbed scenario, the burst test and the batch case on all three engines, with their results recorded;
    - the language and DSL references, `backends.md`, Model Semantics, the roadmap;
    - a note at the top of ADR-0013's context.

---

## Consequences

**Positive**
- **One model of the testbed switch fits every measurement made so far:**
    - the per-port isolation;
    - the burst tests at three frame sizes;
    - the loss runs;
    - the response loss;
    - the batch ratios.
- **Tail drop is a special case,** with an exact identity in frames, so ADR-0011's and ADR-0013's scenarios are unchanged.
- **No new event.** The drop state changes only where an account is charged or released, so the engines stay bit-exact.
- **The run metrics give a model and a testbed the same observable:** runs of lost frames.

**Negative / trade-offs**
- **A new `smolpy-engine-rs` release and ns-3 format version.**
- **More parameters per switch:** a cell size, and a resume level per account. They come from measurement, and a scenario that does not set them keeps tail drop.
- **The resume level is fitted to the run lengths,** so those cannot validate it. The burst tests, the response loss and the batch ratio do.
- **The run distribution needs an audit,** not a metric. Per-port and per-run metrics stay open.
