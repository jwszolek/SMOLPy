# ADR-0003: SMOL External DSL

**Status:** Accepted  
**Date:** 2026-07-24

---

## Context

SMOLPy currently exposes an internal DSL — users describe networks by writing Python scripts that call builder methods on a `Network` object. This is practical for Python developers but has two limitations relevant to the project's goals:

1. **Accessibility** — users must know Python to describe a network topology. The original SMOL was a standalone Network Description Language; SMOLPy should offer the same capability.
2. **Scientific contribution** — for publication purposes, a formal language with its own syntax and grammar is a more citable and referencable artefact than a Python API.

Introducing an external DSL (`.smol` files) restores the "language" character of SMOL while keeping the Python API intact as the execution substrate underneath.

---

## Decision

Introduce a `lang/` package that implements a parser, AST transformer, and interpreter for `.smol` files. The CLI is extended to accept `.smol` files alongside existing Python scripts. The Python API is unchanged.

### Parser library

**Lark** (`lark` on PyPI) is chosen as the parsing library because:
- Pure Python, no native dependencies
- Clean EBNF-style grammar syntax stored as a plain `.lark` file
- Automatically produces a parse tree; a `Transformer` subclass converts it to typed AST nodes
- Precise error messages with line and column numbers
- Well maintained and widely used

### Package layout

```
src/smolpy/
└── lang/
    ├── __init__.py
    ├── grammar.lark      # formal EBNF grammar
    ├── transformer.py    # Lark Tree → typed AST dataclasses
    └── interpreter.py    # AST → Network builder calls
```

### Language syntax

A `.smol` file contains a single `network` block. All node declarations, links, traffic flows, observations, and the simulation directive appear inside it.

```
network "office-net" {

    # --- nodes ---
    adapter      host-A   ip=10.0.0.1
    adapter      server   ip=10.0.0.10
    switch       sw1      ports=8  mode=store-and-forward
    hub          hub1     ports=4
    mqtt_broker  broker1  ip=10.0.2.1
    modbus_slave sensor1  ip=10.0.3.1  unit_id=1

    # --- links ---
    link  host-A  --  sw1      speed=1000   length=5
    link  server  --  sw1      speed=10000  length=2

    # --- traffic ---
    flow     host-A  ->  server   rate=8000  size=1518  pattern=constant
    flow     host-A  ->  server   rate=4000  size=512   pattern=poisson  delay=2000

    publish  sensor  ->  broker1  topic="plant/temp"  rate=1.0  payload=20  qos=1
    route    broker1  topic="plant/temp"  ->  [server]

    poll     plc  ->  sensor1  register=40001  count=10  rate=1.0

    # --- observations ---
    observe  throughput    on  server   every=100
    observe  queue_depth   on  sw1      every=50
    observe  modbus_latency  on  plc    every=500

    # --- simulation ---
    simulate  duration=30000  mode=text
}
```

### Statement reference

| Statement | Maps to Python API |
|---|---|
| `adapter <name> ip=<ip> [mac=<mac>]` | `net.adapter(name, ip=ip, mac=mac)` |
| `switch <name> ports=<n> [mode=<m>]` | `net.switch(name, ports=n, mode=m)` |
| `hub <name> ports=<n>` | `net.hub(name, ports=n)` |
| `mqtt_broker <name> ip=<ip>` | `net.mqtt_broker(name, ip=ip)` |
| `modbus_slave <name> ip=<ip> unit_id=<n>` | `net.modbus_slave(name, ip=ip, unit_id=n)` |
| `link <a> -- <b> speed=<s> length=<l>` | `net.link(a, b, speed=s, length=l)` |
| `flow <src> -> <dst> rate=<r> ...` | `src.sends(to=dst, rate=r, ...)` |
| `publish <src> -> <broker> topic=<t> ...` | `src.publishes(to=broker, topic=t, ...)` |
| `route <broker> topic=<t> -> [<nodes>]` | `broker.routes(topic, to=[nodes])` |
| `poll <master> -> <slave> register=<r> count=<c> rate=<hz>` | `master.polls(slave, register=r, count=c, rate=hz)` |
| `observe <metric> on <node> every=<ms>` | `net.observe(metric, on=node, every=ms)` |
| `simulate duration=<ms> [mode=headless\|text\|live]` | `net.simulate(duration, text=..., live=...)` |

### `lang/` module responsibilities

**`grammar.lark`**  
Formal EBNF grammar. Defines terminals (identifiers, quoted strings, numbers, keywords) and rules (statements, blocks, argument lists). Lark uses this to produce a raw parse tree.

**`transformer.py`**  
A `lark.Transformer` subclass that converts each grammar rule into a typed Python dataclass (e.g. `AdapterDecl`, `LinkDecl`, `FlowDecl`). The output is a `NetworkDecl` dataclass containing ordered lists of all declarations — a clean, inspectable AST independent of Lark internals.

**`interpreter.py`**  
Walks the `NetworkDecl` AST in declaration order and calls the existing `Network` builder API. Raises `SMOLSemanticError` for semantic violations (unknown node reference, invalid metric for node type, duplicate node name, etc.) with a message that includes the source location.

### CLI update

`smolpy run` currently executes Python scripts via `exec()`. It is extended to branch on file extension:

```
smolpy run topology.smol   # → lang.interpreter path
smolpy run topology.py     # → existing Python exec path
```

### Error handling

Two error classes are introduced:

| Class | Raised by | Example |
|---|---|---|
| `SMOLSyntaxError` | Lark / transformer | Unknown keyword, missing argument |
| `SMOLSemanticError` | Interpreter | `observe throughput on sw1` (switch has no throughput) |

Both include file name, line number, and a plain-English message.

### Documentation

A `docs/language-reference.md` file documents the complete syntax, all statement forms, supported metrics per node type, and worked examples for Ethernet, MQTT, and Modbus topologies. This serves as the citable language specification in the science article.

### Dependency addition

```toml
# pyproject.toml
dependencies = [
    ...
    "lark>=1.2",
]
```

---

## Consequences

**Positive**
- Network topologies can be described without Python knowledge — the `.smol` format is readable by domain engineers unfamiliar with programming.
- The language is a formal, citable artefact for the science article.
- The parser and interpreter are independently testable; the Python API is unaffected.
- New protocol statements (e.g. future BACnet, OPC-UA) are added by extending the grammar and interpreter — no changes to the transformer or CLI.

**Negative / trade-offs**
- Lark is a new runtime dependency.
- The `.smol` format is declarative — it cannot express loops, conditionals, or parameterised topologies. Complex or programmatically generated scenarios still require the Python API.
- Grammar changes are a breaking change to `.smol` files written against an earlier version; versioning of the language syntax may be needed as the protocol set grows.
