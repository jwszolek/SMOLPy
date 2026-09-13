# ADR-0001: Protocol Package Structure

**Status:** Accepted  
**Date:** 2026-07-24

---

## Context

SMOLPy started with a single `dsl/` package containing all node types — `Adapter`, `Switch`, `Hub`, `Link`, `MQTTBroker` — mixed together regardless of which network protocol they belong to. This works for two protocols (Ethernet and MQTT), but the project roadmap includes adding Modbus, Profibus, Profinet, BACnet, and other industrial protocols. Keeping everything flat in `dsl/` will become increasingly hard to navigate and maintain as the protocol count grows.

Two restructuring approaches were considered:

**Option A — per-protocol packages, each owning its node types**  
Each protocol package would define its own endpoint adapter, meaning `Adapter` (which currently handles both Ethernet `sends()` and MQTT `publishes()`) would need to be split or duplicated across packages.

**Option B — capability-based adapter in core, protocol packages for infrastructure nodes**  
A single `Adapter` lives in `core/` as a protocol-agnostic endpoint. Protocol packages define only their infrastructure nodes (brokers, masters, slaves, gateways). The adapter gains protocol-specific capabilities (`sends()`, `publishes()`, future `writes_register()`) as methods, regardless of which protocol it participates in.

---

## Decision

Adopt **Option B**. The source tree is reorganised as follows:

```
src/smolpy/
├── core/
│   ├── node.py          # Abstract Node base class
│   ├── adapter.py       # Adapter — protocol-agnostic endpoint node
│   ├── link.py          # Physical link (speed, length, duplex)
│   ├── observation.py   # Metric observation definition
│   └── network.py       # Network — top-level orchestrator and DSL entry point
├── ethernet/
│   ├── switch.py        # MAC-learning switch
│   └── hub.py           # Broadcast hub
├── mqtt/
│   └── broker.py        # MQTT broker with topic routing and QoS 0/1
├── sim/
│   └── engine.py        # SimPy discrete-event simulation engine
└── viz/
    ├── dashboard.py      # Dear PyGui live dashboard
    └── text_dashboard.py # Rich terminal dashboard
```

New protocol packages (e.g. `modbus/`, `profibus/`, `profinet/`) are added as siblings under `src/smolpy/`. Each package defines only the infrastructure nodes specific to that protocol. The `Adapter` in `core/` accumulates the corresponding traffic-generation methods.

The public API exposed via `smolpy.__init__` is unchanged — `Network`, `Adapter`, `Switch`, `Hub`, `MQTTBroker` etc. remain importable from the top-level package.

---

## Consequences

**Positive**
- Each protocol is self-contained and easy to locate.
- Adding a new protocol means adding one new package with no changes to `core/`.
- `Adapter` accurately models real devices — a PLC or sensor node that speaks one or more protocols, not a protocol-specific construct.
- The flat `dsl/` grab-bag is eliminated.

**Negative / trade-offs**
- `Adapter` accumulates methods over time as protocols are added; its interface grows. This is manageable as long as each method is clearly associated with its protocol via naming convention (`sends()` → Ethernet, `publishes()` → MQTT, `writes_register()` → Modbus, etc.).
- One-time refactor required — all existing imports inside the package and in tests must be updated.
- External code that imports directly from `smolpy.dsl.*` (rather than from `smolpy`) will break; only the top-level public API is guaranteed stable.
