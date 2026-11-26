# ADR-0017: The Switch's Drop State Ends on the Fall of Its Occupancy, and a Light Port Is Protected

**Status:** Accepted (2026-11-26). Part 1 is settled by the data and built. Part 2, the rule that protects a light port, was decided (§6, Decision Part 2) and confirmed by a prediction saved before the measurement. It is built, and validated against the hardware with no refitting (As built, both parts).  
**Date:** 2026-11-25, Part 2 2026-11-26  
**Amends:**
- ADR-0016: what counts towards the drain of the switch-wide drop state; which frames are admitted in it.
- ADR-0015: how the shared pool's use is counted, and a dynamic threshold on the fit (Part 2, both optional).
- ADR-0007: new optional IR fields: one for Part 1, three for Part 2. The IR stays version 2.
- ADR-0004: the `smol-ns3` line format becomes version 8 (Part 1), then 9 (Part 2).
- [Model Semantics](../model-semantics.md) §3 and the open issues.

**Roadmap:** ADR-0016 was refuted out of sample for unequal loads; this ADR is its correction.

---

## Context

### 1. ADR-0016 out of sample: equal loads fit, unequal loads do not

A campaign of 48 runs at 100 Mb/s (`stanowisko/pomiary/wyniki/2026-11-25_noc/`, with `plan.txt`, `noc.sh`, `RAPORT.md`, `PORÓWNANIE.md`): two Pis, Poisson 1 518 B, PAUSE off, 16 cases × 3 runs × 180 s, in random order. ADR-0016's predictions (SMOLPy, SimPy, 2 seeds × 120 s, `prognoza_adr0016_<case>.json`) were saved before the first run.

**Equal loads fit.**

| Per Pi | Hardware: loss | ADR-0016 | Hardware: mean run | ADR-0016 |
|---|---|---|---|---|
| 3 950/s | 0.060–0.087 % | 0.076–0.086 % | 3.1–3.5 | 3.0–3.3 |
| 4 050/s | 0.60–0.66 % | 0.54–0.70 % | 3.2–3.4 | 3.25–3.32 |
| 4 150/s | 2.13–2.35 % | 2.09–2.32 % | 3.36–3.41 | 3.31–3.34 |

Runs overlapping the other Pi's: 95–97 % on the hardware, 95–97 % in the model (by admission times, §2).

**Unequal loads do not.** Pi 1 sends the rest of 8 090/s, so the total is the same. The table gives the light stream's loss against the heavy one's.

| Light Pi 2 | Hardware: light loss | light / heavy | ADR-0016: light / heavy | Hardware: heavy mean run | ADR-0016 |
|---|---|---|---|---|---|
| 300/s | 0 | 0 | 0.1 | 6.26–6.42 | 6.12–6.15 |
| 600/s | 0–0.002 % | 0 | 0.55 | 6.26–6.39 | 5.92–5.94 |
| 1 200/s | 0.006–0.014 % | 0.013 | 0.95 | 6.34–6.44 | 5.33–5.50 |
| 1 800/s | 0.14–0.18 % | 0.21 | 1.0 | 5.99–6.08 | 4.90–4.93 |
| 2 400/s | 0.46–0.49 % | 0.67 | 1.0 | 4.98–5.10 | 4.44–4.50 |
| 3 000/s | 0.58–0.70 % | 0.96 | 1.0 | 4.10–4.20 | 3.99–4.08 |
| 3 600/s | 0.61–0.64 % | 0.98 | 1.0 | 3.56–3.66 | 3.54–3.60 |
| 1 200/s, total 8 400/s | 0.038–0.050 % | 0.011 | 1.0 | 6.64–6.69 | 5.58–5.61 |

- **A light port is protected far beyond its reserve.** At 1 200/s it holds about 110 cells on average, about twice R, and loses 1/80 of what the heavy port loses.
- **The protection fades between 1 200 and 3 000/s.** Above that both ports lose alike, as ADR-0016 says.
- **The heavy port's runs are longer than ADR-0016's,** by up to 20 %, wherever the light port is protected.
- **The roles swapped** (light on Pi 1, heavy on Pi 2) give the mirror image, and each Pi alone gives runs of 6.28–6.45. So it is the load, not the Pi or the port.
- **The Waveshare** lost no poll in any of the 48 runs.

### 2. Measuring at 100 Mb/s: admission times, not receive times

**The laptop's receive times pair frames.** Its NIC (e1000e) hands frames over in batches: 86 % in pairs, the rest in threes, at most 6. At 100 Mb/s a frame lasts 123 µs, so the two frames of a pair carry the same `.seq.r` stamp, and their order between the two streams is unknown.

**That made the overlap look 76–80 %.** Two runs that touch, the end of one stream's interval being the start of the other's in the same pair, were counted as disjoint by the strict comparison. That is about 20 % of the runs.

**Admission times fix it.** `stanowisko/model/kolejka.py` maps each frame's send time (`.seq.t`, the Pi's clock) to the laptop's clock: the lower envelope of `rx − tx` in 1 s windows, fitted by a line (drift below 6 ppm). With those times the overlap is 95–97 % in every equal-load run, and 96 % in ADR-0016's own 100 Mb/s check, which had reported 76 %. At 10 Mb/s both measures agree, at 96–97 %. **ADR-0016's model was right about the overlap at both speeds.**

**The admission times have an error.** The Pi's send latency has a median of about 70 µs, a p90 of about 250 µs, and an occasional few ms. So one frame's reconstructed total occupancy is good to about ±20 cells. A port's own occupancy is much better, because only that port's frames count.

**From 2026-11-25 the receiver is a Mac mini** (Ubuntu, tg3) with an interrupt per frame (`rx-usecs 1 rx-frames 1`): 75 % of the gaps lie in 118–130 µs, and none are paired. A control run against the laptop agrees within the spread (§6).

### 3. The state ends when the occupancy has fallen by D

**Divide the heavy port's mean run by its rate.** That gives the time the switch spends in its drop state.

| Case | Heavy mean run | Time in the state | T₀ / (1 − λ_light / μ) |
|---|---|---|---|
| One Pi, 8 090/s | 6.43 | 0.795 ms | 0.792 ms (T₀) |
| Light 300/s | 6.34 | 0.814 ms | 0.822 ms |
| Light 600/s | 6.34 | 0.846 ms | 0.855 ms |
| Light 1 200/s | 6.38 | 0.926 ms | 0.929 ms |
| Light 1 200/s, total 8 400/s | 6.66 | 0.925 ms | 0.929 ms |
| Roles swapped, light 1 200/s | 6.36 | 0.922 ms | 0.929 ms |
| Light 1 800/s | 6.03 | 0.959 ms | 1.017 ms |
| Light 2 400/s | 5.03 | 0.884 ms | 1.124 ms |
| Equal, 2 × 4 050/s | 3.30 | 0.815 ms | 1.574 ms |

- **While the light port is protected, the state lasts T₀/(1 − λ_light/μ), within 1 %.** μ is the egress rate in frames. The light port's frames admitted in the state fill what the egress frees, so the state ends only when the occupancy, net of them, has fallen by D.
- **ADR-0016's drain does not do that.** It counts only what is freed, so the state lasts T₀ whatever is admitted in it, and the heavy run at 1 200/s is 5.4.
- **Where the light port is refused too** (2 400/s and up), less is admitted in the state, and the time returns towards T₀.
- **The occupancy confirms it, more weakly.** The occupancy falls between the last admitted heavy frame before a run and the first after it. The median fall is 72 cells, 6 frames, in every case. The mean is 80.5 with one sender and 79 at 1 200/s. A drain of D freed cells would give about 69 at 1 200/s, a net fall of D about 82.

### 4. The reserve stands

The reconstructed occupancy never exceeds 816–828 cells with two senders, and 780 with one. That is P + 2R = 830 and P + R = 770, with ADR-0016's P = 710 and R = 60. It is an independent check of R, from a measurement that does not fit it.

*Revised by §6.4 (2026-11-26).* L = P + R = 770 for one sender stands. The two-sender reading does not. The Mac's reconstruction, aligned on a Pi sending alone, puts the total at the start of the state at 780–792 cells whatever the split between the ports. The total's 99.9th percentile is 804 cells, and 816–828 are its single highest values, within the reconstruction's ±20 cells. A second reserve is not set aside.

### 5. What protects a light port

**A port's own occupancy at its frames' fates in the state** (`stanowisko/model/kolejka.py`, the heavy port's runs taken as the state):
- **There is no sharp threshold.** The share of the light port's frames refused rises from about 10 % to about 80 % between 190 and 290 cells.
- **The midpoint moves with the load:** about 225 cells at 1 800/s, 235 at 2 400/s, 215 at 2 400/s with a total of 8 400/s, and about 280 at 3 000/s. The 3 000/s value is high, because the heavy runs overstate the state.
- **The measurement smears it by only a few cells,** so a fixed level X is an approximation.

**A quick simulation** (`stanowisko/model/symulacja_puli.py`, not SMOLPy; `kandydaci_zajecie{,2,3}.py`, results in `2026-11-25_noc/symulacje/`) with the net drain of §3 and L = 770, D = 72, R = 60. Every variant below is fitted after the measurements, so none of it is a prediction.

| Variant | 1 200/s | 1 800/s | 2 400/s | 3 000/s | 3 600/s | 1 200/s of 8 400 | 2 400/s of 8 400 |
|---|---|---|---|---|---|---|---|
| **Hardware:** light / heavy loss % | 0.010 / 0.75 | 0.16 / 0.77 | 0.48 / 0.71 | 0.64 / 0.67 | 0.63 / 0.64 | 0.044 / 3.95 | 2.34 / 3.87 |
| **Hardware:** heavy run | 6.4 | 6.0 | 5.0 | 4.2 | 3.6 | 6.7 | 5.3 |
| ADR-0016 | 0.54 / 0.62, run 5.5 | 0.59 / 0.61 | 0.60 / 0.61 | 0.61 / 0.60 | 0.63 / 0.59 | 3.07 / 3.31 | 3.27 / 3.27 |
| Net drain + exemption X = 240 | 0.12 / 0.65, run 6.2 | 0.19 / 0.68 | 0.40 / 0.64 | 0.55 / 0.59 | 0.58 / 0.57 | 0.73 / 3.72 | 2.17 / 3.74 |
| Net drain + relative exemption β = 0.30 | 0.12 / 0.65, run 6.2 | 0.21 / 0.67 | 0.42 / 0.63 | 0.56 / 0.58 | 0.58 / 0.57 | 0.74 / 3.72 | 2.31 / 3.69 |
| Net drain + dynamic thresholds α = 16 + X = 210 | 0.014 / 0.65, run 6.3 | 0.20 / 0.66 | 0.40 / 0.62 | 0.47 / 0.60 | 0.51 / 0.59 | 0.10 / 3.82 | 2.41 / 3.64 |

- **The exemption in the state.** In the state, a frame is admitted if its port holds at most X, or β of the occupancy, and the pool has room. On its own, the exemption leaves a floor at low rates: 0.12 % at 1 200/s and 0.73 % at 1 200/s of 8 400/s, whatever X or β. Those are the light port's frames that arrive at a full pool and start the state themselves.
- **Dynamic thresholds remove the floor.** Outside the state, a port's frame fits if `occ_p + u ≤ R + α·(P − pool_use)`. The heavy port meets its threshold before the pool is full, so a light frame still finds room. The same headroom, though, keeps 15–20 % of protection at 3 000–3 600/s, where the hardware has none, for α from 8 to 32.
- **The missing piece is the rule for who starts the state.** A light port at low occupancy must not start it. A port at about the occupancy of the others must.
- **The quick simulation's loss is 0.05–0.1 points below the hardware** in every variant, ADR-0016 included. SMOLPy's ADR-0016 fits the equal-load loss, so this is the quick model's calibration, not the mechanism. *Resolved in §6.1: the missing piece was the traffic, not the rule.*

### 6. The measurements of 2026-11-25 and 2026-11-26 (Part 2)

**Six campaigns, all but the first on the Mac mini receiver** (an interrupt per frame, `stanowisko/pomiary/kampania_mac.sh`). Directories are under `stanowisko/pomiary/wyniki/`.

| Campaign | Runs | What it is |
|---|---|---|
| `2026-11-25_mac_kontrola` | control | the Mac against the laptop; they agree within the spread, and no stamps are paired |
| `2026-11-25_dzien_mac` | 12 cases × 3 × 180 s | light stream at constant rate (CBR), frames of 5 cells, denser sweep of the fade |
| `2026-11-25_10Mb_mac` | 9 cases × 2 × 900 s | the unequal loads at 10 Mb/s |
| `2026-11-25_noc2_mac` | 44 cases × 3 × 240 s | blind check: overload, light CBR in overload, frames of 5/6/8 cells, roles swapped |
| `2026-11-26_obciazenie_mac` | 22 cases × 3 × 60 s | targeted: the same light stream as the total rises from 8 200 to 8 800/s |
| `2026-11-26_ciemno_mac` | 21 cases × 3 × 60 s | blind check of the rule decided below |

The quick model's predictions were saved before each campaign, with SHA-256 sums in `SHA256_przed_pomiarem.txt`. Two disturbed runs were removed and repeated: an rsync to the laptop in `10Mb_mac`, and `apt-daily` on the Mac in `noc2_mac`. The originals are in `zaklocone/`.

#### 6.1 The traffic was incomplete, not the rule

The Modbus responses towards the Mac's port were missing from the quick model: 84 B on the wire, 1 cell, at a constant rate. That is 50/s from the Waveshare, and 100/s per Pi at 100 Mb/s (10/s at 10 Mb/s). With them, and with each generator's actual rate in place of the nominal one, the −0.1 point bias of §5 is gone. The equal-load and one-sender cases then fit at both speeds, with a mean absolute error (MAE) of 0.02–0.04 points. **SMOLPy's scenarios for the testbed must carry the Modbus traffic.**

#### 6.2 The rule is in cells, and the hardware scales

- **At 10 Mb/s, all rates divided by 10, the hardware gives the same loss and runs** as at 100 Mb/s: a heavy run of 6.41–6.48 against 6.40–6.45, and the same fade of the protection. So the parameters are in cells and frames, not in time.
- **Frames of 5 cells** at the same cell rate as frames of 12 are protected alike. So the threshold counts cells, not frames.

#### 6.3 A light port's fate is decided once per state

**The Mac's reconstruction** (`stanowisko/model/kolejka_mac.py`) bins the light port's frames in the state by its occupancy, and by what happened to its previous frame:
- **after an admitted frame,** few are refused: in `noc2` om5_1800, 0–4 % up to 192 cells;
- **after a refused one,** most are: 39–89 % at the same occupancies.

The state does not refuse frame by frame against a level. It refuses a port from some point until the state ends. Passed through the same reconstruction, a simulated per-frame level X shows no such memory. A marking at the entry of the state shows it.

**The mean run of the light stream decides it.**

| Blind, `noc2_mac` (44 cases) | MAE light run [frames] |
|---|---|
| marking at the entry (a12_s204, a12_s216, a16_s210, a8_s210) | 0.107–0.120 |
| per-frame level X (a12_x225, a8_x240, a16_x210, …) | 0.170–0.296 |
| relative exemption β = 0.30 (E) | 0.307 |
| ADR-0016 | 0.128 |

Light frames of 5 cells in overload show it most: om5_1800's run is 3.00 on the hardware, 2.21 with X = 225, and 3.36–3.43 with the marking. A relative threshold for the marking (more than a share of the total) did not help.

#### 6.4 The free room counts the whole occupancy

**The reconstruction needs a clock.** It maps each Pi's send times to the Mac's receive times, and that mapping fails under sustained overload. The clock was aligned on the windows in which the queue empties, and under overload it never does. So `noc2`'s occupancies in overload came out 360–730 cells at the entry, too low.

**From `obciazenie_mac` on, Pi 2 starts 2 s after Pi 1 and stops 2 s before it** (`STAGGER`). Pi 1 is then alone, at an empty queue, at both ends of each run, and its clock is aligned there. Pi 2's clock is aligned to Pi 1's through the FIFO order of the egress. With that the total at the entry of the state is 780–792 cells in every case.

**The quick model said 840.** The table gives the median occupancy at the entry, light / heavy / total in cells.

| Case | Hardware | Reserve set aside per port (ADR-0015) | One pool for the total (decided) |
|---|---|---|---|
| light CBR 1 800/s, total 8 800/s | 168 / 624 / 780 | – | – |
| light CBR 2 400/s, total 8 400/s | 204 / 576 / 780 | 228 / 624 / 840 | 204 / 576 / 780 |
| light CBR 3 300/s, total 8 400/s | 276 / 504 / 780 | 300 / 540 / 840 | 276 / 504 / 780 |

- **Under ADR-0015 each port's reserve lies outside the pool.** With two senders above their reserves, the switch holds P + 2R.
- **The hardware holds about P + R whatever the split.** The pool's use is the total occupancy less one reserve, not the sum of each port's excess.
- **With one sender the two counts are the same,** so L = 770 stands.
- **Set aside, the second reserve lifts the light port by about 20 cells.** That is enough to put it above the marking level too often: the light CBR's loss came out up to 1.4 points too high.
- **With one pool, the simulated occupancies at the entry,** passed through the same reconstruction, **are the hardware's,** and so is the total's 99.9th percentile (804 cells).

#### 6.5 The dynamic threshold: α between 20 and 28

Outside the state a port's frame fits if `occ_p + u ≤ R + α·(P − pool_use)`, with P = 710·(1 + 1/α) so that one sender keeps L = 770 (§4). *Corrected at the build:* with the pool's use taken before the frame, as here, one sender's limit is about a frame above 770 (Decision, Part 2, the identity for one sender).
- **α must be below 30.** From α = 30, a light CBR port of 1 800/s in overload starts the state itself and loses 0.35–1.9 points, against 0.001 % on the hardware.
- **A larger α gives the light port more of the starts:** with light 3 300/s at 8 400/s, the hardware starts 57 % of the states with a light refusal; α = 12 gives 36 %, α = 24 43 %.
- **Fitted on all five campaigns** (101 cases, actual rates, `obciazenie_mac/ocena_zbiorcza_q*.txt`). Every variant has one pool for the total, the net drain and D = 72.

  | α | P | Marking level | MAE light loss [pp] | MAE heavy | MAE light run |
  |---|---|---|---|---|---|
  | 12 | 769 | 204 | 0.151 | 0.108 | 0.117 |
  | 20 | 746 | 204 | 0.108 | 0.086 | 0.102 |
  | **24** | **740** | **204** | **0.095** | **0.084** | 0.106 |
  | 26 | 737 | 204 | 0.125 | 0.098 | 0.136 |
  | 24 | 740 | 192 | 0.179 | 0.107 | 0.104 |
  | 24 | 740 | 216 | 0.212 | 0.127 | 0.106 |
  | 12, reserve set aside (§6.3's best) | 769 | 216 | 0.173 | 0.109 | 0.124 |

  The optimum in α is narrow and not monotone. The threshold steps by α per free cell, so with frames of 12 cells part of the difference between 20 and 26 may be chance.
- **The marking level is 204 cells.** With frames of 12 cells, 204 marks a port holding 216 or more. The 50 % point of the refusals in the reconstruction is 205–215 cells.

#### 6.6 The blind check

**`2026-11-26_ciemno_mac`**, 21 new cases:
- light 2 000–3 600/s in frames of 12 cells;
- 4 000 and 6 000/s in frames of 5 cells; 3 000 and 5 000/s in frames of 6 cells;
- CBR and Poisson, totals of 8 000–8 800/s.

The predictions were saved at 11:17:50, before the first run, with nominal rates and the Modbus traffic.

| Variant | MAE light loss [pp] | bias | MAE heavy | MAE light run | Worst light case |
|---|---|---|---|---|---|
| **α = 24, mark > 204, one pool** | **0.122** | +0.007 | 0.143 | 0.138 | +0.54 (k6c5000_8800) |
| α = 20, mark > 204, one pool | 0.130 | −0.003 | 0.143 | 0.137 | +0.53 |
| α = 12, mark > 216, reserve set aside | 0.288 | +0.093 | 0.195 | 0.166 | +1.53 |
| β = 0.30, no α (E) | 0.261 | +0.021 | 0.116 | 0.337 | −1.29 |

- **Where the predictions differed most, the hardware sided with the decided rule.** The table gives the light loss: hardware, and model − hardware.

  | Case | Hardware | α = 24, mark > 204, one pool | α = 12, mark > 216, reserve set aside |
  |---|---|---|---|
  | light CBR 2 500/s of 8 700 | 2.92 % | +0.20 | +1.01 |
  | light CBR 2 600/s of 8 500 | 2.56 % | +0.07 | +0.52 |
  | light CBR 5 000/s of 6 cells, of 8 800 | 3.33 % | +0.54 | +1.53 |

- **Four protected cases lose 0.000–0.004 % on the hardware:** CBR 2 000/s of 8 400 and of 8 800, 4 000/s of 5 cells, 3 000/s of 6 cells.
  - With α, the model gives 0 in all four.
  - Without α (E), it gives 0.14–1.01 points.
  - **The dynamic threshold is needed.**
- **α = 20 and 24 are not told apart** by this campaign.
- **With actual rates in place of nominal** (`ciemno_mac/postfakt.json`), α = 24 gives 0.145 / 0.094 / 0.150 (light / heavy / run). The reserve set aside gives 0.304 / 0.154 / 0.166.

**What is left:**
- light frames of 5–6 cells in overload lose up to 0.5 points too much;
- a light Poisson stream of 2 800–3 300/s in overload loses up to 0.4 points too little, the heavy one as much too much;
- the light run with frames of 5 cells is too long: 2.99 against 2.38. That holds for every variant with α.

### Options considered

- **A: ADR-0016 as it is.** Refuted for unequal loads (§1).
- **B: the drain counted net (§3), nothing else.** It gives the heavy runs, and the duration of the state within 1 %, but no protection: it is ADR-0016's admission.
- **C: B, and in the state an exemption up to a fixed X per port.** It fits from 1 800/s up, but leaves the floor at low rates.
- **D: B, an exemption X, and dynamic thresholds α.** It fits the protection up to 2 400/s, both overloads, and the heavy runs and the overlaps throughout (light 1 800 / 2 400 / 3 000 / 2 400 of 8 400: 49 / 83 / 93 / 83 % against 34 / 79 / 94 / 75 %). Too much protection at 3 000–3 600/s.
- **E: B and a relative exemption β.** It fits 3 000/s, 3 600/s and 2 400/s of 8 400, but leaves the floor at low rates.
- **F: B and a starting rule by share,** where a refusal starts the state only if the refused port holds more than its share. Simulated as a relative marking level; no better than an absolute one (§6.3).
- **G: B, dynamic thresholds α, one pool for the total, and a marking at the entry of the state** (§6.3–6.5). Confirmed blind (§6.6). **Chosen.**

---

## Decision

### Part 1 (settled): the drain is counted net

One new optional switch parameter, the count of the drain:

```
switch sw ports=8 cell=128 pool=710cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch ingress_drain_count=net
```

- **`freed`** is the default and ADR-0015's and ADR-0016's rule: the state ends at the release at which the switch has freed at least D since it entered.
- **`net`:** the state ends at the release at which the ingress occupancy of the switch, Σₚ occ_p, is at least D below its value at the entry. That is, what has been freed since the entry, less what has been admitted in the state, is at least D.
- **Valid only with `ingress_drop=switch`.** In the `port` scope, a port's net fall would be ADR-0014's own-occupancy rule measured from the entry, and nothing measured supports it (ADR-0015: runs of 6.6 against 3.3).
- **A refusal in the state still does not restart the count** (ADR-0016).
- **Without the parameter nothing changes:** the same results, event counts and benchmark hashes.

**The identity with ADR-0016 for one sender.** Under ADR-0016's condition `L − R ≥ D + u_max − u_min − 1`, nothing is admitted in the state (ADR-0016's proof, step 5). So freed equals net at every release, and `net` gives exactly ADR-0016's results.
- **It does not extend to light ports.** A light port's frames admitted in the state lengthen it under `net`. That is the point of the rule.
- **So single-Pi runs with the Waveshare move slightly.** Its 50 responses/s of 1 cell cost the state about 1 cell per 20 ms.

**The duration with a light port.** With one heavy sender, a light Poisson port of rate λ_l and frames of u_l cells, all admitted in the state, and the egress busy: the state lasts until the egress has taken D cells plus what the light port added. In expectation that is `T = T₀ / (1 − λ_l·u_l·τ)`, where τ is the egress time per cell. That is §3's formula, and a test.

### Metrics, capabilities, IR, ns-3 (for Part 1)

- **Metrics:** unchanged in meaning. `ingress_drop_time_total` simply gets longer.
- **Capability:** `"drain_net"`, without which `ingress_drain_count` is rejected, either value.
- **IR:** one new optional field `ingress_drain_count`, `"freed"` or `"net"`, written only when set. Version 2.
- **ns-3:** the `switch` line gains `<count>`, 0 for `freed` and 1 for `net`. Format `smol-ns3 8`. `SmolSwitch` keeps the occupancy at entry and compares in `Release`.

### Validation and tests (for Part 1)

- **Unit tests:**
    - leaving at the release at which the net fall reaches exactly D;
    - a frame admitted in the reserve in the state delays the end by its units;
    - no restart at a refusal;
    - validation of the combinations above.
- **Identities, on every engine:**
    - without the parameter nothing changes, and `freed` is the same;
    - with one sender under ADR-0016's condition, `net` equals `freed`.
- **The duration test:** one heavy sender and a light Poisson port within its reserve, T from an audit, against `T₀ / (1 − λ_l·u_l·τ)` within a tolerance from the sample size.
- **Bit-exact on SimPy, Rust and ns-3:** a light frame admitted in the state at the release that would have ended it under `freed`.

*As built (2026-11-25), Part 1:*
- **Nothing changes without the parameter.** The output hashes and event counts of B1–B4 are unchanged on SimPy and Rust. The full suite passes: 789 passed, 64 skipped.
- **The audit** (`tests/test_drain_net.py`, 33 tests) checks at every release in the state that the net fall equals the freed less the charged, and that the state ends exactly when the fall reaches D.
- **An admitted frame lengthens the state.** In frames the fall at the end is exactly D. In cells with mixed sizes it ends with D ≤ fall < D + the last frame's units.
- **A refusal in the state** moves neither the entry occupancy nor the freed count.
- **`freed` for contrast:** admissions in the state do not delay its end.
- **The identities hold exactly on every engine:**
    - no parameter equals `freed`;
    - with one sender, `net` equals `freed`: in frames, in cells with frames of 12 and 1 cells, and with a per-port cap.
- **The duration.** Cells of 128 B, pool 480, reserve 360, D = 72; heavy Poisson 8 000/s, light 800/s, both of 1 518 B; 5 seeds × 4 s; 1 954 states.

    | | Mean time in the state |
    |---|---|
    | `net`, model | 732.08 µs |
    | `T₀ / (1 − λ_l·u_l·τ)`, with T₀ = 660.80 µs, λ_l·u_l·τ = 0.0984 | 732.94 µs |
    | `freed`, model | 660.88 µs = T₀ |

    The model and the formula differ by 0.37 standard errors (2.32 µs).
- **Bit for bit on SimPy, Rust and ns-3,** with SimPy's and Rust's event counts equal:
    - `drain-net-tie`: a light frame admitted in the state at the release that would end it under `freed`;
    - `drain-net-two-ports`;
    - `drain-net-flood`: a flooded frame counts once.
- **Two decisions of the build:**
    - **"Admitted in the state" means charged.** A frame whose every egress copy is refused is never held, so it does not count.
    - **An explicit `freed`** is gated by `drain_net` and needs `ingress_drop=switch` too.
- **Where Part 2 will go.** In each engine the in-state admission and the outside-state fit are one function each, called from the ingress decision:
    - SimPy: `_Account.admits_in_state()` and `_Account.fits()`;
    - Rust: `Switch::admits_in_state()` and `Switch::fits_ingress()`;
    - ns-3: `SmolSwitch::AdmitsInState()` and `SmolSwitch::FitsIngress()`.

### Part 2: the protection of a light port

Option G. Three new optional switch parameters, each valid only with `ingress_drop=switch` and `ingress_drain_count=net`:

```
switch sw ports=8 cell=128 pool=740cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch ingress_drain_count=net ingress_pool_count=total ingress_alpha=24 ingress_mark=204cells
```

**`ingress_pool_count`: how the shared pool's use is counted.**
- **`excess`** is the default and ADR-0015's rule: `pool_use = Σₚ max(0, occ_p − R)`. Each port's reserve is set aside.
- **`total`:** `pool_use = max(0, Σₚ occ_p − R)`. The switch holds about P + R whatever the split. A port within its reserve is still admitted (the reserve is a guarantee), but its cells count against the pool.
- **With one sender the two are equal.** For each other port occ_q = 0, so both counts give max(0, occ_p − R).

**`ingress_alpha=<α>`: a dynamic threshold on the fit.**
- A frame of u cells of port p fits, outside the state, if it is within its reserve (`occ_p + u ≤ R`), or if both of these hold:
    - the pool has room: `pool_use + Δ ≤ P`, where Δ is the frame's addition to `pool_use`;
    - `occ_p + u ≤ R + α·(P − pool_use)`.
- **Without the parameter there is no dynamic threshold,** as now.
- **The pool to set with it is P = 710·(1 + 1/α), rounded:** 740 for α = 24. Then one sender's equilibrium occupancy is R + α·P/(1 + α) = 770.4, and its limit with frames of 12 cells is 780 after a frame (below). The validator does not impose this; the testbed scenarios do.

**`ingress_mark=<cells>`: who is refused in the state.**
- **At the entry of the state** the switch marks:
    - the port whose frame was refused;
    - every port whose occupancy is above the level: `occ_q > mark`.
- **In the state:**
    - a frame of a marked port is refused, even within its reserve;
    - a frame of an unmarked port is admitted if it fits (above, with α).
- **If an unmarked port's frame does not fit, that port is marked,** and the frame is refused.
- **The marks are cleared when the state ends.** It ends on the net fall of D (Part 1). A refusal in the state does not restart it.
- **Without the parameter,** a frame in the state is admitted only within its reserve, as in ADR-0016.

**The occupancies are ADR-0015's:** in cells, a frame from its admission until its transmission starts, the frame being sent not counted.

**The identity for one sender holds without α only** (corrected at the build, 2026-11-26).
- Under ADR-0016's condition (`L − R ≥ D + u_max − u_min − 1`), the sender's occupancy stays above R throughout the state.
- So its frames are refused there with or without the mark: it is the refused port. With one sender `total` equals `excess`.
- **Without α, one sender's results are ADR-0016's,** with the same events. The tests pin this for `total`, `excess` and the mark.
- **With α the limit moves by about a frame.** The threshold takes the pool's use before the frame, as the quick model does, on which α was fitted. Beyond its reserve a frame of u cells fits while `occ + u ≤ R + α·(P + R − occ)`. That is while the port holds at most `⌊(α(P + R) + R − u)/(1 + α)⌋` before it: 769 cells for u = 12, α = 24, P = 740 and R = 60, and 770 for u = 1.
- **With frames of 12 cells, a Pi alone holds at most 780 cells after a frame,** against ADR-0016's 768. That agrees with the hardware's reconstruction of one sender, which never exceeds 780 (§4).
- **The draft's L − R = α·P/(1 + α) = 710.4 was the limit with the use taken after the frame.** It would have given ADR-0016's results exactly, but it is not the rule the campaigns fitted. In the quick model, one Pi at 8 090/s loses 0.517 % with the use after the frame, which is ADR-0016's, and 0.506 % with the use before it. A light CBR stream of 2 400/s of 8 400/s loses 0.744 % against 0.947 %.
- **The tests pin the bound:** from the formula, at every decision SimPy makes, and the quick model's loss and runs with the same streams.

**Isolation.** A port of 1-cell frames (the Waveshare, the Modbus responses) sits far below the level, so it is not marked. It fits within its reserve, so it is admitted in the state.

**The values for the LS108GP:** α = 24, P = 740, mark = 204 cells (§6.5, §6.6), with R = 60, D = 72, L = 770. α from 20 to 28 fits within the spread; α ≥ 30 does not.

### Metrics, capabilities, IR, ns-3 (for Part 2)

- **Metrics:** unchanged. A per-port count of marks at entry may be added for the audit; it is not part of the results.
- **Capability:** `"light_port"`, without which any of the three parameters is rejected.
- **IR:** three optional fields, written only when set. Version 2.
    - `ingress_pool_count`: `"excess"` or `"total"`;
    - `ingress_alpha`: a positive number;
    - `ingress_mark`: in cells.
- **ns-3:** the `switch` line gains `<pool_count> <alpha> <mark>`, with `<pool_count>` 0 for `excess` and 1 for `total`, and 0 meaning unset for `<alpha>` and `<mark>`. Format `smol-ns3 9`.
- **Where it goes:**
    - the fit, with α and `pool_use`, in `fits()`, `fits_ingress()` and `FitsIngress()`;
    - the marks in `admits_in_state()`, `admits_in_state()` and `AdmitsInState()`, set at the entry of the state.

### Validation and tests (for Part 2)

- **Unit tests:**
    - `total` against `excess` with two ports above R;
    - the α threshold at its boundary;
    - the marks at entry, at the level and one cell above;
    - a marked port refused within its reserve;
    - an unmarked port marked at its first misfit;
    - marks cleared at the end of the state;
    - the validator's combinations.
- **Identities, on every engine:**
    - without the parameters nothing changes: the same hashes and event counts as Part 1;
    - with one sender and P = 710·(1 + 1/α), the results equal Part 1's. *Corrected at the build:* without α only; with α, the sender's bound above.
- **Against the quick model** (`stanowisko/model/symulacja_puli.py`, `res_shared=True`, `alpha`, `snap`):
    - SMOLPy, with the same streams and the Modbus traffic, reproduces its loss and mean runs within the sampling error;
    - checked on the `ciemno_mac` cases.
- **Bit-exact on SimPy, Rust and ns-3:**
    - a mark at entry of a port above the level;
    - an unmarked light port admitted in the state;
    - an unmarked port marked at a misfit.

### The testbed (after Part 2)

**Calibration:**
- ADR-0015's set: L = 770, R = 60, D = 72. By the identity, they do not move.
- Part 2's three values are fitted on five campaigns, 101 cases: `dzien_mac`, `10Mb_mac`, `noc`, `noc2_mac`, `obciazenie_mac`.
- α = 24, P = 740, mark = 204 cells, `ingress_pool_count=total`.
- The Modbus traffic towards the receiver's port is part of every scenario (§6.1).

**Validation, without refitting:**
- `2026-11-26_ciemno_mac`, predicted before the measurement (§6.6);
- once built, SMOLPy's own run of its 21 cases, against the hardware and against the quick model.

*As built (2026-11-26), Part 2:*
- **The parameters:**
    - `ingress_pool_count=excess|total`, `ingress_alpha=<α>` and `ingress_mark=<limit>` on the switch, in Python and `.smol`;
    - each needs `ingress_drop=switch` and `ingress_drain_count=net`, and `ingress_pool_count` and `ingress_alpha` need `pool`;
    - α is a positive finite number, and the mark is a whole number of the ingress unit, at least 1;
    - three optional IR fields, written only when set; the IR stays version 2;
    - the capability `"light_port"`;
    - `smol-ns3` format 9, with `<pool count> <alpha> <mark>` at the end of the `switch` line. α is written as Python's shortest decimal, so it is read back exactly, and 0 means unset for α and the mark.
- **One decision of the build: the threshold takes the pool's use before the frame,** as the quick model does. So the one-sender identity holds without α only (Decision, Part 2, corrected).
- **Δ under `total`** is the frame's addition to `max(0, Σ occ − R)`. The quick model adds the port's own excess instead. The two differ only for a frame across its reserve's edge with the pool nearly full, and the quick model gives the same results either way on the cases tried.
- **Where it went:**
    - the pool's count in `_Pool` / `Switch::pool_use` / `SmolSwitch::PoolUse`;
    - α in `fits()`, `fits_ingress()` and `FitsIngress()`;
    - the marks in `_admit_in_switch` and `admits_in_state()`, and their counterparts.

    Each entry takes the marks afresh, so they need no clearing at the end; outside the state they are not read.
- **Nothing changes without the parameters.** B1–B4's output hashes and event counts are unchanged on SimPy and Rust. The full suite passes: 950 passed and 10 skipped, 3 of them the quick-model tests, which need `SMOLPY_QUICK_MODEL`. ruff, mypy (73, as before), `cargo fmt`, clippy and the docs build are clean.
- **The audit** (`tests/light_port_audit.py`) checks every SimPy decision against an oracle of the rule, written apart from the engine. That covers the pool's count, the room, the threshold and the marks, kept by the audit and compared with the engine's at each decision in the state. `tests/test_light_port.py` (63 tests) also checks:
    - the boundary of α, at the bound and one cell above, for five (α, P, R, u);
    - the marks at the level and one above;
    - a marked port refused within its reserve, where ADR-0016's rule admits it;
    - an unmarked port admitted while it fits, then marked at its misfit and refused when it would fit again;
    - a fresh mark at the next entry;
    - `total` holding P + R where `excess` holds up to P + 2R;
    - the Waveshare's 1-cell port in overload, never refused.
- **The identities, on every engine:**
    - `excess` with no α and no mark is Part 1's run, events included;
    - one sender under ADR-0016's condition, with `total`, `excess` or a mark and no α, is ADR-0016's run, events included.
- **One sender with α, from the formula:**
    - beyond the reserve, every SimPy decision admits exactly while `occ ≤ ⌊(α(P + R) + R − u)/(1 + α)⌋`;
    - a Pi of frames of 12 cells holds at most 780 after a frame, against 768 under ADR-0016;
    - one Pi at 8 090/s with the Waveshare, 3 seeds × 30 s, loses 0.688 % in runs of 6.33 under Part 2, against 0.702 % and 6.26 under ADR-0016.
- **Bit for bit on SimPy, Rust and ns-3,** with SimPy's and Rust's event counts equal:
    - `light-port-mark`: a port above the level marked at the entry though not refused, and a marked port refused within its reserve;
    - `light-port-admitted`: an unmarked light port admitted beyond its reserve;
    - `light-port-misfit`: an unmarked heavy port marked at its misfit against α;
    - `light-port-excess`: a misfit against the pool's room, counted by the excess, with no α.

    For stochastic traffic SimPy and Rust draw different numbers. They agree in distribution, not bit for bit; ns-3, fed SimPy's send times, agrees bit for bit.
- **Against the quick model with the same streams.** These are the heavy Pi, the light Pi and the three 1-cell response streams, on the 21 `ciemno_mac` cases, with nominal rates, 3 seeds × 56 s each, and SMOLPy on SimPy.
    - Each stream's loss agrees within 2 standard errors in 29 of 38 comparisons (the 4 protected light streams lose nothing on either), and each mean run within 0.1 frame but one, 4.59 against 4.86 at a total of 8 000/s.
    - SMOLPy's heavy stream loses 0.03–0.18 points more in the cases with a light CBR stream, the mean z being +1.3. In a load balance, the quick model admits 1.0001 of the egress's capacity there and SMOLPy 0.999, so part of it is the quick model's own queue, not the rule.
- **Against the hardware, with no refitting.** The testbed's scenario has the Pis as Modbus slaves polled every 10 ms for 10 registers, the Waveshare every 20 ms, and Pi 2's frames of 554, 680 or 1 472 B of payload, at the rates the Pis sent. The model takes 3 seeds × 56 s, the window in which both Pis send. The light Pi is the one with fewer cells per second. `benchmarks/results/2026-11-26-1508-simpy-jaws-zbook-light-port.json`; `python -m benchmarks light-port-summary <file>`.

    | Campaign | Cases | MAE light loss [pp] | bias | MAE heavy loss | MAE light run [frames] | bias | Waveshare polls lost |
    |---|---|---|---|---|---|---|---|
    | `2026-11-26_ciemno_mac`, blind | 21 | **0.120** | +0.051 | **0.086** | **0.119** | +0.077 | 0 |
    | `2026-11-25_noc2_mac`, in the fit | 44 | 0.107 | +0.016 | 0.073 | 0.107 | +0.003 | 0 |
    | *the quick model on `ciemno_mac`, saved before (§6.6)* | 21 | 0.122 | +0.007 | 0.143 | 0.138 | | |
    | *the quick model, at the rates sent (§6.6)* | 21 | 0.145 | | 0.094 | 0.150 | | |

    - **SMOLPy reproduces the blind check as well as the quick model or better,** with the Modbus traffic as the testbed has it: a Pi's responses come from its own port.
    - **The protected cases:** CBR 2 000/s of 8 400 and of 8 800, 4 000/s of 5 cells and 3 000/s of 6 cells lose 0.000–0.013 % in the model, against 0.000–0.004 % on the hardware.
    - **The cases the rules told apart (§6.6):**
        - light CBR 2 500/s of 8 700: 3.22 % against 2.92 %;
        - CBR 2 600/s of 8 500: 2.67 % against 2.56 %;
        - CBR 5 000/s of 6 cells of 8 800: 4.03 % against 3.33 %. That is the largest error, as in the quick model (+0.54 there).
    - **§6.6's residuals stay:**
        - light frames of 5–6 cells in overload lose up to 0.7 points too much, and their runs are too long: 3.16 against 2.38 at 4 000/s, 4.39 against 3.99 at 6 000/s;
        - a light Poisson stream of 2 800–3 000/s in overload loses 0.1–0.2 points too little.

---

## Remaining (2026-11-26)

- ~~**Build Part 2** in SimPy, Rust and ns-3, with the tests above.~~ Done (As built, Part 2).
- ~~**Run SMOLPy on the `ciemno_mac` and `noc2_mac` cases** with the Modbus traffic, and compare it with the quick model's predictions saved before each campaign.~~ Done: a light-loss MAE of 0.120 points blind, 0.107 in the fit.
- **The residuals of §6.6** (light frames of 5–6 cells in overload; a light Poisson stream of 2 800–3 300/s in overload) are left as the model's stated error. They are not a reason for a further parameter.
- **The heavy stream's 0.1 point above the quick model** in the light CBR cases: the quick model's admitted load is above the egress's capacity there. Not pursued.
- **The `smolpy-engine-rs` floor** is raised to the release that carries this ADR, once it is published, never to a pre-release.
