# ADR-0016: A Drop State Shared by the Whole Switch

**Status:** Accepted  
**Date:** 2026-11-24  
**Amends:**
- ADR-0015: the drop state ended by a drain can belong to the switch instead of to each ingress port. This ADR also corrects a measurement ADR-0015 relied on: the two Pis' loss episodes do overlap.
- ADR-0007: one new optional IR field. The IR stays version 2.
- ADR-0004: the `smol-ns3` line format becomes version 7.
- [Model Semantics](../model-semantics.md) §3, §8, §9 and the open issues.

**Roadmap:** the first open item of ADR-0015, the overlap of two ports' drop states.

---

## Context

### 1. ADR-0015's "0 of 2 072" was an analysis error

ADR-0015 reports that the two Pis' loss episodes never overlap, 0 of 2 072, and that its model overlaps them in 1 092 of 2 461. It names "some mechanism keeps the other ports admitting" as its first open item. **The hardware number is wrong. The fault was in the analysis of the files, not in SMOLPy.**
- **What went wrong.** The episodes were merged by the senders' timestamps in the `.seq.t` files, but each Pi stamps with its own `CLOCK_MONOTONIC`. The two clocks differ by 63.77 s and drift apart by about 28 ppm, so the merge compared instants more than a minute apart.
- **With the clocks aligned** from the cross-correlation of the loss times (`stanowisko/model/zegary_dwa_rpi.py`), the two Pis' losses come together:
    - about 95 % of Pi 1's lost frames have a lost frame of Pi 2 within 1 ms;
    - 18 % of runs overlap one of the other Pi's, against 4 % with a 50 ms shift.
- **Why that is only a bound.** Two free-running clocks aligned from the losses alone keep an error of about 1 ms. So the true overlap at 100 Mb/s, where a run lasts about 0.4 ms, cannot be measured from these files.

### 2. A measurement on one clock

The laptop's port queue is FIFO, so the order in which the laptop receives frames is the order in which the switch admitted them. `stanowisko/pomiary/seq_sink.py` now also writes `<file>.seq.r`: the laptop's kernel receive times (`SO_TIMESTAMPNS`, `CLOCK_REALTIME`, ns) as little-endian `uint64`, in receive order. Both streams are stamped on that one clock.

**The setup** (`stanowisko/pomiary/wyniki/2026-11-24_dwa_rpi_10Mb/`, with `pomiar.sh` and `PORÓWNANIE.md`):
- **The laptop's link at 10 Mb/s.**
- **Two Pis,** each sending Poisson 1 518 B frames to its own UDP port, with PAUSE off on both.
- **The polls:** both Pis every 100 ms (FC03, 4 registers), and the Waveshare every 20 ms (FC02, 8 inputs).
- **The load** is the same total as ADR-0015's single-Pi 10 Mb/s runs: 2 × 405/s for 900 s and 2 × 425/s for 300 s.

**ADR-0015's fitted model was run before the measurement:** `prognoza_simpy.json`, from `stanowisko/model/prognoza_dwa_rpi_10Mb.py`, on SimPy with 3 seeds.

**The measure of a run** is the same on the hardware and in the model (`stanowisko/model/nakladanie.py`):
- **A run of a stream** is a maximal sequence of consecutive lost sequence numbers.
- **Its interval** runs from the receive time of the stream's last frame before the run to the receive time of its first frame after it. In the model the interval uses those frames' admission times. The FIFO egress maps admission times to receive times monotonically, so the overlaps are the same.
- **A run overlaps** if its interval intersects an interval of the other stream.
- **An episode** is a set of overlapping intervals of both streams, merged.

| | 2 × 405/s: hardware | ADR-0015 model | 2 × 425/s: hardware | ADR-0015 model |
|---|---|---|---|---|
| Loss per stream | 0.82 / 0.77 % | 0.62–0.74 % | 4.98 / 5.01 % | 4.64–4.75 % |
| Mean run | 3.34 / 3.20 | 3.6–3.8 | 3.38 / 3.39 | 3.8–3.9 |
| Single-frame runs | 14 % | 5–6 % | 12 % | 6 % |
| Runs overlapping the other Pi's | **96 %** (shifted 500 ms: 2 %) | 45–48 % | **97 %** (shifted: 5 %) | 49–51 % |
| Episodes with both Pis | 93 % | | 94 % | |
| Pi polls retransmitted (RTT ≥ 200 ms) | 0.61 / 0.62 % | 0.38–0.56 % | 4.13 / 4.22 % | 3.1–3.9 % |
| Waveshare polls retransmitted | 0 of 18 387 | 0 | 0 of 3 397 | 1 per seed |

- **A retransmission threshold at 200 ms.** At 10 Mb/s a round trip with no retransmission reaches about 90 ms. The Waveshare's maximum was 170 ms with no retransmission, because the queue wait adds to the skipping of one outstanding request (ADR-0012). The TCP RTO, 200 ms, separates them. `benchmarks/pool.py`'s `LONG_MS = 100` is right at 100 Mb/s only.
- **The background traffic** at rest was 8–28 packets/s of about 230 B, which is negligible.

**So the two Pis' runs are not independent, as ADR-0015's model makes them. They almost always come together, and each stream's run is about half the single-sender run.**

### 3. A model that fits: one drop state for the switch

A quick Python simulation (`stanowisko/model/symulacja_puli.py`, mode `globalny`, not SMOLPy) gives the switch one drop state, with P/R/D = 710/30/72 cells. Its drain is counted as in ADR-0015's "against the quick simulation" note.
- **Entering:** any ingress refusal for lack of room.
- **In the state,** a frame is admitted only if it fits in its own port's reserve: `occ_p + u ≤ R`.
- **Leaving:** once D cells have drained since entry.

| | Hardware | Switch-wide state | Per port (ADR-0015) | A port's own occupancy (ADR-0014) |
|---|---|---|---|---|
| 10 Mb/s, 2 × 405: mean run | 3.20–3.34 | 3.15–3.35 | 3.6–3.9 | 6.4–6.7 |
| Single-frame runs | 14 % | 15–18 % | 6–11 % | 1–2 % |
| Overlapping runs | 96 % | 95 % | 49 % | 42 % |
| 10 Mb/s, 2 × 425: mean run | 3.38–3.39 | 3.34–3.35 | 3.8 | 6.9 |
| Overlapping runs | 97 % | 97 % | 48 % | 49 % |
| 100 Mb/s, 2 × 4 050: mean run | 3.23–3.35 | 3.18–3.36 | 3.6 | 6.5–6.7 |
| Single-frame runs | 13 % | 13–15 % | 9–10 % | 1 % |
| One Pi, 8 090/s: mean run | 6.2–6.7 | 6.22 | 6.22 | 6.22 |
| A light port (50 small frames/s) | 0 lost | 0 / 3 602 | 0 / 3 602 | 0 / 3 602 |

**Why it fits.**
- **The runs split between the streams.** In an episode, each stream loses what it sends during the drain time T, about 6 services of a full frame. Each Pi sends about 3 frames in T at either link speed. So both Pis are in almost every episode, 1 − e⁻³ ≈ 95 %, and each stream's run is about half the single-sender run.
- **It is the same at 10 and at 100 Mb/s.** So the cause is the switch, not the senders' microsecond timing.
- **With one sender it is ADR-0015's model.** The sender's port stays beyond its reserve for the whole drain, so the state admits nothing either way (the identity below).

**The reserve test** (`burst()` in the same file: a constant 9 000/s background, R = 54) fits about as well as with ADR-0015's per-port state. The 64 B bursts come out a little low, for example a median of 80 for 100 × 64 B against 100 measured. R = 0 does not fit. So the reserve stays, and R must be refitted under the new rule.

**What the quick model cannot check:**
- **The Pi's responses.** A response is now lost in any drop state while the Pi's port is beyond its reserve, not only in the Pi's own state. That should move the prediction towards the hardware's 0.61 %.
- **The loss with two senders** stays about 20 % below the hardware in every variant, 0.60–0.74 % against 0.79 %. With one Pi at the same load, the hardware lost 0.60 %. At ρ ≈ 0.995 the spread across seeds is about ±10 %.

### Options considered

- **A: a drop state per ingress port** (ADR-0015). Refuted by the overlap: 49 % of runs against 96 %, and runs of 3.6–3.9 against 3.2–3.3.
- **B: a drop state per egress port,** entered by a refusal of a frame towards it. With one congested egress port, as in every measurement, it admits the same frames as C. With flooding it is ill defined, because a refused flooded frame has no single egress port. It has also no counterpart in the data, so C is the simpler statement.
- **C: one drop state for the switch, with the reserve exempt.** **Chosen.** It fits the overlap, the halving of the runs and the single-frame share, at both link speeds. It keeps the Waveshare isolated, and with one sender it is exactly ADR-0015. Every engine can reproduce it exactly, with no new event.
- **D: C without the reserve exemption.** The Waveshare would lose about the fraction of time the switch spends in the state, about 0.6 % of its polls at 2 × 4 050/s. It lost 0 of 6 000.

---

## Decision

### The parameter

One new optional switch parameter, `ingress_drop`, the scope of the ingress drop state:

```
switch sw ports=8 cell=128 pool=710cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch
```

```python
sw = net.switch(
    "sw", ports=8, cell=128,
    pool=Cells(710), ingress_reserve=Cells(60), ingress_drain=Cells(72),
    ingress_drop="switch",
)
```

- **`port`** is the default, and today's behaviour (ADR-0014, ADR-0015): each ingress account has its own drop state.
- **`switch`:** one drop state for the whole switch, defined below.
- **Without the parameter nothing changes:** the same results, event counts and benchmark hashes.

### Valid combinations

| `ingress_drop` | Needs | Excludes |
|---|---|---|
| `port` | — | — |
| `switch` | `ingress_drain` | `ingress_resume` |

- **`switch` needs `ingress_drain`.** A switch-wide state needs an end that belongs to the switch, and the drain counts the buffer the switch frees.
- **`switch` excludes `ingress_resume`.** A resume level is a level of one port's account. A switch-wide state could end at a level of the pool's use, but ADR-0015 found that rule refuted, with runs of 4.75 against 3.3. So no rule is offered that nothing supports. `ingress_drain` already excludes `ingress_resume`, so the validation adds only the need for a drain.
- **`switch` does not need `pool`.** `ingress_drain` needs `pool` or `ingress_buffer` (ADR-0015), so `switch` with per-port caps only is valid. With no pool there is no reserve, so in the state nothing is admitted: every port is refused until the switch has freed D. The rule costs nothing to keep, and it is well defined, though nothing measured needs it.
- **The values:** `port` or `switch`, nothing else.

### Semantics

The accounting is ADR-0015's: `occ_p`, the reserve, `pool_use`, and the release when the transmitter takes the last queued copy of a frame. Only the drop state changes.

A frame of u units arriving on ingress port p, at the instant the switch forwards it, in the `switch` scope:

1. **In the state.** If the switch is in its drop state, the frame is admitted if `occ_p + u ≤ reserve`, and refused otherwise. Every other frame is refused, whatever its size, even if the pool has room. Without `ingress_reserve` (a reserve of 0) nothing is admitted.
2. **The per-port cap.** If `ingress_buffer` is set and `occ_p + u` exceeds it, the frame is refused.
3. **The reserve, then the pool,** as in ADR-0015.
4. **Egress:** each copy is decided by its egress account, if `buffer` is set, as in ADR-0013.

A frame admitted at step 1 needs nothing more of the ingress scope: the validation keeps `ingress_reserve ≤ ingress_buffer`, and a frame within the reserve takes nothing of the pool.

#### What starts the state

**The switch enters its drop state at a refusal at step 2 or step 3, when it is not in the state already.** A refusal by the per-port cap starts it too:
- **It is the same buffer manager.** A cap and a pool are two limits on the same packet memory, and the drop state is the manager's response to running out of room. ADR-0015 starts a port's drop state at either step, and this keeps that rule, only the state's scope changes.
- **It keeps the identity with ADR-0015** for every switch configuration, not only for those without a cap.
- **Otherwise a capped port would lose frames one at a time,** by tail drop, while every other port goes on admitting. That is ADR-0013's behaviour, which the drain was introduced to replace.

A refusal at egress (step 4) does not start it. The egress scope keeps its own accounts, with their `resume` (ADR-0014).

#### The reserve exemption

**In the state, a frame that fits in its own port's reserve is admitted.** This differs from ADR-0015, where a port in its drop state refuses even frames that fit in its reserve.
- **It is what keeps the Waveshare isolated.** Without it, the Waveshare's responses would be lost in every drop state, about 0.6 % of its polls at 2 × 4 050/s, where it lost none (option D).
- **In ADR-0015 the difference could not show.** A port enters its drop state only on its own refusal, and a light port is never refused. So ADR-0015's rule and this one agree on every port that is not in a drop state of its own, which is every light port.
- **The reserve test** depends on it. A burst from Pi 2 is admitted while its port holds less than its reserve, whether or not the background's refusals have put the switch in the state. So R is refitted under this rule.

#### Leaving: ADR-0015's drain

**The switch leaves the state at the release at which it has freed at least D, in the scope's unit, since it entered.** What counts is ADR-0015's:
- the units of a frame when the transmitter takes its last queued copy, from any ingress port and towards any egress port;
- the frames admitted in the state by the reserve exemption, when they are taken;
- not the frame in transmission at entry, which was released already.

#### A refusal in the state does not restart the drain

**The drain is counted from the entry only.** A refusal while in the state changes nothing but the drop count. That is the quick model's rule, and ADR-0015's for a port's drop state.

**The data tell the two rules apart, already with one sender.** With a restart, the state would end only after D had been freed with no refused arrival in between. At ρ ≈ 1 that is about 6 services with no arrival of the sender, probability about e⁻⁶. The runs would be geometric with a mean in the hundreds. The single-sender runs of 6.2–6.7 (ADR-0015's calibration) are 1 + λT, the rule without a restart.

#### The same instant, flooding and priorities

- **The same instant.** ADR-0015's event-order rules hold:
    - a frame decided after the release that completes the drain sees the switch admitting, by steps 2–3;
    - a frame decided before it sees the state, and the exemption of step 1.
- **Flooding** is charged once at ingress (ADR-0013). A flooded frame in the state is admitted if it fits in its ingress port's reserve.
- **Priorities.** One state for all 8 classes.
- **Egress.** The `buffer` and `resume` of egress accounts are unchanged.

### The identity with ADR-0015 for one sender

**Claim.** Let every frame that enters the switch enter on one port p. Let `L` be p's limit, `min(ingress_buffer, reserve + pool)` with an absent term left out, so that a frame of u fits exactly when `occ_p + u ≤ L`. Let `R` be the reserve (0 without one), and u_min and u_max the smallest and largest frame of p in the scope's unit. If

    L − R ≥ D + u_max − u_min − 1,

then `ingress_drop=switch` gives exactly the results of `ingress_drop=port`, every decision and every event, on every arrival pattern.

**Proof.**
1. **With only p holding frames,** `pool_use = max(0, occ_p − R)`. So steps 2–3 refuse a frame of u exactly when `occ_p + u > L`, in both scopes.
2. **Outside a drop state the two scopes decide alike,** by steps 2–3. They enter at the same refusals, and they leave at the same releases, because both count the switch's frees since the entry.
3. **In a drop state, ADR-0015 refuses every frame.** The `switch` scope refuses one unless `occ_p + u ≤ R`. It remains to show that this never holds.
4. **Bound the occupancy.** The entry was a refusal, so `occ_entry ≥ L − u_max + 1`. Suppose nothing has been admitted in the state so far, by induction. Then the occupancy has only fallen, by exactly what the switch has freed since the entry, and that is at most D − 1, or the state would have ended. So at any decision in the state, `occ_p ≥ L − u_max − D + 2`.
5. **So the exemption never applies.** `occ_p + u ≥ L − u_max − D + 2 + u_min ≥ R + 1`, by the condition. The induction holds, and the two runs are the same. ∎

**With one frame size u the exact condition is weaker,** because the occupancy is always a multiple of u:

    ⌊R/u⌋ ≤ max(0, ⌊L/u⌋ − ⌊(D − 1)/u⌋).

- **It is sufficient.** A refusal happens only at `occ = u⌊L/u⌋`. In the state, the switch has freed a multiple of u that is below D, so at most `u⌊(D − 1)/u⌋`. So the occupancy never falls below `u · max(0, ⌊L/u⌋ − ⌊(D − 1)/u⌋)`.
- **It is necessary.** If the condition fails, an arrival just after the ⌊(D − 1)/u⌋-th take of the state finds the port at that lowest occupancy, and fits in the reserve.

**The LS108GP satisfies it by far.** With L = 770, R = 60 and D = 72 cells, a full frame of 12 cells and a response of 1 cell, 710 ≥ 82.

**The episodes agree too if D ≥ u_max.** After a state ends, the occupancy is at most `L − D`, so the next frame fits, and every entry follows an admission. Then the switch's entries are the port's runs of refusals, and both metrics of §Metrics are the same in both scopes.

**The identity extends to light ports.** Other ports may send too, if each of their frames fits in its port's reserve when it arrives:
- in the `port` scope such a frame is admitted at step 3 and never starts a drop state;
- in the `switch` scope it is admitted at step 3, or at step 1 in the state;
- it takes nothing of the pool, so p's limit is unchanged.

That holds for both runs at once, by induction on the decisions. It covers the single-Pi testbed runs, where the polls and the Waveshare's responses always fit.

**With several senders the scopes differ,** which is the point of this ADR.

### Metrics

In the `switch` scope:
- **`ingress_drop_episodes_total`** counts the switch's entries into its drop state;
- **`ingress_drop_time_total`** is the switch's time in it.

In the `port` scope both stay ADR-0014's: the sum over the ingress accounts of their refusal runs, and of their times in the drop state.

| Metric | Scope `port` | Scope `switch` |
|---|---|---|
| `ingress_drops_total` | frames refused at ingress | the same |
| `ingress_drop_episodes_total` | Σ over ports: maximal runs of refusals | entries of the switch into its state |
| `ingress_drop_time_total` | Σ over ports: time in a port's drop state | the switch's time in its state |

- **In the `switch` scope, `drops / episodes` is not a stream's mean run.** An episode holds both streams' runs, so it is the sum of the runs in an episode.
- **A stream's runs need per-port drop counts.** `observe` addresses a node, so per-port metrics stay open, and the benchmark computes the runs from an audit of SimPy's decisions, as ADR-0015 does.

### Capabilities

| Feature | Rejects without it |
|---|---|
| `"switch_drop_state"` | `ingress_drop`, either value |

An explicit `port` is gated too. The field is in the IR, and an engine without the feature does not know it.

### The IR stays version 2

- **One new optional field:** `ingress_drop`, `"port"` or `"switch"`.
- **Written only when set.**

### ns-3

- **The `switch` line** gains `<scope>`, 0 for `port` and 1 for `switch`. The format becomes `smol-ns3 7`.
- **The drop state** moves from the ingress accounts to the `SmolSwitch` in the `switch` scope. It is entered in `AdmitIngress` and left in `Release`, where the freed counter is kept already.
- **Expected agreement:** Ethernet and PROFINET bit-exact with SimPy; Modbus and MQTT differ by TCP.

### Scope

- **In scope:** a drop state for the whole switch, with the reserve exempt, ended by ADR-0015's drain.
- **Still open:**
    - **the pool's scope,** switch-wide or per egress port. Two congested egress ports would tell;
    - **the two-sender loss gap,** about 20 % below the hardware in every variant;
    - **per-port metrics;**
    - **dynamic thresholds.**

### Validation and tests

- **Unit tests:**
    - entry at a refusal from any port, at the cap or at the pool;
    - another port's frame refused in the state although the pool has room;
    - a frame admitted in the state because it fits in its port's reserve;
    - leaving at the release at which exactly D has been freed;
    - no restart of the drain at a refusal in the state;
    - the episodes and time metrics in the `switch` scope;
    - validation of every combination above.
- **Identities, on every engine:**
    - without the parameter nothing changes, and `ingress_drop=port` is the same;
    - **one sender:** `switch` equals `port` when the condition above holds, in cells with mixed sizes and in frames.
- **Two Poisson senders,** at λ/2 each, one frame size, no reserve, the egress busy throughout. In the state nothing is admitted, so the takes that end it are those of frames already queued, and T, its duration, is fixed at entry. The arrivals after it are independent of it. So, given T:
    - **the stream refused first** loses 1 + Poisson((λ/2)·T) in the episode;
    - **the other stream** loses Poisson((λ/2)·T), so it is in the episode with probability `1 − E[exp(−(λ/2)·T)]`.

    The test takes T and each stream's losses per episode from an audit. It compares the share of episodes with both streams, the per-stream mean loss in an episode and the single-frame share with these, over several seeds, within a tolerance from the sample size. T itself is bounded by ADR-0015's residual and then D/u − 1 services.
- **Bit-exact on SimPy and Rust,** event counts included, and exactly on ns-3:
    - two ports refused in one episode;
    - a light port admitted in its reserve while the switch is in the state;
    - a refusal in the state;
    - the release that ends the state at the instant of arrivals;
    - flooding in the state.
- **The full existing suite passes unchanged.**

### The testbed: refit, then validate

ADR-0015's split between calibration and validation holds.

**Calibration,** ADR-0015's set:
- the single-source bursts and loss runs: by the identity they do not move, so L = P + R = 770 and D = 72 stand;
- the reserve test with its constant background: R, and the pace of Pi 2's 64 B bursts, are refitted under the `switch` scope. The grid is reported as in ADR-0015, with how sharply it determines R.

**Validation,** without refitting:
- **two Pis at 10 Mb/s,** 2 × 405 and 2 × 425, a new case in `benchmarks/pool.py`:
    - the hardware's numbers from the raw files, with the overlap from the `.seq.r` files by the measure above;
    - the model's from the audit, by the same measure;
    - the loss and mean run per stream, the single-frame share, the overlapping runs and the episodes with both Pis;
    - the Pi and Waveshare polls retransmitted, at the 200 ms threshold;
    - the RTT p99 and maximum;
- **two Pis at 100 Mb/s,** 2 × 4 050 and 2 × 3 900: the loss, runs, single-frame share, polls and the Waveshare.
    - **No hardware overlap for this case.** Its files have no receive times, and the aligned clocks give only a bound, about 18 %, with ±1 ms of alignment. The model's overlap is reported without a hardware value;
- **unchanged from ADR-0015:** 10 Mb/s with one Pi, 512 B, mixed sizes, the ADR-0013 ingress test and the batch ratio. By the identity they do not move with R if L stays, and the report says if any does;
- **the two-sender loss gap,** about 20 %, and the Pi-response gap, reported as they come out against ADR-0015's numbers.

**The example** `examples/example_ingress_buffer.smol` becomes the refitted switch with `ingress_drop=switch`, with ADR-0015's line kept as a comment.

*As built (2026-11-24):*
- **Nothing changes without the parameter.**
    - The benchmark hashes of B1–B4 are unchanged on SimPy and on Rust, event counts included.
    - Every earlier test passes. `ingress_drop=port` gives the same results and events as no parameter.
    - The `switch` line of format 7 gains one number, 0 or 1.
- **The audit checks every decision** of the switch-wide state, on SimPy:
    - outside the state, admission exactly by the cap, the reserve and the pool, and a refusal entering it, from either port and by the cap as by the pool;
    - in it, admission exactly within the port's reserve, a light port admitted there, and other ports refused although the pool had room;
    - each state ending at the release at which exactly D had been freed, one frame at a time, with the drain's start never moved by a refusal in the state;
    - the episodes metric equal to the entries, and the drop time to their durations.
- **The one-sender identity holds exactly,** every sample and the event count, on SimPy, Rust and ns-3:
    - in frames, P ≥ D − 1;
    - in cells, with frames of 12 and 1 cells, P ≥ D + 10, and a light port of small frames beside it;
    - with a per-port cap below the pool.

    With P = 4 < D − 1 = 7 in frames the scopes differ, as the condition says.
- **Two senders,** 2 × 4 100 frames/s of 1 518 B, no reserve, a pool of 40 frames, D = 6, five seeds, 493 episodes. Given each episode's T:

    | | Model | Expected |
    |---|---|---|
    | The other stream's losses in an episode | 2.710 | 2.695 |
    | The first stream's, less the refusal that entered | 2.722 | 2.695 |
    | Episodes with both streams | 92.1 % | 93.2 % |
    | Mean run per stream | 3.35 | 3.31 |
    | Single-frame runs | 12.7 % | 13.0 % |

    - **T is shorter than ADR-0015's formula:** 657 µs against `(D − ½)·S` = 677 µs. Every T lies within (D − 1)·S and D·S, so the residual is in (0, S], as the formula says, but its mean is 42 µs, not S/2 = 62 µs. A refusal needs a full pool, which is likelier late in a service, so the residual at a refusal is not uniform. ADR-0015's run-length test assumed it was and passed within its tolerance. This test conditions on T instead, which is exact.
- **Bit for bit.** `switch-drop-two-ports`, `switch-drop-tie` and `switch-drop-flood` are bit-exact on SimPy, Rust and ns-3, with SimPy's and Rust's event counts equal. Between them they cover:
    - two ports refused in one episode;
    - a light port admitted in its reserve in the state;
    - refusals in the state;
    - the end of a state at the instant of an arrival;
    - flooding in the state.
- **The refit** (`benchmarks/results/2026-11-24-2122-simpy-rust-ns3-jaws-zbook-switch-drop-state.json`, [Execution backends](../backends.md#a-drop-state-for-the-whole-switch-the-ls108gp-refitted-and-validated)):
    - **Stage 1 stands.** At L = 770 and D = 72 every burst count and the loss runs are the same under both scopes: 0.593 % lost, runs of 6.40.
    - **R = 60 cells, so P = 710,** with the 64 B bursts at 110 000 frames/s as in ADR-0015. The error is 0.25, and 0.36–0.57 for R from 48 to 72. Below 42 it grows fast: 1.2 at 36, 2.1 at 24, 5.4 at 12 and 33 at 0. So the reserve is needed, and R is determined to about ±12 cells.
    - **These are the quick model's first estimates** but for R: 710 / 30 / 72.
- **The validation**, without refitting, three seeds, SimPy's runs and overlaps from an audit:

    | | Hardware | `switch` | `port` (ADR-0015's fit) |
    |---|---|---|---|
    | 10 Mb/s, 2 × 405: loss per stream | 0.82 / 0.77 % | 0.55–0.65 % | 0.55–0.67 % |
    | Mean run | 3.34 / 3.20 | 3.20–3.34 | 3.69 |
    | Single-frame runs | 14 % | 13–14 % | 7 % |
    | Runs overlapping the other Pi's (shifted 500 ms) | 96 % (2 %) | 97 % (3–5 %) | 45 % |
    | Episodes with both Pis | 93 % | 93–95 % | |
    | Pi polls retransmitted | 0.61 / 0.62 % | 0.44–0.56 % | 0.31–0.45 % |
    | Waveshare | 0 of 18 387 | 0 of 71 022 | |
    | 10 Mb/s, 2 × 425: loss per stream | 4.98 / 5.01 % | 4.71–4.90 % | 4.71–4.93 % |
    | Mean run | 3.38 / 3.39 | 3.36–3.45 | 3.81 |
    | Single-frame runs | 12 % | 12 % | 6 % |
    | Runs overlapping (shifted) | 97 % (5 %) | 97 % (11–13 %) | 50 % |
    | Episodes with both Pis | 94 % | 94 % | |
    | Pi polls retransmitted | 4.13 / 4.22 % | 3.6–4.6 % | 2.9–3.6 % |
    | 100 Mb/s, 2 × 4 050: loss per stream | 0.66 / 0.69 % | 0.57–0.68 % | 0.57–0.69 % |
    | Mean run | 3.24 / 3.35 | 3.22–3.39 | 3.72 |
    | Single-frame runs | 13 % | 13 % | 7 % |
    | Runs overlapping | not measurable | 96 % | 47 % |
    | Pi polls retransmitted | 0.53 / 0.54 % | 0.33–0.40 % | 0.30–0.58 % |
    | 100 Mb/s, 2 × 3 900: loss per stream | 0.036 / 0.039 % | 0.01–0.04 % | 0.01–0.04 % |
    | Mean run | 3.14 / 3.29 | 3.1–3.6 | 3.67 |

    - **The overlap, the halving of the runs and the single-frame share** come out as measured, at 10 and at 100 Mb/s. The per-port state of ADR-0015 gets none of them.
    - **The Waveshare loses nothing,** on every engine.
    - **The round trips:** p99 and maximum of 82.2 / 86.5 ms at 2 × 405, against 82.8 / 87.3 ms for Pi 2. Pi 1's one round trip of 139.9 ms on the hardware is not modelled.
    - **Rust and ns-3** give the same loss and polls within their seeds' spread.
- **The single-source cases do not move.** 10 Mb/s at 808 and 846, 512 B, mixed sizes, the ADR-0013 test and the batch case give ADR-0015's results bit for bit, every seed, on SimPy, Rust and ns-3. L = 770 is unchanged, and the light ports always fit in their reserves. The example's numbers are ADR-0015's.
- **The Pi's responses.** In the switch's state a Pi's response is lost whenever its port is beyond its reserve, not only in its own state:
    - **at 10 Mb/s that moves the prediction towards the hardware:** 0.49 % against 0.37 % per port at 2 × 405, with 0.61 % measured; 4.0 % against 3.1 % at 2 × 425, with 4.2 % measured;
    - **at 100 Mb/s it does not:** 0.38 % against 0.45 %, with 0.53 % measured. That is within the seeds' spread, 0.30–0.58 %, and Rust and ns-3 give 0.30–0.50 % and 0.40–0.56 %.
- **The two-sender loss gap stays.** The scope does not change the loss per stream. The model is below the hardware by 22 % at 2 × 405 (0.62 % against 0.79 %), 3 % at 2 × 425, 9 % at 2 × 4 050, and about 37 % at 2 × 3 900 from about 30 runs a seed. With one Pi at the same load, the hardware lost 0.60 % and the model 0.68 %.
- **The measure of polls.** A poll still in flight at the end of a run is not retransmitted. The benchmark counts a model's poll as retransmitted by its round trip of 200 ms or more, or by its timeout, not as unanswered: at 10 Mb/s a request is nearly always in flight when the run ends.
- **The `smolpy-engine-rs` floor** is raised to the release that carries this ADR, once it is published.

### Steps

1. **The model:** the IR field, the parameter in Python and `.smol`, validation, the capability feature.
2. **SimPy:**
    - the switch's drop state, its exemption and its metrics;
    - unit, identity and two-sender tests.
3. **Rust:**
    - the same;
    - the bit-exact scenarios;
    - the `smolpy-engine-rs` floor raised at the release that carries this ADR, to that release, never to a pre-release.
4. **ns-3:** format 7, and the exact comparisons.
5. **The testbed:** the refit of R, the validation, the results recorded, and the example updated.
6. **Documentation:**
    - Model Semantics, the language and DSL references, `backends.md`, the roadmap;
    - the correction in ADR-0015, `docs/backends.md` and `benchmarks/pool.py`.

---

## Consequences

**Positive**
- **The two-sender runs are modelled as measured:** their overlap, their halving and their single-frame share, at 10 and at 100 Mb/s.
- **ADR-0015's per-port state is a special case:** with one sender, exactly, under a stated condition.
- **The Waveshare stays isolated,** by the reserve exemption.
- **ADR-0015's record is corrected,** with the error and its cause stated.
- **No new event.** The engines stay bit-exact.

**Negative / trade-offs**
- **A new `smolpy-engine-rs` release and ns-3 format version.**
- **One more switch parameter,** and a second meaning of the episodes metric, by scope.
- **The two-sender loss gap stays open,** and so does the pool's scope.
- **A port's losses depend on every other port's traffic,** more than in ADR-0015: any port's refusal starts the state for all. That is what the data show.
