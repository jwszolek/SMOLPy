"""Bounded switch buffers on the three engines: the testbed of ADR-0011.

Scenario T1, after a hardware testbed (a TP-Link LS108GP, an unmanaged
8 x 1 Gb/s store-and-forward switch): a Raspberry Pi on a 1 Gb/s port sends
Poisson UDP traffic of 1 518 B frames to a laptop whose port runs at
100 Mb/s, and the laptop polls a Modbus TCP server on the Pi every 10 ms.
The responses share the laptop's port with the UDP stream. Here the Pi is
two stations, a UDP source and a Modbus slave, each on a 1 Gb/s port.

rho is the UDP stream's wire bits as a share of the laptop's 100 Mb/s; the
Modbus responses add 0.085 %. Two buffers per port:
- 115 000 B, what the testbed's capped RTT points to. A full queue holds 75
  UDP frames (113 850 B) and keeps room for a response (86 B), so only UDP
  frames are lost;
- 113 900 B, 75 UDP frames and 50 B: a full queue drops responses too, so
  polls are lost, where SimPy and Rust lose them and ns-3's TCP retransmits.

Each load runs with several seeds on every engine. SimPy and ns-3 draw the
same UDP stream for a seed; Rust draws its own, a different sample of the
same process. The record keeps, per run:
- the Modbus round trip: mean, median, p99 and maximum of its per-window
  means over 1 ms windows. Polls are 10 ms apart, so a window holds one
  response, unless TCP delivers several at once after a retransmission;
- the polls without a response at the end, lost and in flight;
- the share of frames the switch dropped, of the frames the Pi sent.

SimPy and Rust lose a frame for good; ns-3 runs Modbus over its TCP, which
retransmits it.

    python -m benchmarks loss [--loads 0.93 0.996] [--duration 30000]
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from pathlib import Path

from benchmarks.runner import RESULTS_DIR, environment
from smolpy import Network, backends
from smolpy.model import Scenario
from smolpy.model.frames import WIRE_OVERHEAD_BYTES

LAPTOP_MBPS = 100
FRAME = 1_518
BUFFER = 115_000
BUFFERS = (BUFFER, 75 * FRAME + 50)
POLL_RATE = 100  # polls/s
REGISTERS = 10
LOADS = (0.90, 0.93, 0.96, 0.98, 0.99, 0.996, 1.0, 1.02, 1.05)
DURATION_MS = 30_000
ENGINES = ("simpy", "rust", "ns3")
SEEDS = (1, 2, 3, 4, 5)
# the testbed, at the loads it was measured at
MEASURED = {
    0.93: "RTT as SMOLPy's: mean, median, p90 and p99 within a few percent",
    0.996: "RTT capped at about 9.8 ms; 0.09 % of UDP frames lost",
}


def scenario(
    rho: float,
    duration_ms: float = DURATION_MS,
    buffer: int | None = BUFFER,
    seed: int = SEEDS[0],
    outstanding: int | None = None,
    timeout_ms: float | None = None,
) -> Scenario:
    """T1 with the UDP stream at *rho* of the laptop's port; its polls open
    loop, or with one request in flight (ADR-0012, ``benchmarks.closed_loop``)."""
    net = Network("T1")
    sw = net.switch("sw", ports=3, buffer=buffer)
    pi = net.adapter("pi", ip="10.0.0.1")
    server = net.modbus_slave("server", ip="10.0.0.3", unit_id=1)
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=1_000, length=2)
    net.link(server, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=LAPTOP_MBPS, length=2)
    rate = rho * LAPTOP_MBPS * 1e6 / ((FRAME + WIRE_OVERHEAD_BYTES) * 8)
    pi.sends(laptop, rate=rate, size=FRAME, pattern="poisson")
    laptop.polls(
        server,
        register=0,
        count=REGISTERS,
        rate=POLL_RATE,
        outstanding=outstanding,
        timeout_ms=timeout_ms,
    )
    net.observe("modbus_latency", on=laptop, every=1)
    end = duration_ms - 1  # the last sample, at the end of the run
    net.observe("modbus_unanswered", on=laptop, every=end)
    if outstanding is not None:
        net.observe("modbus_timeouts", on=laptop, every=end)
        net.observe("modbus_skipped", on=laptop, every=end)
    net.observe("frames_sent", on=pi, every=end)
    net.observe("drops_total", on=sw, every=end)
    return net.to_scenario(duration=duration_ms, seed=seed)


def _summary(metrics: dict[str, list[tuple[float, float]]]) -> dict[str, float]:
    rtt = [v / 1_000 for _, v in metrics["modbus_latency:laptop"]]  # ms
    last = {k: (s[-1][1] if s else math.nan) for k, s in metrics.items()}
    sent = last["frames_sent:pi"]
    q = statistics.quantiles(rtt, n=100) if len(rtt) > 1 else [math.nan] * 99
    return {
        "rtt_mean_ms": statistics.fmean(rtt) if rtt else math.nan,
        "rtt_median_ms": q[49],
        "rtt_p99_ms": q[98],
        "rtt_max_ms": max(rtt, default=math.nan),
        "unanswered": last["modbus_unanswered:laptop"],
        "dropped": last["drops_total:sw"] / sent if sent else math.nan,
    }


def run_loss(
    loads: list[float],
    duration_ms: float,
    engines: list[str],
    label: str = "bounded-buffers",
    out: Path | None = None,
    seeds: tuple[int, ...] = SEEDS,
    buffers: tuple[int, ...] = BUFFERS,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": "smolpy-loss",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "scenario": "T1",
        "duration_ms": duration_ms,
        "seeds": list(seeds),
        "runs": [],
    }
    for buffer in buffers:
        for rho in loads:
            for seed in seeds:
                sc = scenario(rho, duration_ms, buffer, seed)
                for engine in engines:
                    result = backends.get(engine).run(sc)
                    record = {
                        "buffer_bytes": buffer,
                        "rho": rho,
                        "seed": seed,
                        "backend": engine,
                        "events": result.stats.events,
                        "wall_s": result.stats.wall_s,
                        "summary": _summary(result.metrics),
                    }
                    doc["runs"].append(record)
                    out.write_text(json.dumps(doc, indent=1))  # keep partial progress
                    s = record["summary"]
                    print(
                        f"buffer {buffer} rho {rho:5.3f} seed {seed} {engine:6s}"
                        f" RTT median {s['rtt_median_ms']:7.3f} p99 {s['rtt_p99_ms']:8.3f}"
                        f" max {s['rtt_max_ms']:9.3f} ms  unanswered {s['unanswered']:4.0f}"
                        f"  dropped {s['dropped']:.4%}  ({record['wall_s']:.1f} s)",
                        flush=True,
                    )
    return out


def summarize_loss(path: Path) -> str:
    """Markdown tables, one per buffer: each load and engine, the mean over
    the seeds, with the range for the RTT median and the share dropped."""
    doc = json.loads(path.read_text())
    groups: dict[tuple[int, float, str], list[dict[str, float]]] = {}
    for r in doc["runs"]:
        groups.setdefault((r["buffer_bytes"], r["rho"], r["backend"]), []).append(r["summary"])
    lines = [
        f"T1, {doc['duration_ms'] / 1000:g} s per run, seeds {doc['seeds']}:"
        " means over the seeds, [min, max] in brackets."
    ]
    for buffer in sorted({b for b, _, _ in groups}, reverse=True):
        lines += [
            "",
            f"Buffer {buffer} B per port.",
            "",
            "| rho | engine | RTT median (ms) | RTT p99 (ms) | RTT max (ms) | unanswered"
            " | dropped |",
            "|---|---|---|---|---|---|---|",
        ]
        for (b, rho, engine), runs in groups.items():
            if b != buffer:
                continue

            def mean(key: str, runs: list[dict[str, float]] = runs) -> float:
                return statistics.fmean(s[key] for s in runs)

            def span(key: str, fmt: str, runs: list[dict[str, float]] = runs) -> str:
                values = [s[key] for s in runs]
                return f"[{format(min(values), fmt)}, {format(max(values), fmt)}]"

            lines.append(
                f"| {rho:.3f} | {engine}"
                f" | {mean('rtt_median_ms'):.3f} {span('rtt_median_ms', '.2f')}"
                f" | {mean('rtt_p99_ms'):.3f} | {max(s['rtt_max_ms'] for s in runs):.3f}"
                f" | {mean('unanswered'):.1f}"
                f" | {mean('dropped'):.3%} {span('dropped', '.2%')} |"
            )
    return "\n".join(lines)
