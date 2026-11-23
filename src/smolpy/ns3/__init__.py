"""The ns-3 backend (ADR-0004): SMOLPy scenarios on ns-3's IPv4 stack, with
UDP for flows and TCP for Modbus and MQTT, and PROFINET RT on layer 2.

A scenario is lowered to plain lines of integers (every time in integer
nanoseconds, every speed and period an exact rational) and run by
``smol-ns3``, a program built once per ns-3 installation from
``cpp/smol-ns3.cc`` and kept in the user's cache. Each run only executes it,
so runs do not touch the ns-3 tree and can run in parallel.

``smol-ns3`` reports raw integer counters at each sample instant; the
metrics are computed from them here, with the reference engine's formulas,
so each value is rounded once, as there (Model Semantics §8).

Locating ns-3: ``NS3_HOME`` names an ns-3 source tree built with its
libraries in ``build/lib`` and headers in ``build/include``, as in the image
``ghcr.io/jwszolek/smolpy-ns3-base``. The C++ compiler is ``$CXX`` or ``g++``.
"""

from __future__ import annotations

import glob
import hashlib
import os
import random
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from smolpy import model
from smolpy.model import timebase as tb
from smolpy.model.frames import (
    IFG_BYTES,
    MODBUS_REQUEST_MESSAGE_BYTES,
    modbus_response_message_bytes,
    mqtt_publish_message_bytes,
    pn_frame_size,
)
from smolpy.model.streams import source_ids, stream_seed

if TYPE_CHECKING:
    from smolpy.core.network import SimulationResult

FORMAT_VERSION = 2  # 2: switch buffers (ADR-0011)
UDP_IP_BYTES = 8 + 20
ETHERNET_BYTES = 14 + 4  # header and FCS
PROGRAM = "smol-ns3"
MODBUS_PORT = 502
MQTT_PORT = 1883


class Ns3Unavailable(RuntimeError):
    """No usable ns-3 build or C++ compiler; the message says why."""


@dataclass(frozen=True)
class Installation:
    home: Path
    lib_dir: Path
    include_dir: Path
    libs: tuple[str, ...]  # linker names, e.g. ns3.49-core
    compiler: str


def locate() -> Installation:
    """The ns-3 build that ``NS3_HOME`` names, and a compiler."""
    home = os.environ.get("NS3_HOME")
    if not home:
        raise Ns3Unavailable("NS3_HOME is not set: it must name a built ns-3 source tree")
    root = Path(home)
    lib_dir, include_dir = root / "build" / "lib", root / "build" / "include"
    cores = sorted(glob.glob(str(lib_dir / "libns3*-core*.so")), key=len)
    if not cores or not (include_dir / "ns3" / "core-module.h").is_file():
        raise Ns3Unavailable(f"NS3_HOME={home} has no ns-3 build (build/lib, build/include)")
    core = Path(cores[0]).name[len("lib") : -len(".so")]  # e.g. ns3.49-core
    libs = tuple(core.replace("-core", f"-{m}", 1) for m in ("internet", "network", "core"))
    for lib in libs:
        if not (lib_dir / f"lib{lib}.so").is_file():
            raise Ns3Unavailable(f"the ns-3 build in {home} lacks the library lib{lib}.so")
    compiler = os.environ.get("CXX", "g++")
    if shutil.which(compiler) is None:
        raise Ns3Unavailable(f"no C++ compiler: {compiler!r} is not on PATH (set CXX)")
    return Installation(root, lib_dir, include_dir, libs, compiler)


def _source() -> bytes:
    return resources.files(__package__).joinpath("cpp", "smol-ns3.cc").read_bytes()


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "smolpy" / "ns3"


def program(ns3: Installation) -> Path:
    """``smol-ns3`` for this ns-3 build, compiled on first use."""
    key = hashlib.sha256()
    for part in (
        _source(),
        str(ns3.home.resolve()).encode(),
        " ".join(ns3.libs).encode(),
        ns3.compiler.encode(),
    ):
        key.update(part)
        key.update(b"\0")
    target = _cache_dir() / key.hexdigest()[:16] / PROGRAM
    if target.is_file():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # e.g. the read-only cache of the -ns3 image
        raise Ns3Unavailable(
            f"{PROGRAM} for this ns-3 build and compiler ({ns3.compiler}) is not built, and"
            f" cannot be built in {target.parent.parent}: {exc.strerror}. Set XDG_CACHE_HOME"
            " to a writable directory to build it there."
        ) from exc
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        src = Path(tmp) / "smol-ns3.cc"
        src.write_bytes(_source())
        built = Path(tmp) / PROGRAM
        cmd = [
            ns3.compiler,
            "-std=c++23",
            "-O2",
            "-DNDEBUG",
            f"-I{ns3.include_dir}",
            str(src),
            "-o",
            str(built),
            f"-L{ns3.lib_dir}",
            *(f"-l{lib}" for lib in ns3.libs),
            f"-Wl,-rpath,{ns3.lib_dir}",
        ]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"building {PROGRAM} against {ns3.home} failed:\n{done.stderr}")
        os.replace(built, target)  # atomic: a parallel build may have won, which is fine
    return target


# --- lowering ---


def _ratio(x: tb.Exact) -> tuple[int, int]:
    f = Fraction(x)
    return f.numerator, f.denominator


@dataclass
class _Sampler:
    """How one observation's raw counters become samples."""

    key: str
    metric: str
    speed: Fraction = Fraction(0)  # utilization: Mb/s into the target


def _strict(node: model.NodeSpec) -> bool:
    """Whether a node's egress queues serve priorities strictly."""
    return not (isinstance(node, model.SwitchNode) and node.qos == "fifo")


def _sends(
    flow: model.Flow, rng: random.Random, delay_ns: int, duration_ns: int
) -> Iterator[tuple[int, int]]:
    """Every frame of *flow* before *duration_ns*: its instant and size
    (untagged, FCS included), drawn from *rng* in the order and with the
    formulas of the reference engine's ``_traffic_gen``, so the frames are
    the same as there."""
    from smolpy.sim.engine import _resolve_size

    def size() -> int:
        return _resolve_size(flow.size, rng)

    now = delay_ns
    period = tb.interval_ns(flow.rate)
    mean_ns = float(period)
    if flow.pattern == "constant":
        for at in tb.instants(delay_ns, period):
            if at >= duration_ns:
                return
            yield at, size()
    elif flow.pattern == "poisson":
        while True:
            now += tb.draw_ns(rng.expovariate(1.0 / mean_ns))
            if now >= duration_ns:
                return
            yield now, size()
    elif flow.pattern == "batch":
        assert flow.batch is not None
        group_ns = mean_ns * flow.batch.mean()
        while True:
            now += tb.draw_ns(rng.expovariate(1.0 / group_ns))
            if now >= duration_ns:
                return
            for _ in range(flow.batch.draw(rng)):
                yield now, size()
    else:  # bursty
        quarter = Fraction(period) / 4
        while True:
            burst_len = max(1, int(rng.paretovariate(1.5)))
            in_burst = tb.instants(now, quarter)
            for _ in range(burst_len):
                now = next(in_burst)
                if now >= duration_ns:
                    return
                yield now, size()
            now += tb.draw_ns(mean_ns * burst_len * rng.uniform(0.3, 1.2))


def lower(scenario: model.Scenario) -> tuple[str, list[_Sampler]]:
    """The input of ``smol-ns3``, and the samplers its output feeds."""
    from smolpy.sim.engine import _topology_mac_table

    assert scenario.run is not None
    nodes = {n.name: n for n in scenario.nodes}
    index = {n.name: i for i, n in enumerate(scenario.nodes)}
    lines = [f"smol-ns3 {FORMAT_VERSION}", f"duration {tb.ms_to_ns(scenario.run.duration_ms)}"]
    for n in scenario.nodes:
        if isinstance(n, model.SwitchNode):
            lines.append(f"switch {n.buffer_bytes or 0}")  # 0: unbounded
        else:
            lines.append(f"adapter {n.mac} {n.ip}")  # a station, with IPv4

    next_port: dict[str, int] = {n.name: 0 for n in scenario.nodes}
    neighbours: dict[str, list[tuple[int, str]]] = {n.name: [] for n in scenario.nodes}
    static: dict[str, dict[str, int]] = {}
    has_link: set[str] = set()
    into: dict[str, list[tuple[int, Fraction]]] = {}  # node -> (device, speed) sending to it
    for i, link in enumerate(scenario.links):
        a, b = nodes[link.a], nodes[link.b]
        port_a, port_b = next_port[a.name], next_port[b.name]
        next_port[a.name] += 1
        next_port[b.name] += 1
        speed = Fraction(tb.exact(link.speed_mbps))
        num, den = _ratio(speed)
        lines.append(
            f"link {index[a.name]} {index[b.name]} {num} {den}"
            f" {tb.propagation_ns(link.length_m)} {tb.transmission_ns(IFG_BYTES, link.speed_mbps)}"
            f" {int(_strict(a))} {int(_strict(b))}"
        )
        neighbours[a.name].append((port_a, b.name))
        neighbours[b.name].append((port_b, a.name))
        has_link.update((a.name, b.name))
        into.setdefault(a.name, []).append((2 * i + 1, speed))
        into.setdefault(b.name, []).append((2 * i, speed))
        # a switch knows the stations wired to it from the start
        for end, other, port in ((a, b, port_b), (b, a, port_a)):
            if not isinstance(end, model.SwitchNode) and isinstance(other, model.SwitchNode):
                static.setdefault(other.name, {})[end.mac] = port
    for n in scenario.nodes:
        if isinstance(n, model.SwitchNode) and n.mac_table == "topology":
            table = static.setdefault(n.name, {})
            for mac, port in _topology_mac_table(nodes, n.name, neighbours).items():
                table.setdefault(mac, port)
    for name, table in static.items():
        lines.extend(f"static {index[name]} {mac} {port}" for mac, port in table.items())

    sinks: list[str] = []
    duration_ns = tb.ms_to_ns(scenario.run.duration_ms)
    connections: dict[tuple[str, str], int] = {}  # one per client and server
    topics: dict[str, int] = {}

    def connection(client: str, server: str, port: int) -> int:
        if (client, server) not in connections:
            connections[client, server] = len(connections)
            lines.append(f"connect {index[client]} {index[server]} {port}")
        return connections[client, server]

    def periodic(rate: float, delay_ms: float) -> str:
        num, den = _ratio(tb.interval_ns(rate))
        return f"{tb.ms_to_ns(delay_ms)} {num} {den}"

    # PROFINET ARs first, by controller in node order, as the reference engine
    # starts them: their first frames are sent at the start of the run
    ars = [t for t in scenario.traffic if isinstance(t, model.ProfinetAR)]
    for ar_id, pn in enumerate(sorted(ars, key=lambda ar: index[ar.controller])):
        if pn.controller in has_link and pn.device in has_link:
            lines.append(
                f"ar {index[pn.controller]} {index[pn.device]} {ar_id}"
                f" {tb.ms_to_ns(pn.cycle_ms)} {pn.hold} {tb.ms_to_ns(pn.delay_ms)}"
                f" {pn_frame_size(pn.outputs)} {pn_frame_size(pn.inputs)}"
            )
    for t, source_id in zip(scenario.traffic, source_ids(scenario), strict=True):
        if isinstance(t, model.ProfinetAR):
            continue
        if isinstance(t, model.Poll):
            if t.source in has_link:
                c = connection(t.source, t.slave, MODBUS_PORT)
                response = modbus_response_message_bytes(t.count)
                lines.append(
                    f"poll {c} {MODBUS_REQUEST_MESSAGE_BYTES} {response}"
                    f" {periodic(t.rate, t.delay_ms)}"
                )
            continue
        if isinstance(t, model.Publish):
            if t.source in has_link:
                c = connection(t.source, t.broker, MQTT_PORT)
                topic = topics.setdefault(t.topic, len(topics))
                message = mqtt_publish_message_bytes(t.topic, t.payload, t.qos)
                lines.append(
                    f"publish {c} {message} {t.qos} {topic} {periodic(t.rate, t.delay_ms)}"
                )
            continue
        if isinstance(t, model.Route):
            topic = topics.setdefault(t.topic, len(topics))
            subscribers = [
                connection(sub, t.broker, MQTT_PORT) for sub in t.subscribers if sub in has_link
            ]
            listed = " ".join(str(c) for c in subscribers)
            lines.append(f"route {index[t.broker]} {topic} {listed}".rstrip())
            continue
        if not isinstance(t, model.Flow):
            raise ValueError(f"the ns3 backend does not support {t.kind} traffic")
        if t.source not in has_link:
            continue
        dst = nodes[t.destination]
        assert isinstance(dst, model.AdapterNode)
        delay_ns = tb.ms_to_ns(t.delay_ms)
        ends = f"{index[t.source]} {dst.ip}"
        if t.pattern == "constant" and isinstance(t.size, int):
            num, den = _ratio(tb.interval_ns(t.rate))
            payload = t.size - ETHERNET_BYTES - UDP_IP_BYTES
            lines.append(f"flow {ends} {payload} {t.priority} {delay_ns} {num} {den}")
        else:
            rng = random.Random(stream_seed(scenario.run.seed, source_id))
            sends = list(_sends(t, rng, delay_ns, duration_ns))
            listed = " ".join(f"{at} {size - ETHERNET_BYTES - UDP_IP_BYTES}" for at, size in sends)
            lines.append(f"sends {ends} {t.priority} {len(sends)} {listed}".rstrip())
        if dst.name not in sinks:
            sinks.append(dst.name)
    lines.extend(f"sink {index[name]}" for name in sinks)

    samplers: list[_Sampler] = []
    for obs in scenario.observations:
        target = nodes[obs.target]
        key = f"{obs.metric}:{target.name}"
        adapter = isinstance(target, model.AdapterNode)
        devices = ""
        speed = Fraction(0)
        metric = obs.metric
        if metric in ("throughput", "latency", "bytes_received", "modbus_latency"):
            ok = adapter and target.name in has_link
        elif metric == "broker_queue":
            ok = isinstance(target, model.MqttBrokerNode)
        elif metric in ("pn_latency", "pn_jitter", "pn_watchdog_expiries"):
            ok = isinstance(target, model.AdapterNode | model.ProfinetDeviceNode)
        elif metric in ("bytes_sent", "frames_sent", "frames_received", "modbus_unanswered"):
            ok = adapter and target.name in has_link
        elif metric == "queue_depth":
            ok = isinstance(target, model.SwitchNode)
        elif metric in ("drops", "drops_total"):
            ok = isinstance(target, model.SwitchNode)
            metric = "drops"  # smol-ns3 reports the total; drops is its difference
        elif metric == "utilization":
            ok = bool(into.get(target.name))
            devices = " ".join(str(d) for d, _ in into.get(target.name, []))
            speed = sum((s for _, s in into.get(target.name, [])), Fraction(0))
        else:
            raise ValueError(f"the ns3 backend does not support the metric {metric!r}")
        samplers.append(_Sampler(key, obs.metric if ok else "", speed))
        if ok:
            num, den = _ratio(tb.period_ns(obs.every_ms))
            lines.append(f"observe {metric} {index[target.name]} {num} {den} {devices}".rstrip())
    return "\n".join(lines) + "\n", samplers


# --- running ---


def _metrics(
    output: str, samplers: list[_Sampler]
) -> tuple[dict[str, list[tuple[float, float]]], int]:
    """The samples of each metric key, with the reference engine's formulas."""
    active = [s for s in samplers if s.metric]
    rows: list[list[tuple[int, int, int]]] = [[] for _ in active]
    events = 0
    for line in output.splitlines():
        fields = line.split()
        if fields[0] == "events":
            events = int(fields[1])
            continue
        i, t, a, b = (int(f) for f in fields)
        rows[i].append((t, a, b))

    metrics: dict[str, list[tuple[float, float]]] = {s.key: [] for s in samplers}
    for s, samples in zip(active, rows, strict=True):
        out = metrics[s.key]
        last_t = last = 0
        for t, a, b in samples:
            t_ms = tb.ns_to_ms(t)
            if s.metric == "throughput":
                out.append((t_ms, (a - last) * 8 * tb.NS_PER_US / (t - last_t)))
            elif s.metric in ("latency", "modbus_latency", "pn_latency"):
                if b:
                    out.append((t_ms, a / (b * tb.NS_PER_US)))
            elif s.metric == "pn_jitter":
                if b:
                    out.append((t_ms, a / tb.NS_PER_US))
            elif s.metric == "bytes_received":
                out.append((t_ms, a / 1_000_000))
            elif s.metric == "bytes_sent":
                out.append((t_ms, a // 8 / 1_000_000))
            elif s.metric in (
                "queue_depth",
                "broker_queue",
                "pn_watchdog_expiries",
                "drops_total",
                "frames_sent",
                "frames_received",
            ):
                out.append((t_ms, float(a)))
            elif s.metric == "drops":
                out.append((t_ms, float(a - last)))
            elif s.metric == "modbus_unanswered":
                out.append((t_ms, float(a - b)))
            elif s.metric == "utilization":
                out.append((t_ms, tb.quotient((a - last) * 100_000, s.speed * (t - last_t))))
            last_t, last = t, a
    for key in metrics:  # a key observed twice: its samplers' samples in time order
        if sum(1 for s in active if s.key == key) > 1:
            metrics[key].sort(key=lambda sample: sample[0])
    return metrics, events


def run(scenario: model.Scenario) -> SimulationResult:
    from smolpy.core.network import RunStats, SimulationResult

    ns3 = locate()
    binary = program(ns3)
    text, samplers = lower(scenario)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="smolpy-ns3-") as tmp:
        scenario_path, samples_path = Path(tmp) / "scenario.txt", Path(tmp) / "samples.txt"
        scenario_path.write_text(text)
        done = subprocess.run(
            [str(binary), str(scenario_path), str(samples_path)], capture_output=True, text=True
        )
        if done.returncode != 0:
            raise RuntimeError(f"{PROGRAM} failed ({done.returncode}):\n{done.stderr}")
        output = samples_path.read_text()
    wall_s = time.perf_counter() - started
    metrics, events = _metrics(output, samplers)
    return SimulationResult(
        metrics, scenario, RunStats(backend="ns3", events=events, wall_s=wall_s)
    )
