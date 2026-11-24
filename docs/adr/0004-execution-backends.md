# ADR-0004: Pluggable Execution Backends (ns-3 first)

**Status:** Proposed  
**Date:** 2026-10-24  
**Amended by:** ADR-0007 (2026-11-13)

> **Amendment (ADR-0007).** Backends no longer consume the `.smol` AST. They consume the execution-independent **Scenario IR** (`smolpy.model.Scenario`), which both the Python API and `.smol` lower into. As a result:
>
> - The Python API is **not** SimPy-only: `Network.simulate(..., backend="ns3")` is supported.
> - Semantic validation lives in `smolpy.model.validate`, not in `lang/validator.py`.
> - The ns-3 code generator reads the Scenario IR, not the AST.
>
> The ns-3 approach, the SMOL → ns-3 mapping, results interchange and capability checks below are unchanged. Superseded passages are marked in place. Batch runs and the cross-backend equivalence suite are defined in ADR-0009. The ns-3 backend is Phase 6 of the [roadmap](../roadmap.md).

---

## Context

ADR-0003 made SMOL a standalone language: a `.smol` file is parsed into a typed AST (`NetworkDecl`) and then interpreted. Today the interpreter has exactly one way to execute that AST — it builds a `Network` object and runs the built-in SimPy engine (`sim/engine.py`).

The SimPy engine is fast and well suited to sweeping parameters, but its model is deliberately simple:

- **No TCP.** Modbus TCP and MQTT frames are sized as if they carried TCP headers, but there is no connection setup, acknowledgement, retransmission or congestion control. Under load, real Modbus TCP and MQTT behave differently from what SimPy predicts.
- **No IP layer behaviour.** No ARP, no IP fragmentation, no per-host protocol stack delays.
- **Simplified shared media.** Hubs use a collision *approximation*, not a medium-access model.

Two needs follow from this:

1. **Fidelity and validation.** Results intended for publication must show that the fast model is accurate enough in the regime being studied — and where it stops being accurate. The standard way to do that is to run the same scenario on an independent, more detailed simulator and compare.
2. **Universality of the language.** If SMOL is only a front end to one engine, it is effectively a configuration format for SMOLPy. If the same `.smol` file can drive several tools of different fidelity, SMOL becomes a genuine, tool-independent network description language — a stronger contribution for the science article and a more useful tool.

[ns-3](https://www.nsnam.org/) is chosen as the first additional backend: it is a widely used, open-source, packet-level discrete-event network simulator with mature TCP/IP models, Ethernet-style devices, a learning bridge, and established use in networking research.

### Options considered

**Where backends attach**

- **A — backends consume the Python `Network` object.** Lets the Python API also select a backend, but every backend must reverse-engineer intent from runtime objects (e.g. `Adapter.traffic_specs`), and the Python API's dynamic, imperative nature is harder to translate faithfully.
- **B — backends consume the `.smol` AST (`NetworkDecl`).** The AST is already a clean, typed, declarative description of the whole scenario, independent of SimPy. **Chosen originally; superseded by ADR-0007, where backends consume the Scenario IR.**

**How the ns-3 backend runs**

- **A — in-process via ns-3's Python bindings.** Recent ns-3 releases ship cppyy-based Python bindings. However, SMOL needs custom ns-3 Applications (Modbus master/slave, MQTT broker/publisher, metric samplers); implementing them as Python callbacks puts Python in the per-packet hot path, and binding builds are tightly coupled to a specific ns-3 build.
- **B — code generation.** Translate the AST into a standalone ns-3 C++ scenario that uses a small C++ module shipped with SMOLPy, run it with ns-3's own build tooling, and read its metric output back. Keeps ns-3 an optional external dependency, runs at native speed, and makes the generated scenario an inspectable, archivable artefact (useful for reproducibility in publications). **Chosen.**
- **C — ns-3 emulation mode (real-time, tap devices).** Out of scope here; a candidate for a later ADR on emulated testbeds.

---

## Decision

### Backend abstraction

Introduce a `backends/` package. Every backend implements one protocol and consumes the AST:

```
src/smolpy/
├── lang/
│   ├── validator.py      # backend-independent semantic checks (superseded: smolpy.model.validate, ADR-0007)
│   └── ...
└── backends/
    ├── __init__.py       # registry: name → Backend
    ├── base.py           # Backend protocol, Capabilities
    ├── simpy_backend.py  # today's interpreter execution path
    └── ns3/
        ├── __init__.py
        ├── backend.py    # AST → C++ scenario, build/run, collect results
        ├── codegen.py    # scenario templates
        └── module/       # C++ sources: SMOL Applications + metric samplers
```

```python
class Backend(Protocol):
    name: str
    capabilities: Capabilities          # supported statements, metrics, simulate modes

    def run(self, decl: NetworkDecl, *, file: str) -> SimulationResult: ...  # ADR-0007: run(scenario: Scenario)
```

The `.smol` pipeline becomes three stages:

1. **parse** — `parse_smol()` (unchanged)
2. **validate** — backend-independent semantic checks (undeclared nodes, wrong node types, duplicate names, metric/node-type compatibility, exactly one `simulate`) move from `interpreter.py` into `lang/validator.py` (*superseded by ADR-0007: `smolpy.model.validate`, shared by the Python API*); then a **capability check** rejects anything the selected backend cannot execute, with a `SMOLSemanticError` such as `ns3 backend does not support metric 'collision_rate'`.
3. **execute** — `backend.run(decl)`

The SimPy backend is today's `_Interpreter.build_and_run()` moved behind the protocol, with no behavioural change. All existing tests must pass unchanged.

### Selecting a backend

The backend is chosen **outside** the `.smol` file, so the same file runs everywhere:

```bash
smolpy run topology.smol                    # simpy (default)
smolpy run topology.smol --backend ns3
smolpy backends                             # list backends and their capabilities
```

~~The Python API (`Network.simulate()`) remains SimPy-only; multi-backend execution is a feature of the language layer.~~ *Superseded by ADR-0007:* the Python API selects a backend directly with `Network.simulate(..., backend="ns3")`, because both front ends produce the same Scenario IR.

### ns-3 backend

**Execution.** The backend locates an ns-3 source tree via `--ns3-home` or the `NS3_HOME` environment variable, places the generated scenario and the SMOL C++ module under ns-3's `scratch/` area, builds and runs it with ns-3's build tool, and reads the results. A missing or incompatible ns-3 installation produces a clear error, not a traceback. The supported ns-3 release is pinned and documented; generated scenarios are kept on request (`--keep-generated`) for inspection and archiving.

*Amended 2026-11-16 (as built):* the scenario is not code-generated. `smol-ns3`, one C++ program (`src/smolpy/ns3/cpp/smol-ns3.cc`), is compiled once per ns-3 build and kept in the user's cache. For each run, the Python side lowers the scenario to plain lines of integers: every time in integer nanoseconds, every speed and period as an exact rational. The program reads them and runs.
- **Why.** A run does not build anything or write into the ns-3 tree. So runs are parallel and fast, and the images can run as an ordinary user.
- **Archiving.** The scenario JSON, the lowered input and the program's source, all in the repository, fix a run as exactly as generated code would.

**Results interchange.** ~~Samplers in the C++ module write the same long CSV format that `SimulationResult.export()` already produces (`time_ms,metric,value`).~~ *As built:* at each sample instant, the samplers write raw integer counters: bytes, bits and frames, the sum and count of latencies, and queue lengths. The Python side computes every metric from them with the reference engine's formulas, so each value is rounded once, as there. The backend loads the result into a regular `SimulationResult`, so `report()`, `export()` and `--output` work identically for both backends.

**Seed.** ~~The `simulate` statement's `seed` maps to ns-3's RNG seed/run configuration, so seeded runs are reproducible on both backends.~~ *As built (2026-11-16):* random traffic is not drawn by ns-3's generators. The Python side draws every frame of a random flow from the flow's own stream, the one Model Semantics §6 seeds from the run seed and its source id. It uses the reference engine's generator, order of draws and formulas, so the instants and sizes are the same as SimPy's. ns-3 sends the listed frames.
- **Why.** With the same seed, a stochastic scenario is the same run on SimPy and ns-3. Any difference is then the network's, not the traffic's, and the comparison is exact rather than statistical.
- **Cost.** Constant flows of fixed-size frames are still generated in C++. Only random flows and IMIX sizes are listed, one pair of integers per frame.

**Time.** *Added by the ADR-0007 revision of 2026-11-14.* The generator keeps ns-3's `Time` resolution at 1 ns and writes every time as integer nanoseconds, rounded by the rules of [Model Semantics §1](../model-semantics.md#1-time-and-units): delays, cycles, sampling instants and the end of the run. So ns-3 and the other engines share one clock. Timings that ns-3's own device and TCP/IP models compute are ns-3's, which is why ns-3 is compared under ADR-0009's high-fidelity rules.

**Mapping SMOL → ns-3**

| SMOL | ns-3 |
|---|---|
| `adapter`, `mqtt_broker`, `modbus_slave` | Node with Internet stack (IPv4 address from `ip`) |
| `profinet_device`, `controls` | *Added 2026-11-16:* a node whose device sends and receives PROFINET RT frames on layer 2; see "PROFINET RT on ns-3" below |
| `switch` (store-and-forward) | ~~Node with a learning bridge over one device per port~~ *As built:* a node with one SMOL Ethernet device per port and SMOL's switch, which learns, floods unknown destinations and drops hairpins. ns-3's `BridgeNetDevice` floods a hairpin instead of dropping it. *Amended by ADR-0013:* the switch keeps an ingress account per port, which its devices' egress queues release, and its limits are in bytes or frames. *Amended by ADR-0014:* or in cells, with a resume level and a drop state per account. |
| `switch mode=cut-through` | **unsupported** — capability error |
| `hub` | Shared CSMA channel *(hubs and `collision_rate` were later removed; see [Model Semantics](../model-semantics.md#open-issues), open issue 3)* |
| `link speed= length=` | *Amended 2026-11-16: SMOL's own full-duplex Ethernet device and channel, not CSMA; see "Link model on ns-3" below.* Device data rate = `speed`; channel delay = `length / 2·10⁸ m/s` (same propagation constant as `core/link.py`). *Since ADR-0010:* inter-frame gap = the time of 20 B at `speed` (preamble, SFD and gap, which CSMA does not model otherwise); CSMA adds the Ethernet header and FCS itself. *In ns-3.49 the gap is not an attribute: `Attach()` sets 12 B, so the generator calls `CsmaNetDevice::SetInterframeGap()` on each device after installing it.* |
| `flow` | UDP application; payload sized so the on-wire frame matches `size` (constant, Poisson, bursty patterns; `imix` sizes). *As built:* the payload is `size` minus 46 B (Ethernet 18, IPv4 20, UDP 8), sent to port 9 of the destination, where a socket discards it. |
| `publish` / `route` | MQTT publisher / broker Applications over one persistent TCP connection per client; QoS 1 sends PUBACK. *As built:* port 1883; subscribers connect to the broker as clients, and one connection carries all of a client's PUBLISH messages and copies (see "Modbus and MQTT over TCP" below). |
| `poll` | Modbus TCP master / slave Applications over one persistent TCP connection per master–slave pair (port 502), MBAP transaction IDs, Read Holding Registers (FC 03). *As built:* without transaction IDs. Responses come back in order on the connection, so the master pairs them with its requests in that order. *Since [ADR-0012](0012-modbus-one-request-in-flight.md):* a poll with `outstanding=1` numbers its requests, and accepts a response only with the outstanding id and before its timeout. |
| `observe` | Periodic sampler writing CSV rows |
| `simulate mode=` | `headless` only; `text`/`live` are capability errors (results are post-processed after the run) |

**Initial metric support**

| Metric | SimPy | ns-3 |
|---|---|---|
| `throughput`, `latency`, `bytes_sent`, `bytes_received` | ✓ | ✓ |
| `modbus_latency` | ✓ | ✓ |
| `queue_depth` | ✓ | ✓ (device / traffic-control queue length) |
| `utilization` | ✓ | ✓ |
| `broker_queue` | ✓ | ✓ (application-level queue in the broker app) |
| `collision_rate` | ✓ | — (shared-medium semantics differ; see below) *(removed)* |

**Metric definitions on ns-3** *(added 2026-11-15)*

**Decision: the ns-3 backend computes SMOLPy's own metric definitions** ([Model Semantics §8](../model-semantics.md#8-metrics)), with the samplers of the SMOL C++ module. ns-3's FlowMonitor is not the source of any metric. The definitions stay what they are because:
- they are what SMOLPy measures: windows per node, and PROFINET latency, jitter and watchdog;
- ADR-0009 compares backends metric by metric, which needs the same definition on every backend;
- adopting FlowMonitor's definitions would lose the window and PROFINET metrics.

| Metric | Measured in ns-3 at |
|---|---|
| `throughput`, `bytes_received` | frames addressed to the node, received by its device: frame bytes, header to FCS (`MacRx`) |
| `bytes_sent` | frames whose transmission on the node's device has completed (`PhyTxEnd`), frame bytes |
| `latency` | from the origin time, carried in a packet tag set when the data is created, to reception by the device. Queueing at the sender is included, as in SimPy. |
| `modbus_latency` | the same, with the request's origin tag copied into the response. *As built:* measured when the master has the whole response. |
| `pn_latency`, `pn_jitter`, `pn_watchdog_expiries` | the SMOL PROFINET consumer application, with the definitions of Model Semantics §7. *As built:* the node's consumer, called by its device for each PROFINET frame; see "PROFINET RT on ns-3" below. |
| `queue_depth` | frames waiting in the egress queues of the switch's devices, not counting the frame in transmission |
| `utilization` | frame + 20 B per frame completed on the links into the node, as a share of their capacity (ADR-0010) |
| `broker_queue` | the queue of the broker application. *As built:* messages waiting for a subscriber's connection, which the subscriber has not yet opened. The broker handles a message at once, as on SimPy, so the queue is otherwise empty. |

- **How FlowMonitor differs.** This is for comparisons with ns-3 literature, and is to be checked against the pinned release. FlowMonitor's statistics:
    - are per flow (IP 5-tuple), not per node and window;
    - are cumulative over the run;
    - are measured at the IP layer: bytes from the IP header, without the Ethernet header and FCS, and delay from IP send to IP receive;
    - have a jitter that is the sum of |delay_i − delay_(i−1)| over consecutive packets (IPDV), not the deviation from a cycle.

    Numbers from FlowMonitor and from SMOLPy are therefore different quantities, and the generated scenario does not mix them.
- **Per-flow metrics comparable with FlowMonitor**, such as a flow latency and IPDV (RFC 3393), are a candidate for a later decision. They are not part of this one.

### Cross-backend consistency tests

A new test suite runs selected scenarios on both backends and compares metrics:

- **Low-load scenarios** (no queuing, no TCP congestion effects) — metrics must agree within a stated tolerance. Disagreement here indicates a translation bug in the backend.
- **Loaded scenarios** — differences are expected and are reported rather than asserted; quantifying them is part of the research output.

The ns-3 suite is skipped automatically when ns-3 is not available, and runs in CI in a container image with the pinned ns-3 release ([below](#container-images)).

### Container images

*Added 2026-11-15.*

ns-3 is the one backend that cannot be installed with `pip`. It is a C++ code base that must be built from source with its own toolchain, which takes tens of minutes, and SMOLPy supports exactly one pinned release. The ns-3 backend is therefore shipped, run in CI and archived as a **container image**. A lighter image without ns-3 serves users who want SMOLPy without installing Python.

*As built (2026-11-16):* the image without ns-3 is `docker/Dockerfile`, built by `.github/workflows/docker-image.yml`. Each release, `publish.yml` calls it once both packages are on PyPI. It tests the image of each platform with the wheel smoke test and a `.smol` run through the CLI before it pushes `<version>` and `latest`. A pull request that changes the image builds and tests it for `linux/amd64` and pushes nothing. The image runs as uid 1000, not as root.

*As built (2026-11-16):* the image with ns-3 is `docker/Dockerfile.ns3`, built by a second job of the same workflow, for `linux/amd64` and (*as built, 2026-11-17*) `linux/arm64`. Each platform is built and tested on a runner of its own architecture, since `smol-ns3` is compiled in the image, and pushed as `<version>-ns3-amd64` and `-arm64`; `<version>-ns3` and `latest-ns3` are the index of both:
- **Contents.** The base image `smolpy-ns3-base:3.49`, plus `smolpy` and `smolpy-engine-rs` from the same wheels as the image without ns-3.
- **`smol-ns3` built in.** It is built once, in the image, into a cache that every user reads (`XDG_CACHE_HOME=/opt/smolpy/cache`). No run compiles it. The cache is read-only, so a run that would need another build, for example with another `CXX`, fails instead of building one. Its message says to set `XDG_CACHE_HOME` to a writable directory, where the build then takes about 8 s.
- **Tests.** Before the image is pushed, CI runs `docker/smoke.smol`, with every protocol, on ns-3 through the CLI as uid 1000. It also runs the ns-3 suite of the same commit against the SMOLPy installed in the image.
- **Tags.** `<version>-ns3` and `latest-ns3`, first published with 1.10.0. `latest` remains the image without ns-3.
- **Size.** 1.26 GB unpacked, 303 MB compressed, for 1.9.1.dev9. ns-3's build and the C++ toolchain stay in the image.

**Registry and images.** The images are published to the GitHub Container Registry, `ghcr.io/jwszolek/smolpy`, public, for `linux/amd64` and `linux/arm64`:

| Tag | Contents | Size | From |
|---|---|---|---|
| `<version>`, `latest` | Python, `smolpy` and `smolpy-engine-rs` from the release's own wheels | ~ 200 MB | the first release that publishes the Rust engine (ADR-0008 step 7) |
| `<version>-ns3`, `latest-ns3` | the above, plus the pinned ns-3 release built with the SMOL C++ module, and `NS3_HOME` set | ~~several GB~~ *as built:* 1.3 GB, 300 MB compressed | Phase 6, with the ns-3 backend |

The entry point is the CLI:

```bash
docker run --rm -v "$PWD:/work" -w /work ghcr.io/jwszolek/smolpy run plant.smol --backend rust
docker run --rm -v "$PWD:/work" -w /work ghcr.io/jwszolek/smolpy:latest-ns3 run plant.smol --backend ns3
```

- **Headless only.** The live dashboard needs a display, which a container does not have. Headless runs, text mode, `--runs` and `-o` all work.
- **Built at release.** `publish.yml` builds and pushes the images at each release, after `smolpy` and `smolpy-engine-rs` are on PyPI, from the same files. The pushed images are listed under the repository's "Packages". The Dockerfiles live in `docker/`.
- **ns-3 is built once per pinned release.** It is built in a separate base image, `ghcr.io/jwszolek/smolpy-ns3-base:<ns-3 version>`, which is rebuilt only when the pinned version changes. Each release then adds SMOLPy to it in a few minutes, instead of rebuilding ns-3.
- **CI uses the image.** The ns-3 equivalence suite (ADR-0009) runs inside `<version>-ns3`, or inside the base image for pull requests. It never builds ns-3 itself. *As built (2026-11-16):* pull requests run it in the base image, against the SMOLPy of the pull request. Each release runs it in the new `<version>-ns3` image before pushing it.
- **Reproducible results.** An image tag fixes the SMOLPy, Rust engine and ns-3 versions together. Recording the image digest next to the scenario JSON, and the generated ns-3 scenario (`--keep-generated`), is enough to rerun a published experiment exactly.
- **Licences.** ns-3 is licensed under GPLv2, and SMOLPy and its Rust engine under MIT.
    - The `-ns3` image distributes ns-3 binaries and the SMOL C++ module linked against them, so that image as a whole is distributed under GPLv2's terms.
    - Its ns-3 sources are public at the pinned release, and the image records where. The SMOL module's source is in this repository.
    - MIT is compatible with GPLv2, so the MIT code needs no change.
    - The image without ns-3 contains no GPL code.
- **Not a replacement for pip.** `pip install smolpy smolpy-engine-rs` stays the main way to install SMOLPy. The images are for ns-3, for running without a Python setup, and for archiving.

**Link model on ns-3** *(decided 2026-11-16)*

ns-3.49's CSMA is a shared bus: the two ends of a link wait for each other, and a busy link backs off. SMOLPy's links are full duplex. With CSMA, ns-3 would differ from the other engines because of the link model, not because of the TCP/IP stack it is there to bring.

- **Decision.** The ns-3 backend uses SMOL's own Ethernet device and channel, from the SMOL C++ module.
    - The device is full duplex, with one egress queue per port. It is unbounded, or, on a switch with `buffer`, bounded with tail drop (*amended by [ADR-0011](0011-bounded-switch-buffers.md), 2026-11-23*). An idle port takes a queued frame after the events already scheduled for that instant, as the reference engine does; the `smol-ns3` line format is version 2, version 3 since [ADR-0012](0012-modbus-one-request-in-flight.md), version 4 since [ADR-0013](0013-ingress-buffer-accounting.md), and version 5 since [ADR-0014](0014-buffer-cells-and-hysteresis.md).
    - A frame is its IP packet plus 18 B, at least 64 B.
    - It occupies the wire for preamble, SFD and frame, and the next frame waits for the 12 B gap (ADR-0010).
    - Times are rounded as in Model Semantics §1.
- **IP on top.** IPv4 runs on the device with a /32 address and every destination on the link. IPv6 is not installed, so there are no router solicitations or other IPv6 traffic. The neighbour caches start full, because SMOL models no ARP.
- **What ns-3 contributes** is its IPv4/UDP stack, and its TCP stack for Modbus and MQTT ("Modbus and MQTT over TCP" below). The differences that remain between engines are the protocol stack's, which is what the comparison is for.
- **Options rejected.**
    - **CSMA as planned.** Its differences from the other engines would come from half duplex.
    - **`SimpleNetDevice`.** It is full duplex, but it counts neither the Ethernet header and FCS nor the preamble and gap: about 38 B per frame too few.

**First results** *(2026-11-16)*. Everything Ethernet is in scope: adapters, switches and flows with every pattern, IMIX sizes and 802.1Q priorities.
- **Deterministic scenarios.** `line`, `switching` and `bridging` give SimPy's results bit for bit, through ns-3's IPv4/UDP stack. `bridging` has a flooding learning core, a hairpin at a converged switch, jumbo frames and a saturated 10 Mb/s link.
- **Stochastic scenarios.** `random-load` and `groups`, with Poisson, bursty and batch arrivals, IMIX and priorities, are bit for bit the same as SimPy's with the same seed, because the frames are drawn from the same streams. With other seeds they agree statistically.
- **Consequence.** The suite compares ns-3 exactly, like Rust. ADR-0009's high-fidelity rules remain for TCP.

ns-3 adds a priority to a frame as a packet tag. Its device then adds the 802.1Q tag's 4 B and serves its egress queue strictly by priority, or FIFO on a switch with `qos="fifo"`.

**Modbus and MQTT over TCP** *(decided and built 2026-11-16)*

What ns-3 is there for is its TCP. The backend therefore keeps everything else as on the other engines, so that the differences that remain are TCP's.
- **Connections.**
    - One persistent connection per Modbus master and slave (port 502), and one per MQTT client and broker (port 1883).
    - A client that publishes to a broker and subscribes to it uses one connection.
    - Every connection is opened at the start of the run. The first messages come later.
- **Messages, not bytes.**
    - The applications send whole protocol messages, sized in TCP payload bytes with SMOL's formulas (`frames.py`: `mqtt_publish_message_bytes`, `modbus_response_message_bytes`, ...).
    - The bytes carry nothing. Each connection keeps, per direction, the messages whose bytes are on the way. TCP delivers bytes in order, so a message has arrived when all of its bytes have.
    - Each message's bytes carry its origin time as a byte tag. So `latency` is measured per frame for TCP data as for UDP. Segments without data, such as acknowledgements and the handshake, have no origin and do not count in `latency`.
- **SMOL's frame sizes.**
    - Every segment has a 20 B TCP header, as SMOL sizes Modbus and MQTT frames: the timestamp option is off. Window scaling and SACK stay on. They add 8 B to the SYN and SYN-ACK, and SACK blocks only to the acknowledgements of a loss, which SMOL's unbounded queues do not cause.
    - The segment size is Ethernet's 1460 B, not ns-3's default of 536 B.
- **ns-3's defaults otherwise.** Congestion control is CUBIC. Delayed acknowledgements are every second segment or after 200 ms. Nagle is off (`TcpNoDelay`). The minimum RTO is 1 s.
    - *Amended by [ADR-0012](0012-modbus-one-request-in-flight.md), 2026-11-23:* the settings that shape recovery from a loss are Linux's: a minimum RTO of 200 ms, a clock granularity of 200 ms, a delayed-ACK timeout of 40 ms and a SYN RTO of 1 s. They are a property of the backend, `smolpy.ns3.TcpSettings`, not of the scenario.
- **Applications.**
    - They act at the instant a message arrives, as on SimPy.
    - They never block: the send buffer (1 GiB) holds what TCP cannot send yet.
    - A broker sends its copies to subscribers first and then the PUBACK, as on SimPy.
- **Options rejected.**
    - **Parsing real MQTT and MBAP bytes in the stream.** SMOL's sizes are not real encodings: for example, MQTT's remaining length takes 2 B above 127 B. Real encodings would change the frame sizes.
    - **ns-3's timestamp option.** It is on in Linux, but it would make every frame 12 B larger than SMOL's. That difference would come from sizing, not from TCP's behaviour.

**What TCP changes, to the nanosecond** (tests in `tests/test_ns3.py`):
- **1 ns per send.** ns-3 sends an application's data one time step after the call, which is 1 ns here. So a Modbus round trip and a PUBLISH from publisher to subscriber both take SimPy's time plus 2 ns. A broker's or slave's reply would otherwise leave at once, ahead of other data: `Send()` on the connection that has just received data is flushed in the same event. Replies are therefore scheduled as events of their own, in the order the application makes them.
- **The handshake.** Each client receives a SYN-ACK of 66 B at the start, and switches learn the stations from it.
- **The first data segment** of a connection is acknowledged at once. ns-3 counts the handshake as a full delayed-ACK count. The reply waits behind that frame, 6.72 µs at 100 Mb/s.
- **Acknowledgements.**
    - **Request and response (Modbus, MQTT QoS 1).** Each reply carries the acknowledgement of the request, and the next request carries the acknowledgement of the reply. No extra frames.
    - **One-way PUBLISH (QoS 0, and copies to subscribers).** The receiver sends a 64 B acknowledgement for every second segment. At a broker, that frame leaves just before the copies and delays them by its transmission time.

**First results over TCP** *(2026-11-16)*. The suite's TCP scenarios sample the same metrics at the same instants as SimPy (`test_over_tcp`). The table gives the means of their values over the run:

| Scenario | Metric | SimPy | ns-3 | Why |
|---|---|---|---|---|
| `protocols` | `modbus_latency:scada` | 43.76 µs | 44.37 µs | +2 ns per round trip; +6.72 µs on the first one |
| | `latency:hmi` | 47.12 µs | 49.26 µs | copies behind the broker's acknowledgements of the QoS 0 topic |
| | `bytes_received:scada` | 3.19 kB | 3.38 kB | SYN-ACKs and the first acknowledgements |
| `polling` | `modbus_latency:scada` | 123.21 µs | 131.05 µs | the first window: the handshakes on the 10 Mb/s link; then +2 ns |
| | `bytes_received:plc` | 1.06 kB | 1.30 kB | SYN-ACKs and the first acknowledgements |
| | `utilization:edge` | 0.40 % | 0.48 % | the handshakes, in the first windows |
| `messaging` (loaded uplink) | `bytes_received:hall`, `:cell` | 0.48 kB, 0.31 kB | 0.86 kB, 0.61 kB | a 64 B acknowledgement for every second QoS 0 PUBLISH |
| | `utilization:plant` | 1.07 % | 1.67 % | the acknowledgements sent to the broker |
| | `latency:hmi`, `:scada`, `:historian` | 291, 266, 119 µs | 289, 259, 115 µs | acknowledgements change the order of frames in the FIFO queues, both ways |
| | `queue_depth:edge` | 0.0175 | 0.0063 | the same |

- **Low load.** After the first messages, ns-3 is SimPy plus 2 ns per message. The remaining differences are the handshake, the first acknowledgement, and the acknowledgements of one-way traffic.
- **Loaded.** The acknowledgements reorder frames in shared queues, so differences remain throughout the run. They are reported, not asserted (ADR-0009). Quantifying them under heavier load, where TCP's flow and congestion control act, is the next part of this research.

**PROFINET RT on ns-3** *(built 2026-11-16)*

PROFINET RT is a layer 2 protocol (EtherType 0x8892), without IP, so ns-3's stack has nothing to add to it. The backend runs it on SMOL's Ethernet device, so that scenarios that mix PROFINET with flows, Modbus and MQTT run whole on ns-3.
- **Providers.** Each AR has a provider on the controller (outputs) and on the device (inputs). Each one hands a frame to its device every cycle, from the AR's start delay on.
    - Frames are sized by the model's formula (`pn_frame_size`), 802.1Q tag and padding included.
    - Their priority is 6, served strictly by the egress queues, or FIFO on a switch with `qos="fifo"`.
    - The providers start by controller in node order, as on the reference engine.
- **Consumers.** Each node's consumer measures latency and jitter, and supervises each AR with a watchdog, as defined in Model Semantics §7. The watchdog is one ns-3 event at `hold × cycle` after the last frame. Each frame cancels it and sets it again.
- **The deadline comes first.** A frame that arrives exactly at the deadline was scheduled after the watchdog, at the end of its transmission. ns-3 runs events of one instant in the order they were scheduled, so the expiry counts, and the frame then restarts supervision. That is SimPy's outcome, where the watchdog's timeout fires before the frame is delivered.
- **Results.** `priorities`, `cyclic` and `batches` (with the same seed) give SimPy's results bit for bit. That includes watchdog expiries under load, and a hold of one cycle with no jitter, where every frame arrives at its deadline (`test_a_frame_at_the_watchdogs_deadline_is_too_late`).

**Results under load** *(2026-11-16)*. `python -m benchmarks load` (`benchmarks/load.py`) runs scenario L1 on SimPy and ns-3. L1 is a plant cell whose Modbus and MQTT traffic shares a 10 Mb/s uplink with a UDP camera. The camera's rate sets the uplink's offered load ρ from 0.5 to 1.5. The table and the explanations are in [Execution backends](../backends.md#modbus-and-mqtt-over-tcp). In short:
- **ρ ≤ 0.95:** within 0.4 %.
- **ρ = 1.00:** +18 % on the Modbus round trip and +25 % on MQTT latency. TCP's acknowledgements add about 0.7 % to a link that is exactly full on SimPy.
- **1.05 ≤ ρ ≤ 1.2:** both engines overloaded, within a few percent.
- **ρ ≥ 1.35:** the MQTT connections are limited by their windows (window / RTT), as the RTT reaches seconds. Their messages wait in the senders' TCP buffers, while the UDP camera takes more of the link. MQTT latency is +24 % and +43 %, and the HMI receives 26 % and 41 % less.
- **How the cause was established.** A run with ns-3's minimum RTO raised from 1 s to 100 s, and one with a 64 MiB receive buffer, gave the same results to the digit. Tracing the connections showed no losses or retransmissions, and every MQTT connection's bytes in flight at its congestion or receive window.

This meets Phase 6's exit criterion that differences under load are quantified and documented.

**The ns-3 base image, as built** *(2026-11-16)*:
- `ghcr.io/jwszolek/smolpy-ns3-base:3.49`, built by `.github/workflows/ns3-base.yml` from `docker/ns3-base/Dockerfile`, for `linux/amd64` and `linux/arm64`. Each platform is built and tested on a GitHub runner of its own architecture (`ubuntu-latest`, `ubuntu-24.04-arm`) and pushed as `3.49-amd64` and `3.49-arm64`; `3.49` is the index of both. It is rebuilt when that Dockerfile changes, and pushed after the merge to `main`.
- **Pinned release:** ns-3.49 (released 2026-10-07), as the git tag `ns-3.49` with its commit checked. Debian 12's g++ 12 and CMake 3.25 meet its minimums: g++ 11.1, C++23 and CMake 3.25.
- **Build:**
    - The `release` profile, without examples, tests or Python bindings.
    - The modules `core`, `network`, `internet`, `applications`, `csma`, `bridge`, `traffic-control` and `stats`, plus what they need.
    - Not the `optimized` profile: it compiles with `-march=native`, and a binary built for the CPU of the CI runner may not run on another one.
    - A local build with 15 threads takes 80 s.
- **Test:** each build compiles and runs `docker/ns3-base/smoke.cc` as a scratch program against the prebuilt modules. The program is two hosts on CSMA links to a bridge, with the SMOL wire model and a UDP echo.
- **Findings for the generator** (step 3):
    - **Scratch programs work.** A scenario placed in `scratch/` is built against the prebuilt modules, so no contrib module is needed.
    - **The inter-frame gap** is set with `SetInterframeGap()`, as in the mapping table.
    - **Neighbour caches must start full** (`NeighborCacheHelper::PopulateNeighborCache()`). SMOL models no ARP. Without the full caches, ns-3 delays its first ARP request by up to 10 ms (`ArpL3Protocol::RequestJitter`), and its queue of 3 pending packets drops the rest.

### Documentation

- `docs/backends.md` — backend concept, installing ns-3, `--backend`, capability tables, known fidelity differences.
- `docs/language-reference.md` — note that `.smol` semantics are backend-independent, with backend-specific limits listed in `docs/backends.md`.

### To verify during implementation

- Exact behaviour of ns-3's CSMA model versus SimPy's collision approximation, to decide whether `hub` support should be limited further. *(Resolved: hubs were removed.)*
- How ns-3 queue lengths should be sampled so that `queue_depth` is comparable to SimPy's per-port frame count.
- Whether the pinned ns-3 release's build tool supports building scratch programs with an extra module cleanly, or the module should be installed as a contrib module. *(Resolved: scratch programs build against the prebuilt modules; see the base image above.)*
- The size of the `-ns3` image, and whether to build ns-3 in its `optimized` profile only, without examples and tests, to keep it small.
- Whether GitHub's hosted runners can build the ns-3 base image for `linux/arm64` in reasonable time, or that platform needs a native arm64 runner or is offered for `amd64` only at first. *(Decided: `amd64` only at first; `arm64` on a native runner later. As built (2026-11-17): the base image on GitHub's hosted arm64 runner.)*

---

## Consequences

**Positive**
- SMOL becomes a tool-independent network description language: one `.smol` file, several engines of different fidelity — a stronger and more citable contribution.
- Realistic TCP behaviour for Modbus TCP and MQTT becomes available without complicating the SimPy engine.
- Cross-backend comparison gives a principled answer to the model-validation question: where the fast model agrees with ns-3, and where it does not.
- Semantic validation becomes a separate, reusable stage, which future backends (e.g. OMNeT++/INET via NED generation, or emulated testbeds) can reuse unchanged.
- Generated ns-3 scenarios are concrete artefacts that can be archived alongside published results.

**Negative / trade-offs**
- ns-3 is a large, optional external dependency that users must install and build themselves; it cannot be a regular PyPI dependency.
- SMOLPy gains C++ code (Applications and samplers) that must be maintained against the pinned ns-3 release.
- Metric parity is an ongoing cost: every new metric or statement needs an implementation, or an explicit capability exclusion, in every backend.
- ~~The Python API stays SimPy-only for now; scenarios written in Python cannot use other backends without first being expressed as `.smol`.~~ *Resolved by ADR-0007.*
- ns-3 runs are much slower than SimPy runs, so large parameter sweeps remain a SimPy task, with ns-3 used to validate selected points.
