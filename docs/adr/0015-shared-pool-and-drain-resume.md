# ADR-0015: A Shared Switch Buffer Pool, with a Reserve per Ingress Port, and a Drop State Ended by a Drain

**Status:** Accepted  
**Date:** 2026-11-24  
**Amends:**
- ADR-0013: a switch's ingress accounts can share one pool, beyond a reserve each. This is the "shared pool" extension ADR-0013 names as next.
- ADR-0014: a drop state can end after an amount of the buffer has been freed, instead of at a resume level. ADR-0014 also names the shared pool as next.
- ADR-0007: five new optional IR fields. The IR stays version 2.
- ADR-0004: the `smol-ns3` line format becomes version 6.
- [Model Semantics](../model-semantics.md) §3, §8, §9 and the open issues.

**Roadmap:** the "shared pool with dynamic thresholds" left open by ADR-0013 and ADR-0014.  
**Amended by:** [ADR-0016](0016-switch-wide-drop-state.md): the drop state can belong to the whole switch, and the two-sender overlap below is corrected.

---

## Context

> **Correction (2026-11-24, [ADR-0016](0016-switch-wide-drop-state.md)).** The two-sender overlap of 0 of 2 072 below was an analysis error: the episodes were merged by two unaligned sender clocks, 63.77 s apart. Measured on the laptop's one clock, 96–97 % of the two Pis' loss runs overlap one of the other's. ADR-0016 corrects it with a drop state for the whole switch. The history below is left as it was written.

### ADR-0014 predicts every single-source measurement

ADR-0014 models the LS108GP with 740 cells of 128 B per ingress port and a resume level of 668. It predicted every single-source testbed scenario, including two it was not calibrated on (`2026-11-24_prognoza_adr0014/`):
- 512 B frames: loss runs of 18.5 measured, 18.1 predicted;
- mixed 1 518 + 512 B: a 512/1 518 loss ratio of 0.86 measured, 0.89 predicted.

Three new measurements with **two senders** show where its model of the switch ends. The setup:
- the same LS108GP and laptop port, with PAUSE off on every sender;
- a second Pi on its own 1 Gb/s port;
- the laptop polls both Pis' Modbus servers every 10 ms and the Waveshare every 20 ms.

The raw data is in the paper's repository, `stanowisko/pomiary/wyniki/`:
- `*.seq` files hold the little-endian `uint64` sequence numbers of the received UDP frames;
- `*.seq.t` files hold the sender's `CLOCK_MONOTONIC` send times in ns, in the same order;
- `*.csv` files hold Modbus RTTs.

### 1. Two Pis at once (`2026-11-24_dwa_rpi/powtorka/`)

Both Pis send Poisson 1 518 B frames to the laptop's 100 Mb/s port, 125 s per point. The predictions were made before the measurement.

**At 2 × 4 050 frames/s (ρ 0.995):**

| | Hardware | ADR-0014, ingress only | ADR-0014 with an egress limit |
|---|---|---|---|
| UDP loss | 0.664 / 0.691 % | 0.38 % | 0.75 % |
| RTT max / p99 without retransmissions | 9.26 / 8.48 ms | wait 15.1 / 13.3 ms | wait 7.7 / 7.3 ms |
| Pi polls retransmitted | 0.53 % | 0.10–0.13 % | 0.39 % |
| Waveshare polls lost | 0 / 6 000 | 0 | about 0.5 % |
| Mean loss run per stream | 3.2–3.3, 13 % single | 6.2 | 6.4 |

- **The queue is capped as if by one shared limit,** about the size of the single-source one, not by two ingress limits.
- **The Waveshare still loses nothing,** which an egress limit would not allow.
- **Drop states are per port.** Merged by send time, the loss episodes of the two Pis never overlap: 0 of 2 072. The runs are half as long as with one sender.

**At 2 × 3 900 frames/s (ρ 0.96):** loss 0.036 / 0.039 % (the egress-limit prediction was 0.034 %), RTT max 8.7–9.0 ms, Waveshare 0.

### 2. The reserve of a port (`2026-11-24_rezerwa/`)

Pi 1 holds the laptop's port full with a **constant** 9 000 frames/s (111 %). Pi 2 sends bursts of N back-to-back frames, and the table counts how many arrive (`rezerwa_tlo9000_const.csv`, 10 repetitions):

| Burst from Pi 2 | Arrived |
|---|---|
| 10 × 1 518 B | 5–9 (median 6.5) |
| 20 × 1 518 B | 4–9 |
| 40 × 1 518 B | 7–12 |
| 50 × 64 B | always 50 |
| 100 × 64 B | 71–100 |
| 200 × 64 B | 112–168 |
| 400 × 64 B | 203–227 |

The last three repetitions of N = 400 are left out: the background had ended.
- **Control:** without background (`rezerwa_tlo0.csv`), bursts arrive whole up to about 60 full frames.
- **A Poisson background cannot show the reserve.** At 102 % (`rezerwa_tlo8300.csv`) the queue sits on average μ/(λ − μ) ≈ 44 frames below the top, so bursts find space.
- **The reserve.** After what drains during a burst is subtracted, a port gets about 25–48 cells, roughly 30, even when the shared space is full.

### 3. What ends a drop state (`2026-11-24_10Mb/`)

The laptop's link was set to 10 Mb/s, with one Pi sending Poisson 1 518 B frames:

| | Hardware | Drop state ends after 72 cells drain | Ends after a fixed 0.74 ms |
|---|---|---|---|
| 808/s (ρ 0.995): loss | 0.60 % | 0.61 % | 0.59 % |
| Mean run | 6.20 | 6.17 | 1.63 |
| Single-frame runs | 1 % | 1 % | 53 % |
| 846/s (ρ 1.04): loss | 4.27 % | 4.22 % | 4.21 % |
| Mean run | 6.54 | 6.47 | 1.66 |
| Single-frame runs | 0 % | 1 % | 52 % |

The RTT max is about 82 ms, which is 740 cells at 10 Mb/s. So the drop state ends after an amount has drained, not after a time.

### A model that fits all of it

A quick Python simulation (`stanowisko/model/symulacja_puli.py`, not SMOLPy) has three parts:
- **a shared pool** of about 710 cells;
- **a reserve** of about 30 cells per ingress port;
- **a drop state per ingress port,** entered when a frame of that port is refused, and left when about 72 cells have departed since entry.

| | Hardware | Simulation |
|---|---|---|
| One sender: mean run | 6.4–6.7 | 6.15 |
| One sender: loss | 0.55–0.66 % | 0.58 % |
| Two senders: mean run | 3.3 | 3.63 |
| Two senders: single-frame runs | 13 % | 9 % |
| Two senders: loss | 0.68 % | 0.54 % |

The rival end conditions fail with two senders:
- **the port's own occupancy falling by 72 cells,** ADR-0014's rule: a mean run of 6.58;
- **the pool's occupancy falling by 72 cells:** 4.75;
- **a fixed time:** refuted by the 10 Mb/s run.

With one sender all of them are equivalent, which is why ADR-0014 fits the single-source data.

### Options considered

- **A: per-port limits only** (ADR-0013, ADR-0014). Refuted by the two-sender loss and delay cap.
- **B: an egress limit beside them.** It caps the queue, but the Waveshare would lose about 0.5 % of its polls, where it lost none.
- **C: a shared pool with a reserve per ingress port, and a drop state ended by a drain.** **Chosen.** It fits the single-sender data as ADR-0014 does, and the two-sender loss, runs and isolation besides. Every engine can reproduce it exactly, with no new event.
- **D: dynamic thresholds,** a port's share of the pool depending on the pool's free space, as on Broadcom-style chips. The data do not need it, and it is a vendor-specific policy. It stays out of scope.

---

## Decision

### The parameters

Three new optional switch parameters, in the units of ADR-0013 and ADR-0014: bytes, frames, or cells of `cell` bytes.

```
switch sw ports=8 cell=128 pool=710cells ingress_reserve=30cells ingress_drain=72cells
```

```python
from smolpy import Cells

sw = net.switch(
    "sw", ports=8, cell=128,
    pool=Cells(710), ingress_reserve=Cells(30), ingress_drain=Cells(72),
)
```

- **`pool`:** a buffer shared by the switch, for the frames queued beyond their ingress ports' reserves.
- **`ingress_reserve`:** room guaranteed to each ingress port, outside the pool.
- **`ingress_drain`:** a drop state of an ingress port ends once that much of the switch's buffer has been freed since it began. It is the alternative to `ingress_resume`.

### Valid combinations

**The ingress scope has one unit.** `ingress_buffer`, `pool`, `ingress_reserve`, `ingress_resume` and `ingress_drain` are all in the same unit, bytes, frames or cells, because they are all accounted from what each ingress port holds. The egress scope, `buffer` and `resume`, keeps its own unit.

| Parameter | Needs | May go with | Excludes |
|---|---|---|---|
| `pool` | — | `ingress_reserve`, `ingress_buffer` (a per-port cap), `buffer` (egress), `ingress_drain` or `ingress_resume` | — |
| `ingress_reserve` | `pool` | | |
| `ingress_drain` | `pool` or `ingress_buffer` | | `ingress_resume` |
| `ingress_resume` (ADR-0014) | `ingress_buffer` | `pool` | `ingress_drain` |

- **`pool` alone** is a shared limit with no reserve: every port's frames come out of the pool.
- **`ingress_reserve` without `pool`** would guarantee room within nothing, so it is an error.
- **`ingress_resume` stays a level of a port's own account,** so it needs `ingress_buffer`, as in ADR-0014.
- **`ingress_drain` only needs a drop state to end,** and a refusal can come from a pool or from `ingress_buffer`.
- **Every frame of the scenario must be admittable:** reserve + pool must hold the largest frame, in the scope's unit, as must `ingress_buffer` if set.
- **Validation:**
    - each is a whole number;
    - `pool` is at least 1;
    - `ingress_reserve` and `ingress_drain` are at least 1;
    - `ingress_reserve` is at most `ingress_buffer` if both are set.
- **Without the new parameters** nothing changes: the same results, event counts and benchmark hashes.

### Accounting

- **What a port holds.** Each ingress port p holds `occ_p`, its ingress occupancy of ADR-0013: what entered on p and is still queued on any port of the switch. A flooded frame counts once, and the frame in transmission is not counted.
- **The pool's use** is the sum over ports of what lies beyond each reserve:

    `pool_use = Σ_p max(0, occ_p − reserve)`.

    So a frame fills its port's reserve first, and the pool beyond it. A frame that crosses the reserve's edge takes the pool only for the part beyond it.
- **Release.** A frame leaves `occ_p`, and with it the pool, when the transmitter takes its last queued copy (ADR-0013's release rule). `pool_use` is updated at that instant.

### Admission

A frame of u units arriving on ingress port p, at the instant the switch forwards it:
1. **The drop state.** If p is in its drop state, the frame is refused.
2. **The per-port cap.** If `ingress_buffer` is set and `occ_p + u` exceeds it, the frame is refused.
3. **The reserve, then the pool.** The frame is admitted if `occ_p + u ≤ reserve`, or else if the pool has room for the part beyond the reserve:

    `pool_use + max(0, occ_p + u − reserve) − max(0, occ_p − reserve) ≤ pool`.

    Otherwise it is refused.
4. **Egress.** Each copy is then decided by its egress account, if `buffer` is set, as in ADR-0013.

A frame refused at steps 1–3 is **one ingress drop**, with no egress account looking at it, as in ADR-0013. The pool is accounted from the ingress ports, so a pool refusal is an ingress refusal. **The existing split covers the pool:** `ingress_drops_total` counts the frames refused by their ingress port, for its cap, the pool or its drop state, and `egress_drops_total` counts the copies refused at egress. The charge, once per frame if a copy was queued, is ADR-0013's.

### The scope of the pool: switch-wide

**Decided: one pool per switch, shared by all its ports,** as Realtek-style chips have one packet memory.

With one congested egress port, as in every measurement so far, a switch-wide pool and a pool per egress port admit the same frames, so the data cannot tell them apart. That stays open (Scope, below).

### The drop state, ended by a drain

The drop state stays per ingress port, as in ADR-0014.
- **Entering.** A port enters it when one of its frames is refused at step 2 or 3, and it is not already in it.
- **In the drop state** every frame of the port is refused, whatever its size, **even one that would fit in its reserve.**
- **Leaving, with `ingress_drain=D`.** The port leaves it at the release at which **the switch has freed at least D**, in the scope's unit, since the port entered. What counts:
    - **the buffer the switch frees,** at each release of ADR-0013: the units of a frame when the transmitter takes its last queued copy, from any ingress port and towards any egress port;
    - **not the frame being transmitted when the port entered:** it was released already.
    - **Against the quick simulation.** It counts a frame when its transmission ends, so it also counts the frame in transmission at entry. Here a frame is counted when it is taken. So the same end of a drop state comes about one frame earlier in the simulation's count: its D = 72 cells is about 60 here. The fit settles the value.
- **Leaving, with `ingress_resume=R`,** is ADR-0014's: at the release that brings the port's own occupancy to R.
- **Without either,** a refusal is tail drop, as in ADR-0013.

**Why the buffer freed switch-wide.**
- **It is what such a chip can count:** packet memory returned to its free list.
- **It is well defined with flooding.** A refused flooded frame has no single egress port whose departures could be counted.
- **It matches the data.** With one congested egress port, the departures from that port are the switch's departures, which is the rule the quick simulation fits.
- **It differs from the rivals that fail with two senders.** ADR-0014's own occupancy falling by D gives runs of 6.58. The pool's occupancy falling by D gives 4.75. Both count the space a full pool regains *net of new admissions*, where the drain counts it *gross*: another port's admissions do not delay the end of a port's drop state.

**The reserve within a drop state.** A port in its drop state loses even frames that fit in its reserve. That agrees with the data: a port enters a drop state only on a refusal, and the Waveshare's few small frames always fit in its reserve, so it never has been in one. In the reserve test, Pi 2's burst is admitted until its first refusal, and the rest of the burst is lost in its drop state. That is why 10 frames give 5 to 9.

**Same instant.** As in ADR-0013 and ADR-0014, decisions and releases at the same instant are taken in event order:
- a frame decided after the release that completes a drain sees its port admitting;
- a frame decided before it sees the drop state.

The releases are the transmitters' takes, ordered the same way in every engine.

**Flooding** charges once at ingress (ADR-0013). **Priorities:** the pool, the reserves and the drop states are shared by all 8 classes.

**Episodes and drop time** are ADR-0014's, per ingress account: `ingress_drop_episodes_total` and `ingress_drop_time_total`.

### Metrics

| Metric | On | Value at a sample | Unit |
|---|---|---|---|
| `pool_occupancy` | switch | `pool_use`: what the ports hold beyond their reserves | the pool's unit |

Per-port metrics stay open: `observe` addresses a node.

### Capabilities

| Feature | Rejects without it |
|---|---|
| `"buffer_pool"` | `pool` or `ingress_reserve` |
| `"drain_resume"` | `ingress_drain` |

### The IR stays version 2

- **Five new optional fields:**
    - `pool_bytes`, `pool_frames` and `pool_cells`, at most one, in the unit of `ingress_buffer_*` if that is set;
    - `ingress_reserve` and `ingress_drain`, in the scope's unit.
- **Written only when set.**

### ns-3

The pool, reserves and drains belong to the `SmolSwitch`, beside its ingress accounts:
- **The `switch` line** gains `<pool> <reserve> <drain>`, in the ingress scope's unit, 0 for none. The format becomes `smol-ns3 6`.
- **The pool's use and the switch's freed counter** are updated in `SmolDevice::StartNext`, where the ingress account is released.
- **Expected agreement:** Ethernet and PROFINET bit-exact with SimPy; Modbus and MQTT differ by TCP.

### Scope

- **In scope:** a switch-wide pool, a reserve per ingress port, and a drop state ended by a drain.
- **Still open:**
    - **why drop states of different ports do not overlap on the hardware** (As built, above);
    - **the pool's scope,** switch-wide or per egress port. A testbed run with two congested egress ports would tell: the laptop and Pi 2 each at 100 Mb/s, both receiving;
    - **a drain for egress accounts:** nothing measured needs it;
    - **dynamic thresholds;**
    - **per-port metrics.**

### Validation and tests

- **Unit tests:**
    - reserve-first accounting: `pool_use` against the ports' occupancies at every decision, by an audit;
    - refusal exactly when neither the reserve nor the pool has room;
    - a light port admitted in its reserve while the pool is full;
    - a drop state entered on a refusal and left at the release at which exactly D has been freed;
    - a drop state dropping a frame that fits in the reserve;
    - validation of every combination above.
- **Identities, on every engine:**
    - **one source, `pool=P ingress_reserve=R`, no drain or resume, is exactly `ingress_buffer=P+R`.** With one port holding frames, `pool_use = max(0, occ − R)`, so a frame fits exactly when `occ + u ≤ R + P`;
    - **in frames, one source and one frame size, `ingress_drain=D` is exactly `ingress_resume=L−D`.** In frames a port is refused only when full, so it always enters at `L`. In the drop state it admits nothing, so its occupancy only falls, by one frame at each release. With one source every release is one of its own frames. So D frames freed is its occupancy falling from L to L − D.
- **Two-source run lengths.** Two Poisson sources of 1 518 B at λ/2 each share the pool, and one egress port serves them, busy throughout. A refused port's run is 1 + the arrivals of its stream during T, the time for D to be freed. That is the residual of the frame in transmission, uniform by PASTA, then D/u − 1 services. The per-stream mean run is compared with 1 + (λ/2)·E[T], within a tolerance from several seeds.
- **Cross-engine:** deterministic scenarios join the bit-exact set of SimPy and Rust, event counts included, and are compared exactly on ns-3:
    - two sources filling the pool;
    - a light port admitted in its reserve while the pool is full;
    - drain-ended drop states at the instants of arrivals;
    - flooding with a full pool.
- **The full existing suite passes unchanged.**

### The testbed: calibration and validation

The parameters are fitted on one set of measurements and then checked, without refitting, on another.

**The calibration set:**
- the single-source burst tests: `2026-11-23_bufor_paczki.csv`, `2026-11-24_bufor_paczki_{512B,64B}.csv`;
- the reserve test with a constant background, `2026-11-24_rezerwa/rezerwa_tlo9000_const.csv`, with `rezerwa_tlo0.csv` as its control;
- the single-source 1 518 B loss runs: the loss, the mean run and the single-frame share of `2026-11-24_serie_strat/`, computed from the `.seq` files.

**The fit.**
- **The parameters:** P, R and D in cells of 128 B, by a grid search.
- **The objective:** the squared errors of the burst counts, the reserve-test counts and the loss-run statistics, each scaled by its own spread.
- **The models:**
    - each burst is N back-to-back frames at the pace the Pi put them on the wire (ADR-0014);
    - the reserve test adds a constant 9 000 frames/s from another port, with each repetition starting its burst at a different phase of the background.
- **The engine:** the fit runs on the Rust engine. It is bit-exact with SimPy on the deterministic bursts, and has the same distributions on the random runs.
- **First estimates:** 710 / 30 / 72 cells.

**The validation set,** predicted with the fitted values, reported against the hardware:
- **two Pis** at 2 × 4 050 and 2 × 3 900 frames/s:
    - loss per stream;
    - mean run and single-frame share, per stream;
    - the overlap of the two Pis' episodes;
    - RTT p99 and maximum;
    - polls lost or retransmitted per server, and the Waveshare's;
- **10 Mb/s** at 808 and 846 frames/s;
- **512 B and mixed sizes** (`2026-11-24_prognoza_adr0014/`);
- **the ADR-0013 ingress test** (`2026-11-24_ingress_test/`);
- **the batch ratio** of the overnight campaign: K = 8 at 7 500 frames/s, 0.55.

**Where the hardware numbers come from.** They are computed from the raw files by the benchmark itself (the `.seq` files for losses and runs, the `.csv` files for RTTs), and set beside the numbers quoted above. Each run's length and the overlap of episodes come from an audit of SimPy's decisions, reported as SimPy's.

**A residual gap to look at.** In single-source runs the hardware loses the Pi's responses at about 70–80 % of the model's rate: 0.44 % against about 0.52–0.55 %. The report says whether the pool and the reserve change that.

*As built (2026-11-24):*
- **Nothing changes without the new parameters.**
    - The benchmark hashes of B1–B4 are unchanged on SimPy and on Rust, event counts included.
    - Every earlier test passes. Two validation messages of ADR-0014's tests changed wording.
    - The `switch` line of format 6 gains three numbers.
- **The identities hold exactly,** every sample and the event count, on SimPy, Rust and ns-3:
    - one source with `pool=P ingress_reserve=R` is `ingress_buffer=P+R`, in frames and in cells;
    - in frames, one source, `ingress_drain=D` is `ingress_resume=L−D`.
- **The audit checks every decision:**
    - the pool's use against the ports' occupancies;
    - admission exactly when the port is not dropping and has room in its reserve or the pool;
    - each drained drop state ending at the release at which exactly D has been freed, one frame at a time;
    - frames dropped in a drop state though they fit in the reserve, and a light port admitted in its reserve while the pool is full.
- **Two sources' runs** match 1 + (λ/2)·E[T] over five seeds, with T the residual of a service and then D − 1 services.
- **Bit for bit.** `pool-two-sources`, `drain-tie` and `flood-full-pool` are bit-exact on SimPy, Rust and ns-3.
- **The fit** (`benchmarks/results/2026-11-24-1538-simpy-rust-ns3-jaws-zbook-shared-pool.json`, [Execution backends](../backends.md#a-shared-pool-the-ls108gp-calibrated-and-validated)):
    - **Stage 1** gives L = P + R = 770 cells and D = 72, from the bursts and the single-source runs. 770 is what ADR-0014's burst test pointed to.
    - **Stage 2,** the reserve test, gives R = 54, so P = 716. Any R from 42 to 60 fits about as well, with the unrecorded pace of Pi 2's 64 B bursts fitted beside it.
    - **The phases matter.** The background's cycle of refill and drain lasts about 7.7 ms, and phases spread over only 1.4 ms of it gave a reserve test that no R could fit.
- **The validation**, without refitting:
    - **the two-sender loss,** 0.61–0.64 % against 0.66–0.69 %, where ADR-0014 predicted 0.38 %;
    - **the halving of the runs,** 3.7–3.8 against 3.2–3.4;
    - **the Waveshare's isolation,** 0 lost;
    - **10 Mb/s, 512 B, mixed sizes and the batch ratio,** each as measured.
    - **The response-loss gap** of ADR-0014 is mostly a denominator: per poll sent the hardware retransmitted 0.49 %, and the model gives 0.44–0.62 %.
- **Not reproduced: the overlap of drop states.** On the hardware no Pi's drop state overlaps the other's (0 of 2 072). In the model, 1 092 of 2 461 do: a port refused while the pool is full is often refused again while the other drains it. So the model's two-sender runs are a little long, 3.8 against 3.3, and rarely single, 7 % against 13 %. Some mechanism keeps the other ports admitting while one port is in its drop state. It is the first open item.
- **The `smolpy-engine-rs` floor** is `>=1.15.0`, the release that carries this ADR. 1.14 refuses these scenarios by its capabilities.

### Steps

1. **The model:** the five IR fields, the parameters in Python and `.smol`, validation, the two capability features.
2. **SimPy:**
    - the pool, the reserves, the drain and `pool_occupancy`;
    - unit, identity and run-length tests;
    - Model Semantics updated.
3. **Rust:**
    - the same;
    - the bit-exact scenarios;
    - the `smolpy-engine-rs` floor raised at the release that carries this ADR, to that release, never to a pre-release, which would let pip pick development builds.
4. **ns-3:** format 6, and the exact comparisons.
5. **The testbed:** the fit on the calibration set, the predictions on the validation set, with the results recorded, and the example updated to the calibrated LS108GP.
6. **Documentation:**
    - Model Semantics, the language and DSL references, `backends.md`, the roadmap;
    - a note in ADR-0013 and ADR-0014.

---

## Consequences

**Positive**
- **One model of the testbed switch fits single and two-sender measurements:**
    - the loss;
    - the runs, and their halving with two senders;
    - the delay cap;
    - the Waveshare's isolation;
    - the reserve.
- **ADR-0013 and ADR-0014 are special cases.** With one source a pool and a reserve are an ingress limit, and a drain is a resume level, with exact identities.
- **No new event.** The engines stay bit-exact.
- **The fit and the validation are separate,** so the validation set tests the model.

**Negative / trade-offs**
- **A new `smolpy-engine-rs` release and ns-3 format version.**
- **More switch parameters,** and the ingress scope's five parameters must share a unit.
- **The pool's scope is a choice** that the data cannot make yet.
- **A drop state's end depends on the whole switch's releases,** so a port's runs depend on other ports' traffic. That is what the two-sender data show.
