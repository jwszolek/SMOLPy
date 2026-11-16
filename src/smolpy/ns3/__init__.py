"""The ns-3 backend (ADR-0004): SMOLPy scenarios on ns-3's IPv4/UDP stack.

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
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from smolpy import model
from smolpy.model import timebase as tb
from smolpy.model.frames import IFG_BYTES

if TYPE_CHECKING:
    from smolpy.core.network import SimulationResult

FORMAT_VERSION = 1
UDP_IP_BYTES = 8 + 20
ETHERNET_BYTES = 14 + 4  # header and FCS
PROGRAM = "smol-ns3"


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
    target.parent.mkdir(parents=True, exist_ok=True)
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


def lower(scenario: model.Scenario) -> tuple[str, list[_Sampler]]:
    """The input of ``smol-ns3``, and the samplers its output feeds."""
    from smolpy.sim.engine import _topology_mac_table

    assert scenario.run is not None
    nodes = {n.name: n for n in scenario.nodes}
    index = {n.name: i for i, n in enumerate(scenario.nodes)}
    lines = [f"smol-ns3 {FORMAT_VERSION}", f"duration {tb.ms_to_ns(scenario.run.duration_ms)}"]
    for n in scenario.nodes:
        if isinstance(n, model.AdapterNode):
            lines.append(f"adapter {n.mac} {n.ip}")
        elif isinstance(n, model.SwitchNode):
            lines.append("switch")
        else:  # the capabilities exclude it
            raise ValueError(f"the ns3 backend does not support {n.kind} nodes")

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
        )
        neighbours[a.name].append((port_a, b.name))
        neighbours[b.name].append((port_b, a.name))
        has_link.update((a.name, b.name))
        into.setdefault(a.name, []).append((2 * i + 1, speed))
        into.setdefault(b.name, []).append((2 * i, speed))
        # a switch knows the stations wired to it from the start
        for end, other, port in ((a, b, port_b), (b, a, port_a)):
            if isinstance(end, model.AdapterNode) and isinstance(other, model.SwitchNode):
                static.setdefault(other.name, {})[end.mac] = port
    for n in scenario.nodes:
        if isinstance(n, model.SwitchNode) and n.mac_table == "topology":
            table = static.setdefault(n.name, {})
            for mac, port in _topology_mac_table(nodes, n.name, neighbours).items():
                table.setdefault(mac, port)
    for name, table in static.items():
        lines.extend(f"static {index[name]} {mac} {port}" for mac, port in table.items())

    sinks: list[str] = []
    for t in scenario.traffic:
        if not isinstance(t, model.Flow):
            raise ValueError(f"the ns3 backend does not support {t.kind} traffic")
        if t.source not in has_link:
            continue
        dst = nodes[t.destination]
        assert isinstance(dst, model.AdapterNode) and isinstance(t.size, int)
        num, den = _ratio(tb.interval_ns(t.rate))
        payload = t.size - ETHERNET_BYTES - UDP_IP_BYTES
        lines.append(
            f"flow {index[t.source]} {dst.ip} {payload} {tb.ms_to_ns(t.delay_ms)} {num} {den}"
        )
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
        if metric in ("throughput", "latency", "bytes_received"):
            ok = adapter and target.name in has_link
        elif metric == "bytes_sent":
            ok = adapter and target.name in has_link
        elif metric == "queue_depth":
            ok = isinstance(target, model.SwitchNode)
        elif metric == "utilization":
            ok = bool(into.get(target.name))
            devices = " ".join(str(d) for d, _ in into.get(target.name, []))
            speed = sum((s for _, s in into.get(target.name, [])), Fraction(0))
        else:
            raise ValueError(f"the ns3 backend does not support the metric {metric!r}")
        samplers.append(_Sampler(key, metric if ok else "", speed))
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
            elif s.metric == "latency":
                if b:
                    out.append((t_ms, a / (b * tb.NS_PER_US)))
            elif s.metric == "bytes_received":
                out.append((t_ms, a / 1_000_000))
            elif s.metric == "bytes_sent":
                out.append((t_ms, a // 8 / 1_000_000))
            elif s.metric == "queue_depth":
                out.append((t_ms, float(a)))
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
