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

**Results interchange.** Samplers in the C++ module write the same long CSV format that `SimulationResult.export()` already produces (`time_ms,metric,value`). The backend loads that file into a regular `SimulationResult`, so `report()`, `export()` and `--output` work identically for both backends.

**Seed.** The `simulate` statement's `seed` maps to ns-3's RNG seed/run configuration, so seeded runs are reproducible on both backends.

**Time.** *Added by the ADR-0007 revision of 2026-11-14.* The generator keeps ns-3's `Time` resolution at 1 ns and writes every time as integer nanoseconds, rounded by the rules of [Model Semantics §1](../model-semantics.md#1-time-and-units): delays, cycles, sampling instants and the end of the run. So ns-3 and the other engines share one clock. Timings that ns-3's own device and TCP/IP models compute are ns-3's, which is why ns-3 is compared under ADR-0009's high-fidelity rules.

**Mapping SMOL → ns-3**

| SMOL | ns-3 |
|---|---|
| `adapter`, `mqtt_broker`, `modbus_slave` | Node with Internet stack (IPv4 address from `ip`) |
| `switch` (store-and-forward) | Node with a learning bridge over one device per port |
| `switch mode=cut-through` | **unsupported** — capability error |
| `hub` | Shared CSMA channel *(hubs and `collision_rate` were later removed; see [Model Semantics](../model-semantics.md#open-issues), open issue 3)* |
| `link speed= length=` | Device data rate = `speed`; channel delay = `length / 2·10⁸ m/s` (same propagation constant as `core/link.py`). *Since ADR-0010:* `InterframeGap` = the time of 20 B at `speed` (preamble, SFD and gap, which CSMA does not model otherwise); CSMA adds the Ethernet header and FCS itself |
| `flow` | UDP application; payload sized so the on-wire frame matches `size` (constant, Poisson, bursty patterns; `imix` sizes) |
| `publish` / `route` | MQTT publisher / broker Applications over one persistent TCP connection per client; QoS 1 sends PUBACK |
| `poll` | Modbus TCP master / slave Applications over one persistent TCP connection per master–slave pair (port 502), MBAP transaction IDs, Read Holding Registers (FC 03) |
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
| `modbus_latency` | the same, with the request's origin tag copied into the response |
| `pn_latency`, `pn_jitter`, `pn_watchdog_expiries` | the SMOL PROFINET consumer application, with the definitions of Model Semantics §7 |
| `queue_depth` | frames waiting in the egress queues of the switch's devices, not counting the frame in transmission |
| `utilization` | frame + 20 B per frame completed on the links into the node, as a share of their capacity (ADR-0010) |
| `broker_queue` | the queue of the broker application |

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

**Registry and images.** The images are published to the GitHub Container Registry, `ghcr.io/jwszolek/smolpy`, public, for `linux/amd64` and `linux/arm64`:

| Tag | Contents | Size | From |
|---|---|---|---|
| `<version>`, `latest` | Python, `smolpy` and `smolpy-engine-rs` from the release's own wheels | ~ 200 MB | the first release that publishes the Rust engine (ADR-0008 step 7) |
| `<version>-ns3`, `latest-ns3` | the above, plus the pinned ns-3 release built with the SMOL C++ module, and `NS3_HOME` set | several GB | Phase 6, with the ns-3 backend |

The entry point is the CLI:

```bash
docker run --rm -v "$PWD:/work" -w /work ghcr.io/jwszolek/smolpy run plant.smol --backend rust
docker run --rm -v "$PWD:/work" -w /work ghcr.io/jwszolek/smolpy:latest-ns3 run plant.smol --backend ns3
```

- **Headless only.** The live dashboard needs a display, which a container does not have. Headless runs, text mode, `--runs` and `-o` all work.
- **Built at release.** `publish.yml` builds and pushes the images at each release, after `smolpy` and `smolpy-engine-rs` are on PyPI, from the same files. The pushed images are listed under the repository's "Packages". The Dockerfiles live in `docker/`.
- **ns-3 is built once per pinned release.** It is built in a separate base image, `ghcr.io/jwszolek/smolpy-ns3-base:<ns-3 version>`, which is rebuilt only when the pinned version changes. Each release then adds SMOLPy to it in a few minutes, instead of rebuilding ns-3.
- **CI uses the image.** The ns-3 equivalence suite (ADR-0009) runs inside `<version>-ns3`, or inside the base image for pull requests. It never builds ns-3 itself.
- **Reproducible results.** An image tag fixes the SMOLPy, Rust engine and ns-3 versions together. Recording the image digest next to the scenario JSON, and the generated ns-3 scenario (`--keep-generated`), is enough to rerun a published experiment exactly.
- **Licences.** ns-3 is licensed under GPLv2, and SMOLPy and its Rust engine under MIT.
    - The `-ns3` image distributes ns-3 binaries and the SMOL C++ module linked against them, so that image as a whole is distributed under GPLv2's terms.
    - Its ns-3 sources are public at the pinned release, and the image records where. The SMOL module's source is in this repository.
    - MIT is compatible with GPLv2, so the MIT code needs no change.
    - The image without ns-3 contains no GPL code.
- **Not a replacement for pip.** `pip install smolpy smolpy-engine-rs` stays the main way to install SMOLPy. The images are for ns-3, for running without a Python setup, and for archiving.

### Documentation

- `docs/backends.md` — backend concept, installing ns-3, `--backend`, capability tables, known fidelity differences.
- `docs/language-reference.md` — note that `.smol` semantics are backend-independent, with backend-specific limits listed in `docs/backends.md`.

### To verify during implementation

- Exact behaviour of ns-3's CSMA model versus SimPy's collision approximation, to decide whether `hub` support should be limited further. *(Resolved: hubs were removed.)*
- How ns-3 queue lengths should be sampled so that `queue_depth` is comparable to SimPy's per-port frame count.
- Whether the pinned ns-3 release's build tool supports building scratch programs with an extra module cleanly, or the module should be installed as a contrib module.
- The size of the `-ns3` image, and whether to build ns-3 in its `optimized` profile only, without examples and tests, to keep it small.
- Whether GitHub's hosted runners can build the ns-3 base image for `linux/arm64` in reasonable time, or that platform needs a native arm64 runner or is offered for `amd64` only at first.

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
