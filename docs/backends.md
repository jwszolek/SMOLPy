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

Each Modbus master and slave, and each MQTT client and broker, share one persistent TCP connection, opened at the start. ns-3's TCP defaults apply: CUBIC, delayed acknowledgements, no Nagle. Timestamps are off, so that segments have the 20 B header SMOL sizes them with.

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
| 1.20 | 1584 → 1557 ms (−2 %) | 1583 → 1586 ms | 1.1872 → 1.1274 MB (−5 %) |
| 1.35 | 2463 → 2455 ms | 2463 → 3057 ms (+24 %) | 1.0553 → 0.7781 MB (−26 %) |
| 1.50 | 3166 → 3356 ms (+6 %) | 3167 → 4528 ms (+43 %) | 0.9499 → 0.5648 MB (−41 %) |

- **Below saturation**, ns-3 agrees with SimPy within 0.4 %.
- **At ρ = 1.00** the link is exactly full on SimPy. ns-3's acknowledgements add about 0.7 % of the uplink: the sensor with QoS 1 acknowledges every second PUBACK. That overloads the link, so its queue grows slowly.
- **Overloaded**, both engines queue without bound, and the latencies agree within a few percent while the queueing delay stays under about a second.
- **Heavily overloaded**, TCP's windows decide. A connection can have at most its congestion window (or the receiver's window) in flight, so its rate is at most window / RTT. As the RTT reaches seconds, that falls below the sensors' publishing rate. Their messages wait in their TCP send buffers, and the camera's UDP stream, which does not back off, takes more of the link. On SimPy every source keeps sending, and the switch's queue shares the link in proportion to what each one offers.
- **No losses.** SMOL's queues are unbounded, so nothing is dropped and TCP retransmits nothing. A real switch would drop frames, and TCP would react to the losses as well: that is outside SMOL's model.

The results are in `benchmarks/results/*-tcp-under-load.json`. `python -m benchmarks load-summary <file>` prints their table.
