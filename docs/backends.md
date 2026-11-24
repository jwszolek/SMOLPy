# Execution backends

A SMOL scenario does not depend on the engine that runs it. SMOLPy has three engines, called **backends**, and the same `.smol` file or `Network` runs on each one ([ADR-0004](adr/0004-execution-backends.md)).

| Backend | What it is | Use it for |
|---|---|---|
| `simpy` (default) | the reference engine, in Python, on SimPy | the live and text dashboards, and the reference results |
| `rust` | the same model in Rust ([ADR-0008](adr/0008-rust-des-engine-mvp.md)) | large or many runs: about 40× faster, with the same results bit for bit |
| `ns3` | the model on ns-3 3.49, with ns-3's IPv4, UDP and TCP stack | Modbus TCP and MQTT over a real TCP, and comparisons with ns-3 |

```python
result = net.simulate(duration=60_000, backend="ns3")
batch = run_many(net, runs=30, duration=60_000, backend="rust")
```

```bash
smolpy run plant.smol --backend ns3 -o results.csv
```

## What each backend runs

All three run the whole model: every node and traffic kind, every traffic pattern, IMIX, 802.1Q priorities and every metric. The differences:

| | `simpy` | `rust` | `ns3` |
|---|---|---|---|
| Live and text dashboards | ✓ | – (headless) | – (headless) |
| Install | included | `pip install smolpy[rust]` | the image `ghcr.io/jwszolek/smolpy:latest-ns3`, or an ns-3 build |
| Ethernet and PROFINET RT | reference | bit-exact | bit-exact |
| Modbus TCP and MQTT | sized as TCP, without TCP's behaviour | bit-exact | over ns-3's TCP |
| Bounded switch buffers ([ADR-0011](adr/0011-bounded-switch-buffers.md)) | reference: a lost message is lost | bit-exact | Ethernet and PROFINET bit-exact; TCP retransmits a lost segment |
| Polls with one request in flight ([ADR-0012](adr/0012-modbus-one-request-in-flight.md)) | reference: a lost poll times out | bit-exact | over TCP, with Linux's RTO: a lost poll is answered late |
| Ingress limits, limits in frames, flows from any station ([ADR-0013](adr/0013-ingress-buffer-accounting.md)) | reference | bit-exact | Ethernet and PROFINET bit-exact; TCP retransmits a lost segment |

A scenario outside a backend's capabilities is rejected before it runs, with the reason.

## Installing ns-3

ns-3 cannot be installed with `pip`. The simplest way is the image, for `linux/amd64` and `linux/arm64`:

```bash
docker run --rm -v "$PWD:/work" ghcr.io/jwszolek/smolpy:latest-ns3 run plant.smol --backend ns3
```

It holds ns-3.49 built for SMOLPy, SMOLPy itself, the Rust engine, and `smol-ns3`, the program that runs scenarios on ns-3. It is about 1.3 GB, or 300 MB to download. Because it contains ns-3, it is distributed under GPLv2.

Without the image, the backend needs:
- `NS3_HOME`, naming a built ns-3 3.49 source tree, with its libraries in `build/lib` and headers in `build/include`;
- a C++23 compiler, `$CXX` or `g++`.

The first run compiles `smol-ns3` against that build, which takes about 8 s, and keeps it in `~/.cache/smolpy/ns3` (or under `$XDG_CACHE_HOME`). Later runs only execute it.

## How the results compare

The cross-backend suite ([ADR-0009](adr/0009-batch-execution-and-cross-backend-validation.md)) compares every backend with SimPy.

**Rust** gives SimPy's results bit for bit on every deterministic scenario: every sample, down to the event count. Random traffic uses its own generator, so there the results agree statistically.

**ns-3** runs SMOL's own full-duplex Ethernet device and switch, because ns-3's CSMA is half duplex. On top of them it runs ns-3's IP stack, and its random traffic is drawn from SimPy's streams. So:
- **Ethernet** over UDP, with every pattern, IMIX and priorities, gives SimPy's results bit for bit. That holds for stochastic scenarios too, with the same seed.
- **PROFINET RT** runs on layer 2 and also gives SimPy's results bit for bit, watchdog expiries included.
- **Modbus TCP and MQTT** run over ns-3's TCP, which is what that backend is for. Their frames have SMOL's sizes. What differs is TCP's behaviour, below.

### Modbus and MQTT over TCP

Each Modbus master and slave, and each MQTT client and broker, share one persistent TCP connection, opened at the start. ns-3's TCP defaults apply, CUBIC, delayed acknowledgements and no Nagle, except where Linux's differ in how TCP recovers from a loss ([ADR-0012](adr/0012-modbus-one-request-in-flight.md)):

| Setting | ns-3's default | Used, as Linux | `TcpSettings` field |
|---|---|---|---|
| Minimum retransmission timeout | 1 s | 200 ms | `min_rto_ms` |
| Clock granularity of the RTO | 1 ms | 200 ms | `clock_granularity_ms` |
| Delayed-ACK timeout | 200 ms | 40 ms | `delayed_ack_ms` |
| SYN retransmission timeout | 3 s | 1 s | `syn_rto_ms` |

- **The RTO.** ns-3 computes it as `max(SRTT + max(G, 4·RTTVAR), MinRto)`. With a granularity `G` of 200 ms that is Linux's `SRTT + 4·RTTVAR`, with the variance term at least 200 ms. So a lost segment on a fast network is retransmitted after about 200 ms plus the RTT, as on Linux.
- **Other values.** `smolpy.ns3.TcpSettings` holds them, and `Ns3Backend` takes them. They are a backend option, not part of the scenario:

    ```python
    from smolpy.backends import Ns3Backend
    from smolpy.ns3 import TcpSettings

    result = Ns3Backend(tcp=TcpSettings(min_rto_ms=1_000, clock_granularity_ms=1)).run(scenario)
    ```

    With `min_rto_ms=1000`, `clock_granularity_ms=1`, `delayed_ack_ms=200` and `syn_rto_ms=3000`, ns-3's own defaults apply.

Timestamps are off, so that segments have the 20 B header SMOL sizes them with.

**At low load** the results are SimPy's, plus what TCP adds:
- **1 ns per send.** A Modbus round trip, or a PUBLISH from publisher to subscriber, takes SimPy's time plus 2 ns.
- **The handshake.** Each client receives a SYN-ACK of 66 B at the start.
- **The first data segment** of each connection is acknowledged at once. Its reply waits behind that frame, 6.72 µs at 100 Mb/s.
- **One-way traffic is acknowledged.** A QoS 0 PUBLISH, or a broker's copy to a subscriber, is acknowledged every second segment with a 64 B frame. Request and response traffic (Modbus, QoS 1 with its PUBACK) carries the acknowledgements in its own frames.

**Under load**, `python -m benchmarks load` runs scenario L1, a plant cell. A SCADA master polls four Modbus slaves, and two sensors publish MQTT to a broker that forwards to an HMI. They share a 10 Mb/s uplink with a camera's UDP stream, whose rate sets the uplink's offered load ρ. The protocols alone offer ρ = 0.126. Means over 20 simulated seconds, SimPy → ns-3:

| ρ | Modbus RTT | MQTT latency | Delivered to the HMI |
|---|---|---|---|
| 0.50 | 0.886 → 0.886 ms | 0.477 → 0.479 ms | 1.4246 → 1.4247 MB |
| 0.95 | 1.283 → 1.283 ms | 0.806 → 0.807 ms | 1.4246 → 1.4247 MB |
| 1.00 | 1.377 → 1.627 ms (+18 %) | 1.023 → 1.274 ms (+25 %) | 1.4246 → 1.4247 MB |
| 1.10 | 864 → 847 ms (−2 %) | 864 → 851 ms (−2 %) | 1.2953 → 1.2704 MB (−2 %) |
| 1.20 | 1584 → 1557 ms (−2 %) | 1583 → 1585 ms | 1.1872 → 1.1269 MB (−5 %) |
| 1.35 | 2463 → 2484 ms (+1 %) | 2463 → 3053 ms (+24 %) | 1.0553 → 0.7801 MB (−26 %) |
| 1.50 | 3166 → 3356 ms (+6 %) | 3167 → 4553 ms (+44 %) | 0.9499 → 0.5714 MB (−40 %) |

- **Below saturation**, ns-3 agrees with SimPy within 0.4 %.
- **At ρ = 1.00** the link is exactly full on SimPy. ns-3's acknowledgements add about 0.7 % of the uplink: the sensor with QoS 1 acknowledges every second PUBACK. That overloads the link, so its queue grows slowly.
- **Overloaded**, both engines queue without bound, and the latencies agree within a few percent while the queueing delay stays under about a second.
- **Heavily overloaded**, TCP's windows decide. A connection can have at most its congestion window (or the receiver's window) in flight, so its rate is at most window / RTT. As the RTT reaches seconds, that falls below the sensors' publishing rate. Their messages wait in their TCP send buffers, and the camera's UDP stream, which does not back off, takes more of the link. On SimPy every source keeps sending, and the switch's queue shares the link in proportion to what each one offers.
- **No losses.** The switches of L1 have no `buffer`, so their queues are unbounded: nothing is dropped and TCP retransmits nothing. With bounded buffers, below, TCP reacts to losses as well.

The results are in `benchmarks/results/*-tcp-under-load.json`, recorded with ns-3's own TCP settings. `python -m benchmarks load-summary <file>` prints their table. The table gives ns-3's means with Linux's settings: they are the same up to ρ = 1.1, and differ by at most 1.2 % above, where the queueing delay reaches seconds.

### Bounded switch buffers

A switch with `buffer` drops a frame that does not fit in its port's egress buffer ([ADR-0011](adr/0011-bounded-switch-buffers.md)). The three engines apply the same rule, to the same frame:
- **Rust** gives SimPy's results bit for bit, drops and event counts included.
- **ns-3** does too for Ethernet over UDP and PROFINET RT, random traffic included with the same seed.
- **Modbus and MQTT** are where they differ, by design. SimPy and Rust have no transport layer, so a lost request or response leaves its poll unanswered (`modbus_unanswered`). On ns-3 the frame is a TCP segment, and TCP retransmits it, so the poll is answered late.

`python -m benchmarks loss` runs scenario T1, after a hardware testbed:
- an unmanaged 1 Gb/s switch;
- Poisson UDP of 1 518 B frames from a Raspberry Pi to a laptop on a 100 Mb/s port, at load ρ of that port;
- the laptop polls a Modbus server on the Pi every 10 ms.

Each load runs 30 simulated seconds with 5 seeds. The tables give means over the seeds, with the range in brackets:
- **RTT:** of the Modbus polls, in ms, over 1 ms windows.
- **Unanswered:** polls without a response at the end of the run.
- **Dropped:** the frames the switch dropped, as a share of the UDP frames sent.

**115 000 B per port**, what the testbed's capped RTT points to:

| ρ | RTT median, SimPy and ns-3 | RTT median, Rust | RTT max, all | Unanswered | Dropped, SimPy and ns-3 |
|---|---|---|---|---|---|
| 0.93 | 0.58 [0.55, 0.62] | 0.57 [0.54, 0.60] | 8.66 | 0 | 0 |
| 0.98 | 2.13 [1.99, 2.44] | 1.90 [1.73, 2.10] | 9.34 | 0 | 0.095 % |
| 0.996 | 4.33 [3.93, 4.85] | 3.86 [3.39, 4.36] | 9.36 | 0 | 0.51 % [0.39, 0.63] |
| 1.02 | 7.45 [7.22, 7.60] | 7.29 [7.04, 7.54] | 9.36 | ≤ 1 in flight | 2.18 % |

- **The delay is capped.** It is at most 9.36 ms on every engine, where unbounded queues give a p99 of tens of ms at ρ = 0.996. The testbed measured about 9.8 ms.
- **Only UDP is lost.** A full queue holds 75 frames of 1 518 B (113 850 B), and the 1 150 B left over always fit a Modbus response (86 B). So no poll goes unanswered, and ns-3's TCP has nothing to retransmit: ns-3 gives SimPy's results, apart from the third decimal at ρ ≥ 1.02.
- **Rust and SimPy differ only by their random draws.** Rust draws its own streams. With these five seeds its medians are lower near ρ = 1, and with seeds 6–15 they are not: 4.20 ± 0.08 ms against 4.07 ± 0.18 ms at ρ = 0.996.
- **Loss is higher than measured.** At ρ = 0.996 the model drops 0.51 % of the UDP frames, where the testbed lost 0.09 %. That points to a different load, or a larger or shared buffer, on the testbed. It is not fitted here.

**113 900 B per port**: 75 frames and 50 B, so a full queue drops responses too:

| ρ | Unanswered, SimPy | Unanswered, Rust | Unanswered, ns-3 | RTT p99, SimPy | RTT p99, ns-3 | RTT max, SimPy | RTT max, ns-3 |
|---|---|---|---|---|---|---|---|
| 0.98 | 1.6 | 2.8 | 0 | 8.64 | 8.65 | 9.24 | 32.4 |
| 0.996 | 14.0 | 10.4 | 0 | 9.10 | 9.15 | 9.24 | 38.1 |
| 1.02 | 58.0 | 48.2 | 0 | 9.21 | 31.5 | 9.24 | 42.6 |
| 1.05 | 117.6 | 119.8 | 0 | 9.23 | 32.7 | 9.24 | 48.0 |

- **SimPy and Rust lose polls.** A poll whose request or response is dropped is never answered, and the delay of the others stays capped by the buffer.
- **ns-3 loses none.** TCP retransmits the lost response. A poll every 10 ms keeps segments flowing, so the next responses arrive out of order, and three duplicate acknowledgements trigger a fast retransmit, without waiting for the retransmission timeout. The poll is answered 3 to 4 poll periods late, at 30 to 48 ms.
- **What to use.** For Modbus and MQTT under loss, SimPy and Rust count what a stack without retransmission would lose. They give a lower bound on delivery and say nothing about the delay of retransmission. ns-3 shows what a TCP stack does.

The results are in `benchmarks/results/*-bounded-buffers.json`. `python -m benchmarks loss-summary <file>` prints their tables.

### One request in flight

A poll with `outstanding=1` waits for each response before it sends the next request ([Modbus TCP](modbus.md#one-request-in-flight), [ADR-0012](adr/0012-modbus-one-request-in-flight.md)). The engines apply the same rules: skipped instants, transaction ids, the timeout and late responses.
- **Rust** gives SimPy's results bit for bit, with and without loss.
- **ns-3** gives SimPy's counts when nothing is lost. TCP adds 1 ns per send, so a response is 2 ns later than on SimPy.
- **With loss,** SimPy and Rust lose the request or the response, and the poll times out. On ns-3 TCP retransmits it.

**Retransmission is slow here.** With one request in flight, nothing is sent while the master waits, so TCP has no later segments that could trigger a fast retransmit. It waits for the retransmission timeout, at least 200 ms with Linux's settings.

`python -m benchmarks closed-loop` runs scenario T2: T1 above, with `buffer=91080` (60 frames of 1 518 B), so that a full queue drops responses, and the poll with `outstanding=1`, every 10 ms. Each load runs 30 simulated seconds with 5 seeds. With a timeout of 1 s, sums over the seeds:
- **Affected:** the polls answered after more than 100 ms, timed out or unanswered, as a share of the requests sent.
- **Long RTTs:** the round trips above 100 ms: their number, and their lowest, median and highest value, in ms.
- **Dropped:** the frames the switch dropped, as a share of the UDP frames sent.

| ρ | Affected, SimPy | Affected, Rust | Affected, ns-3 | Long RTTs, ns-3 | Dropped, ns-3 |
|---|---|---|---|---|---|
| 0.985 | 0.28 % (33 timeouts) | 0.21 % (26) | 0.28 % | 40: 210.0, 212.9, 216.5 | 0.31 % |
| 0.99 | 0.31 % (35) | 0.33 % (36) | 0.34 % | 47: 210.2, 213.3, 217.2 | 0.46 % |
| 0.996 | 0.56 % (54) | 0.45 % (44) | 0.61 % | 81: 210.1, 213.2, 217.3 | 0.69 % |

- **On ns-3 a lost response comes back after the RTO,** 210 to 217 ms after its request. The hardware testbed of ADR-0011 measured about 215 ms. No poll times out, and none is left unanswered at the end beyond the one in flight. With ns-3's own settings the same responses come back after about 1 s.
- **On SimPy and Rust a lost poll is a timeout,** followed by the 100 instants skipped while waiting for it. Without a timeout, polling stops at the first loss.
- **More polls are affected than on the testbed,** which measured 0.18 % (3 of 1 680). The share follows the frames dropped, and the model drops 3 to 8 times more than the testbed's 0.09 % (ADR-0011). That gap is left open.

The results are in `benchmarks/results/*-one-request-in-flight.json`. `python -m benchmarks closed-loop-summary <file>` prints their tables, without a timeout too.

### Ingress buffers

A switch can also bound what each of its ingress ports holds, and count its limits in frames rather than bytes ([ADR-0013](adr/0013-ingress-buffer-accounting.md)). The engines apply the same admission and release rules:
- **Rust** gives SimPy's results bit for bit, drops and event counts included.
- **ns-3** does too for Ethernet over UDP and PROFINET RT, random traffic included with the same seed.
- **Modbus and MQTT** differ by TCP, as with egress limits: ns-3 retransmits what SimPy and Rust lose.

`python -m benchmarks ingress` runs scenario T3, the testbed as it was measured more widely:
- **The Pi** is one `modbus_slave` on a 1 Gb/s port. It sends Poisson UDP of 1 518 B frames to the laptop, at load ρ of the laptop's 100 Mb/s port, and serves the laptop's polls every 10 ms.
- **A Waveshare relay module** is a second `modbus_slave` on another 1 Gb/s port, polled every 20 ms. Its responses, like the Pi's, are under 100 B.
- **Both polls** have one request in flight and a timeout of 1 s.
- **The switch** holds `ingress_buffer=60frames`. Beside it, the model of ADR-0011 and ADR-0012 holds `buffer=91080`: one egress buffer of 60 full frames.

Each load runs 30 simulated seconds with 5 seeds. The table gives sums over the seeds:
- **Polls affected:** retransmitted on ns-3 (a round trip above 100 ms), timed out on SimPy and Rust, or unanswered at the end, as a share of the polls sent.
- **Pi's frames dropped:** `drops_total` / `frames_sent:pi`. Its responses are 1.2 % of its frames, so this is its UDP loss, to within about 1 % of its value.

| Switch | ρ | Engine | Pi: polls affected | Waveshare: polls affected | Pi's frames dropped |
|---|---|---|---|---|---|
| `ingress_buffer=60frames` | 0.96 | SimPy / Rust / ns-3 | 0.02 / 0.05 / 0.02 % | 0 / 0 / 0 | 0.027 / 0.035 / 0.027 % |
| | 0.984 | SimPy / Rust / ns-3 | 0.25 / 0.18 / 0.28 % | 0 / 0 / 0 | 0.28 / 0.26 / 0.29 % |
| | 0.997 | SimPy / Rust / ns-3 | 0.59 / 0.69 / 0.62 % | 0 / 0 / 0 | 0.73 / 0.65 / 0.74 % |
| `buffer=91080` | 0.96 | SimPy / Rust / ns-3 | 0.02 / 0.02 / 0.02 % | 0.01 / 0.04 / 0.01 % | 0.028 / 0.035 / 0.028 % |
| | 0.984 | SimPy / Rust / ns-3 | 0.22 / 0.17 / 0.25 % | 0.24 / 0.19 / 0.26 % | 0.28 / 0.26 / 0.29 % |
| | 0.997 | SimPy / Rust / ns-3 | 0.51 / 0.64 / 0.52 % | 0.54 / 0.54 / 0.58 % | 0.73 / 0.65 / 0.75 % |
| **the testbed** | 0.997 | | **0.44 %** (133 of 30 000) | **0** (of 15 000) | **0.66 %** (UDP) |

- **The Waveshare is isolated only with ingress accounts.** Its port never fills, so none of its polls is lost, on every engine at every load, as on the hardware. With one egress buffer it loses about as many polls as the Pi, 0.54 to 0.58 % at ρ = 0.997, where the hardware lost none.
- **The Pi's responses are lost as its UDP frames are,** because a limit in frames drops a small frame as readily as a large one. A limit in bytes would almost always keep room for them.
- **On ns-3 every lost response comes back after the RTO,** 210 to 217 ms after its request. One response was lost twice and came back after 636 ms: the RTO doubles on each retransmission. No poll times out.
- **Against the hardware at ρ = 0.997:** ns-3 drops 0.74 % of the Pi's frames, against 0.66 % of its UDP frames on the testbed, and affects 0.62 % of its polls, against 0.44 %. The model loses the Pi's responses at 84 % of the rate of its frames, and the testbed at 67 % of its UDP rate. That gap is left open.
- **The delay is still capped at about 7.4 ms:** 60 full frames ahead of a response, as with the egress buffer. With one source, the Pi's ingress account holds what the laptop's egress queue would.

The results are in `benchmarks/results/*-ingress-buffers.json`. `python -m benchmarks ingress-summary <file>` prints their table, with the round trip's median and p99 and the spread of the long round trips.

### Cells and a resume threshold

A switch limit can be in cells of `cell` bytes, and an account can have a resume level: once a frame does not fit, the account drops every frame until it has drained to that level ([ADR-0014](adr/0014-buffer-cells-and-hysteresis.md)). The engines apply the same rules:
- **Rust** gives SimPy's results bit for bit, drop episodes and event counts included.
- **ns-3** does too for Ethernet over UDP and PROFINET RT, random traffic included with the same seed.
- **Modbus and MQTT** differ by TCP, as before.

`python -m benchmarks cells` runs scenario T4: T3 above at the testbed's 8 100 frames/s, with three models of the switch. Each case runs 30 simulated seconds with 5 seeds:
- **`cells`:** `cell=128 ingress_buffer=740cells ingress_resume=668cells`, ADR-0014's calibration;
- **`cells-tail`:** the same 740 cells, with tail drop;
- **`frames`:** ADR-0013's `ingress_buffer=60frames`.

The columns:
- **UDP loss:** the Pi's frames dropped, as a share of those it sent.
- **Mean run:** `ingress_drops_total / ingress_drop_episodes_total`.
- **Single-frame runs:** from an audit of SimPy's decisions, since no metric gives each run's length.
- **Pi's polls:** lost on SimPy and Rust, or retransmitted on ns-3.

| Model | Engine | UDP loss | Mean run | Single-frame runs | Pi's polls | Waveshare's polls |
|---|---|---|---|---|---|---|
| `cells` | SimPy | 0.73 % | 6.28 | 0.5 % of 1 424 | 0.49 % | 0 |
| | Rust | 0.64 % | 6.25 | | 0.40 % | 0 |
| | ns-3 | 0.74 % | 6.23 | | 0.55 % | 0 |
| `cells-tail` | SimPy / Rust / ns-3 | 0.70 / 0.62 / 0.70 % | 1.35 / 1.33 / 1.35 | 72 % | 0 | 0 |
| `frames` | SimPy / Rust / ns-3 | 0.71 / 0.63 / 0.73 % | 1.35 / 1.34 / 1.34 | 72 % | 0.81 / 0.56 / 0.72 % | 0 |
| **the testbed** | | **0.55–0.66 %** | **6.4–6.7** | **none** | **0.44 %** | **0** |

- **Only cells with a resume level give the runs:** about 6 frames, almost never one, as on the hardware. Tail drop, in cells or frames, gives runs of 1.35 with 72 % single frames, as M/D/1/K predicts.
- **The Pi's small responses are lost in the runs:** 0.40 to 0.55 % of its polls, against 0.44 %. With tail drop in cells they always fit and are never lost, and with frames they are lost at the UDP rate.
- **The UDP loss is 0.64 to 0.74 %,** a little above the hardware's 0.55 to 0.66 %.

**The batch case.** Poisson groups of 8 back-to-back 1 518 B frames at 7 500 frames/s, with the same polls. The ratio is the Pi's polls affected over its UDP loss:

| Model | SimPy | Rust | ns-3 | The testbed |
|---|---|---|---|---|
| `cells` | 0.65 | 0.56 | 0.63 | **0.55** |
| `cells-tail` | 0 | 0 | 0 | |
| `frames` | 0.37 | 0.29 | 0.32 | |

**The burst test.** The Pi sent N frames back to back, at its measured send rate or the 1 Gb/s line rate if that was lower. The model sends the same and counts the frames received. Measured against the 57 bursts of the testbed's CSVs:
- **Bursts that do not fill the port** give exactly the measured count in cells, with or without a resume level: every burst of 40 and 60 full frames, of 100 and 200 frames of 512 B, and of 64 B frames up to 2 000. `frames` already drops some of the 512 B and 64 B ones.
- **`frames` holds 60 frames of any size,** so it receives 105 to 220 of the 512 B bursts where the hardware received 240 to 355, and about 650 too few of the 64 B ones.
- **`cells` holds what the hardware held, a little less:** received less what drained, 56–60 full frames against 60–65, 170–185 of 512 B against 181–196, and 672–732 of 64 B against 716–776. The resume level costs up to 72 cells at the end of a burst that ends in a drop state.

**Calibration.** 740 and 668 are first estimates, and 668 came from a fit to the run lengths. A limit of 770 cells resuming at 698, the same gap, fits the burst tests better: the mean error of the received count goes from −4, −9 and −21 frames at 1 518, 512 and 64 B to −0.5, −1.7 and −3.4. On SimPy it lowers the UDP loss to 0.69 %, with the same runs (6.28) and Pi's polls (0.53 %). It is reported here, not put into the scenario.

The results are in `benchmarks/results/2026-11-24-1006-simpy-rust-ns3-jaws-zbook-buffer-cells.json`. `python -m benchmarks cells-summary <file>` prints their tables, every burst included.

### A shared pool: the LS108GP, calibrated and validated

A switch can share one pool among its ingress ports, beyond a reserve each, and end a port's drop state once it has freed an amount of its buffer ([ADR-0015](adr/0015-shared-pool-and-drain-resume.md)). The engines apply the same rules:
- **Rust** gives SimPy's results bit for bit, event counts included.
- **ns-3** does too for Ethernet and PROFINET, random traffic included with the same seed.

`python -m benchmarks pool` fits the LS108GP on one set of measurements, then predicts another without refitting. It computes the hardware's numbers itself, from the raw `.seq` and `.csv` files of the testbed.

**Calibration.** Cells are 128 B. With one source, a pool P with a reserve R is an ingress limit of L = P + R, so the fit has two stages:
1. **L and the drain D,** on the single-source measurements:
    - the 57 burst tests;
    - the loss runs at 8 100 frames/s: 0.55–0.59 % lost, runs of 6.42–6.68, none of a single frame.

    The grid was L from 730 to 800 and D from 36 to 96. Best: L = 770 and D = 72, with a loss of 0.59 % and runs of 6.40.
2. **R,** on the reserve test: Pi 1 holds the port full at a constant 9 000 frames/s, and Pi 2 sends bursts. Each modelled repetition starts at another phase of the background's 7.7 ms cycle of refill and drain.
    - **The pace of Pi 2's 64 B bursts was not recorded,** and the burst tests saw 63 000 to 340 000 frames/s. It is fitted with R, from 110 000 to 230 000 frames/s.
    - **R from 42 to 60 cells fits about equally well.** The best was R = 54 at 110 000 frames/s; R = 48 at 130 000 is nearly as good. So R is not sharply determined.

| Pi 2's burst | 10 × 1 518 B | 20 × 1 518 B | 40 × 1 518 B | 50 × 64 B | 100 × 64 B | 200 × 64 B | 400 × 64 B |
|---|---|---|---|---|---|---|---|
| Measured: arrived | 5–9 (6.5) | 4–9 (6.5) | 7–12 (9) | 50 | 71–100 (100) | 112–168 (144) | 203–227 (207) |
| Model, R = 54 | 4–10 (6.5) | 4–10 (6.5) | 4–10 (6.5) | 50 | 56–100 (96) | 68–176 (126) | 181–257 (202) |

**The fitted LS108GP:** `cell=128 pool=716cells ingress_reserve=54cells ingress_drain=72cells`. [ADR-0016](adr/0016-switch-wide-drop-state.md) refits it with one drop state for the switch, [below](#a-drop-state-for-the-whole-switch-the-ls108gp-refitted-and-validated).

**Validation,** with those values. Three seeds per case, 60 s each, or 240 s at 10 Mb/s. Runs and single-frame shares are SimPy's, from an audit of its decisions:

| Case | | Loss | Mean run | Single-frame runs | Pi's polls | RTT p99 / max (ms) | Waveshare |
|---|---|---|---|---|---|---|---|
| Two Pis, 2 × 4 050/s | hardware | 0.664 / 0.691 % | 3.24 / 3.35 | 13 % | 0.53 % | 8.48 / 9.26 | 0 |
| | SimPy / Rust / ns-3 | 0.62 / 0.61 / 0.64 % | 3.76 / 3.74 / 3.80 | 6.8 % | 0.44 / 0.35 / 0.45 % | 8.2 / 8.5 | 0 |
| Two Pis, 2 × 3 900/s | hardware | 0.036 / 0.039 % | 3.14 / 3.29 | 9–16 % | 0.04–0.06 % | 7.1 / 9.0 | 0 |
| | SimPy / Rust / ns-3 | 0.024 / 0.021 / 0.024 % | 3.8 / 3.2 / 3.8 | 10 % | 0.01–0.02 % | 6.9 / 8.5 | 0 |
| 10 Mb/s, 808/s | hardware | 0.60 % | 6.20 | 0.9 % | 0.49 % | 79.2 / 82.3 | |
| | SimPy / Rust / ns-3 | 0.68 / 0.64 / 0.69 % | 6.38 / 6.35 / 6.39 | 0.3 % | 0.48–0.54 % | 76.2 / 80.0 | |
| 10 Mb/s, 846/s | hardware | 4.27 % | 6.54 | 0.3 % | 4.03 % | 81.5 / 83.1 | |
| | SimPy / Rust / ns-3 | 4.22 / 4.09 / 4.23 % | 6.60 / 6.61 / 6.58 | 0.4 % | 3.4–3.7 % | 78.6 / 80.0 | |
| 512 B, 23 383/s | hardware | 0.117 % | 18.5 | 0 % | 0.08 % | 8.27 / 8.91 | 0 |
| | SimPy / Rust / ns-3 | 0.098 / 0.116 / 0.100 % | 18.2 | 0 % | 0.09–0.11 % | 7.6 / 8.2 | 0 |
| Mixed 1 518 + 512 B | hardware | 0.567 / 0.487 % (ratio 0.86) | 7.9, merged | | 0.36 % | 8.21 / 8.68 | 0 |
| | SimPy / Rust / ns-3 | 0.60 / 0.54 % (ratio 0.89) | 8.2 | 0.1 % | 0.39–0.47 % | 7.6 / 8.0 | 0 |
| ADR-0013's test, 8 100/s | hardware | 0.66 % | | | 0.49 % of polls sent | 8.21 / 8.69 | 0 |
| | SimPy / Rust / ns-3 | 0.71 / 0.63 / 0.72 % | 6.3–6.5 | 0.4 % | 0.62 / 0.44 / 0.56 % | 7.6 / 8.0 | 0 |
| Batch K = 8, 7 500/s | hardware | | | | ratio 0.55 | | |
| | SimPy / Rust / ns-3 | 3.07 % | 8.5–8.6 | 12.5 % | ratio 0.60 / 0.59 / 0.63 | | 0 |

- **Two senders are predicted, as ADR-0014 could not:**
    - the loss at 2 × 4 050/s is 0.61–0.64 %, against 0.66–0.69 %, where ADR-0014 predicted 0.38 %;
    - the runs are about half as long as with one sender: 3.7–3.8 in the model, 3.2–3.4 on the hardware;
    - the Waveshare loses nothing.
- **The single-source cases it was not fitted on** come out as measured:
    - 10 Mb/s;
    - 512 B;
    - the mixed sizes' ratio, 0.89 against 0.86;
    - the batch ratio, 0.59–0.63 against 0.55.
- **The Pi's responses.** In ADR-0013's test the hardware retransmitted 133 of the 27 160 polls it sent, 0.49 %, not 0.44 %: it sent only 27 160 of its 30 000 poll instants. Counted the same way, the model's 0.44–0.62 % is within its engines' spread, so the residual gap of ADR-0014 is mostly this difference of denominators. The pool and the reserve do not change it.
- **What the model does not get:**
    - **The two Pis' runs are a little long, 3.8 against 3.3, and rarely single, 7 % against 13 %.** In SimPy 1 092 of 2 461 drop states of one Pi overlap one of the other's. The hardware's "0 of 2 072" that ADR-0015 set against it was an analysis error: the episodes were merged by two unaligned sender clocks. Measured on one clock, 96–97 % of the two Pis' runs overlap, and one drop state for the whole switch reproduces that ([below](#a-drop-state-for-the-whole-switch-the-ls108gp-refitted-and-validated), [ADR-0016](adr/0016-switch-wide-drop-state.md)).
    - **The round trips are a little shorter,** 8.2 / 8.5 ms against 8.5 / 9.3 ms. The model has no host processing time on either end.
    - **At 2 × 3 900/s the model loses about 60 % of the hardware's frames,** 0.021–0.024 % against 0.036–0.039 %, from only about 30 runs per seed.

The results are in `benchmarks/results/2026-11-24-1538-simpy-rust-ns3-jaws-zbook-shared-pool.json`. `python -m benchmarks pool-summary <file>` prints the calibration and validation tables.

### A drop state for the whole switch: the LS108GP refitted and validated

With `ingress_drop=switch` a switch has one drop state, entered at any port's refusal and ended by ADR-0015's drain, and in it a frame is admitted only within its port's reserve ([ADR-0016](adr/0016-switch-wide-drop-state.md)). The engines apply the same rules:
- **Rust** gives SimPy's results bit for bit, event counts included.
- **ns-3** does too for Ethernet and PROFINET.
- **With one sender** it is ADR-0015's per-port state, exactly.

**Two Pis measured on one clock.** The laptop's port is FIFO, so the order in which the laptop receives frames is the order in which the switch admitted them. A second measurement at 10 Mb/s records the laptop's kernel receive times of both streams (`.seq.r`). A run's interval goes from the stream's last frame before it to its first frame after it, by those times on the hardware and by admission times in the model. A run overlaps if its interval meets one of the other stream's.

`python -m benchmarks switch-drop` refits the reserve, then predicts without refitting. It computes the hardware's numbers from the raw files, and counts a poll as retransmitted at a round trip of 200 ms, the TCP RTO: at 10 Mb/s a round trip with no retransmission reaches 170 ms.

**The refit.** With one sender nothing changes, so ADR-0015's L = P + R = 770 and D = 72 stand: every burst and the loss runs are the same under both scopes. R is refitted on the reserve test, with the pace of Pi 2's 64 B bursts beside it:

| R (cells) | 0 | 12 | 24 | 36 | 48 | 54 | **60** | 66 | 72 | 84 |
|---|---|---|---|---|---|---|---|---|---|---|
| Error | 33.3 | 5.4 | 2.1 | 1.2 | 0.57 | 0.45 | **0.25** | 0.36 | 0.52 | 0.92 |

| Pi 2's burst | 10 × 1 518 B | 20 × 1 518 B | 40 × 1 518 B | 50 × 64 B | 100 × 64 B | 200 × 64 B | 400 × 64 B |
|---|---|---|---|---|---|---|---|
| Measured: arrived | 5–9 (6.5) | 4–9 (6.5) | 7–12 (9) | 50 | 71–100 (100) | 112–168 (144) | 203–227 (207) |
| Model, R = 60 | 5–10 (7.5) | 5–10 (7.5) | 5–10 (7.5) | 50 | 62–100 (89.5) | 114–132 (126) | 182–242 (209) |

**The refitted LS108GP:** `cell=128 pool=710cells ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch`. R is determined to about ±12 cells, and the reserve is needed: R = 0 does not fit.

**Validation,** three seeds per case, at the hardware's durations at 10 Mb/s, 900 s and 300 s. Runs and overlaps are SimPy's, from an audit of its decisions. The last column is ADR-0015's per-port state, with its own fit:

| Case | | Hardware | `switch`: SimPy / Rust / ns-3 | `port`, SimPy |
|---|---|---|---|---|
| 10 Mb/s, 2 × 405/s | loss per stream | 0.82 / 0.77 % | 0.55–0.65 / 0.54–0.66 / 0.56–0.65 % | 0.55–0.67 % |
| | mean run | 3.34 / 3.20 | 3.20–3.34 | 3.69 |
| | single-frame runs | 14 % | 13–14 % | 7 % |
| | runs overlapping the other Pi's (shifted 500 ms) | 96 % (2 %) | 97 % (3–5 %) | 45 % |
| | episodes with both Pis | 93 % | 93–95 % | |
| | Pi polls retransmitted | 0.61 / 0.62 % | 0.44–0.56 / 0.45–0.50 / 0.38–0.60 % | 0.31–0.45 % |
| | Pi RTT p99 / max (ms) | 82.8 / 87.3 | 82.2 / 86.5 | |
| | Waveshare | 0 of 18 387 | 0 | |
| 10 Mb/s, 2 × 425/s | loss per stream | 4.98 / 5.01 % | 4.71–4.90 / 4.63–4.92 / 4.80–4.88 % | 4.71–4.93 % |
| | mean run | 3.38 / 3.39 | 3.36–3.45 | 3.81 |
| | single-frame runs | 12 % | 12 % | 6 % |
| | runs overlapping (shifted) | 97 % (5 %) | 97 % (11–13 %) | 50 % |
| | episodes with both Pis | 94 % | 94 % | |
| | Pi polls retransmitted | 4.13 / 4.22 % | 3.6–4.6 / 3.5–4.6 / 3.4–4.3 % | 2.9–3.6 % |
| | Waveshare | 0 of 3 397 | 0 | |
| 100 Mb/s, 2 × 4 050/s | loss per stream | 0.66 / 0.69 % | 0.57–0.68 / 0.49–0.67 / 0.59–0.67 % | 0.57–0.69 % |
| | mean run | 3.24 / 3.35 | 3.22–3.39 | 3.72 |
| | single-frame runs | 13 % | 13 % | 7 % |
| | runs overlapping | not measurable | 96 % (1–2 %) | 47 % |
| | Pi polls retransmitted | 0.53 / 0.54 % | 0.33–0.40 / 0.30–0.50 / 0.40–0.56 % | 0.30–0.58 % |
| | Waveshare | 0 of 6 000 | 0 | |
| 100 Mb/s, 2 × 3 900/s | loss per stream | 0.036 / 0.039 % | 0.01–0.04 % | 0.01–0.04 % |
| | mean run | 3.14 / 3.29 | 3.1–3.6 | 3.67 |
| | runs overlapping | not measurable | 90–94 % | 45 % |

- **The overlap, the halving of the runs and the single-frame share are predicted,** at 10 and at 100 Mb/s. A drop state per port gets none of them.
- **No overlap is measured at 100 Mb/s.** Those files have only the senders' clocks, and aligned from the losses they are good to about 1 ms, where a run lasts about 0.4 ms. They bound the overlap at about 18 %, which says nothing about the true value.
- **The single-source cases do not move.** 10 Mb/s with one Pi, 512 B, mixed sizes, ADR-0013's test and the batch case give the [previous section's](#a-shared-pool-the-ls108gp-calibrated-and-validated) results bit for bit, every seed and engine: L is the same, and the light ports always fit in their reserves.
- **The Pi's responses** are now lost in any drop state while the Pi's port is beyond its reserve. At 10 Mb/s that brings them near the hardware: 0.49 % against 0.37 % per port, with 0.61 % measured, and 4.0 % against 3.1 %, with 4.2 %. At 100 Mb/s it does not, 0.38 % against 0.45 %, with 0.53 % measured, within the seeds' spread.
- **What the model does not get: the two-sender loss.** The scope does not change it. It stays below the hardware by 22 % at 2 × 405, 3 % at 2 × 425, 9 % at 2 × 4 050 and about 37 % at 2 × 3 900, from about 30 runs a seed. With one Pi at the same 10 Mb/s load the hardware lost 0.60 %.

The results are in `benchmarks/results/2026-11-24-2122-simpy-rust-ns3-jaws-zbook-switch-drop-state.json`. `python -m benchmarks switch-drop-summary <file>` prints the tables.

