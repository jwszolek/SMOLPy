"""Ingress accounts and limits in frames on the three engines: the testbed of
ADR-0013.

Scenario T3 is the testbed as it was measured more widely: a TP-Link
LS108GP, with
- a Raspberry Pi on a 1 Gb/s port, one station that sends Poisson UDP of
  1 518 B frames to the laptop and serves the laptop's Modbus polls every
  10 ms, on its one port (a Modbus slave with a flow, ADR-0013);
- a Waveshare Modbus TCP relay module on another 1 Gb/s port, polled every
  20 ms; its responses are small, as the Pi's;
- the laptop on a 100 Mb/s port, polling both with one request in flight
  and a timeout of 1 s (ADR-0012).

rho is the UDP stream's wire bits as a share of the laptop's 100 Mb/s. Two
models of the switch:
- ``ingress``: 60 frames per ingress port, whatever their size, and no
  egress limit: what the testbed's losses point to;
- ``egress``: 91 080 B per egress port (60 frames of 1 518 B), the model of
  ADR-0011 and ADR-0012, for comparison.

At 8 100 frames/s (rho = 0.997) the testbed retransmitted 0.44 % of the Pi's
polls and none of the Waveshare's, and lost 0.66 % of the UDP frames.

The record keeps, per run and per server:
- the polls sent, and the instants skipped while a request was outstanding;
- every long round trip, over ``LONG_MS``: a response TCP retransmitted, on
  ns-3; timeouts and polls unanswered at the end, on SimPy and Rust;
- the polls affected, as a share of the polls sent;
- the round trip's median and p99 of its means over 1 ms windows;

and the share of the Pi's frames the switch dropped, ``drops_total /
frames_sent:pi``. The Pi's responses are 1.2 % of its frames, and the
Waveshare's 0.6 % of the frames through the switch, so that is its UDP
loss to within about 1 % of its value.

    python -m benchmarks ingress [--loads 0.997] [--duration 30000]
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from pathlib import Path

from benchmarks.runner import RESULTS_DIR, environment
from smolpy import Frames, Network, backends
from smolpy.model import Scenario
from smolpy.model import timebase as tb
from smolpy.model.frames import WIRE_OVERHEAD_BYTES

LAPTOP_MBPS = 100
FRAME = 1_518
INGRESS = Frames(60)
EGRESS = 60 * FRAME  # 91 080 B: the model of ADR-0011 and ADR-0012
MODELS = ("ingress", "egress")
SERVERS = {"pi": (100, 10), "waveshare": (50, 1)}  # polls/s, registers
TIMEOUT_MS = 1_000.0
LOADS = (0.96, 0.984, 0.997)
DURATION_MS = 30_000
ENGINES = ("simpy", "rust", "ns3")
SEEDS = (1, 2, 3, 4, 5)
LONG_MS = 100.0  # a round trip far over any queueing delay
# the testbed, at 8 100 frames/s (rho = 0.997) for 310 s
MEASURED = {
    "pi": "133 of 30 000 polls retransmitted (0.44 %)",
    "waveshare": "0 of 15 000 polls",
    "udp_loss": "0.66 %",
}


def udp_rate(rho: float) -> float:
    """The Pi's UDP frames per second at *rho* of the laptop's port."""
    return rho * LAPTOP_MBPS * 1e6 / ((FRAME + WIRE_OVERHEAD_BYTES) * 8)


def scenario(
    rho: float, switch: str = "ingress", duration_ms: float = DURATION_MS, seed: int = SEEDS[0]
) -> Scenario:
    """T3 with the UDP stream at *rho* and the *switch* model."""
    net = Network("T3")
    limits = {"ingress_buffer": INGRESS} if switch == "ingress" else {"buffer": EGRESS}
    sw = net.switch("sw", ports=3, **limits)
    pi = net.modbus_slave("pi", ip="10.0.0.1", unit_id=1)
    waveshare = net.modbus_slave("waveshare", ip="10.0.0.3", unit_id=1)
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=1_000, length=2)
    net.link(waveshare, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=LAPTOP_MBPS, length=2)
    pi.sends(laptop, rate=udp_rate(rho), size=FRAME, pattern="poisson")
    for server, (rate, count) in SERVERS.items():
        node = pi if server == "pi" else waveshare
        laptop.polls(node, register=0, count=count, rate=rate, outstanding=1, timeout_ms=TIMEOUT_MS)
    end = duration_ms - 1  # the last sample, at the end of the run
    for node in (pi, waveshare):
        net.observe("modbus_latency", on=node, every=1)
        for metric in ("modbus_timeouts", "modbus_unanswered", "modbus_skipped"):
            net.observe(metric, on=node, every=end)
    net.observe("frames_sent", on=pi, every=end)
    for metric in ("drops_total", "ingress_drops_total", "egress_drops_total"):
        net.observe(metric, on=sw, every=end)
    return net.to_scenario(duration=duration_ms, seed=seed)


def _instants(until_ms: float, rate: float) -> int:
    """The poll instants up to *until_ms*, which falls on none."""
    end = tb.ms_to_ns(until_ms)
    n = 0
    for t in tb.instants(0, tb.interval_ns(rate)):
        if t > end:
            return n
        n += 1
    raise AssertionError("unreachable")


def _summary(metrics: dict[str, list[tuple[float, float]]]) -> dict[str, object]:
    last = {k: (s[-1][1] if s else math.nan) for k, s in metrics.items()}
    out: dict[str, object] = {}
    for server, (rate, _) in SERVERS.items():
        rtt = [v / 1_000 for _, v in metrics[f"modbus_latency:{server}"]]  # ms
        long = sorted(r for r in rtt if r > LONG_MS)
        at, skipped = metrics[f"modbus_skipped:{server}"][-1]
        sent = _instants(at, rate) - skipped
        unanswered = last[f"modbus_unanswered:{server}"]
        affected = len(long) + unanswered  # retransmitted, timed out or unanswered
        q = statistics.quantiles(rtt, n=100) if len(rtt) > 1 else [math.nan] * 99
        out[server] = {
            "sent": sent,
            "skipped": skipped,
            "long_rtts_ms": long,
            "timeouts": last[f"modbus_timeouts:{server}"],
            "unanswered": unanswered,
            "affected": affected / sent if sent else math.nan,
            "rtt_median_ms": q[49],
            "rtt_p99_ms": q[98],
        }
    out["dropped"] = last["drops_total:sw"] / last["frames_sent:pi"]
    out["ingress_drops"] = last["ingress_drops_total:sw"]
    out["egress_drops"] = last["egress_drops_total:sw"]
    return out


def run_ingress(
    loads: list[float],
    duration_ms: float,
    engines: list[str],
    label: str = "ingress-buffers",
    out: Path | None = None,
    seeds: tuple[int, ...] = SEEDS,
    models: tuple[str, ...] = MODELS,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = {
        "format": "smolpy-ingress",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "scenario": "T3",
        "models": {"ingress": str(INGRESS), "egress": f"{EGRESS} B"},
        "duration_ms": duration_ms,
        "seeds": list(seeds),
        "measured": MEASURED,
        "runs": [],
    }
    runs: list[dict[str, object]] = []
    doc["runs"] = runs
    for model in models:
        for rho in loads:
            for seed in seeds:
                sc = scenario(rho, model, duration_ms, seed)
                for engine in engines:
                    result = backends.get(engine).run(sc)
                    s = _summary(result.metrics)
                    runs.append(
                        {
                            "model": model,
                            "rho": rho,
                            "seed": seed,
                            "backend": engine,
                            "events": result.stats.events,
                            "wall_s": result.stats.wall_s,
                            "summary": s,
                        }
                    )
                    out.write_text(json.dumps(doc, indent=1))  # keep partial progress
                    pi, ws = s["pi"], s["waveshare"]
                    assert isinstance(pi, dict) and isinstance(ws, dict)
                    print(
                        f"{model:7s} rho {rho:5.3f} seed {seed} {engine:6s}"
                        f" pi {pi['affected']:.2%} waveshare {ws['affected']:.2%}"
                        f" dropped {s['dropped']:.3%} ({runs[-1]['wall_s']:.1f} s)",
                        flush=True,
                    )
    return out


def summarize_ingress(path: Path) -> str:
    """A markdown table: each model, load and engine over the seeds."""
    doc = json.loads(path.read_text())
    groups: dict[tuple[str, float, str], list[dict]] = {}
    for r in doc["runs"]:
        groups.setdefault((r["model"], r["rho"], r["backend"]), []).append(r["summary"])
    m = doc["measured"]
    lines = [
        f"T3, {doc['duration_ms'] / 1000:g} s per run, seeds {doc['seeds']}: sums or means over"
        f" the seeds. Models: ingress {doc['models']['ingress']}, egress"
        f" {doc['models']['egress']}. The testbed at rho = 0.997: Pi {m['pi']}, Waveshare"
        f" {m['waveshare']}, UDP loss {m['udp_loss']}.",
        "",
        "| model | rho | engine | Pi: polls affected | Pi: long RTTs (ms): n, min, median, max"
        " | Waveshare: polls affected | RTT median, p99 (ms), Pi | Pi's frames dropped |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def affected(runs: list[dict], server: str) -> str:
        sent = sum(s[server]["sent"] for s in runs)
        n = sum(s[server]["affected"] * s[server]["sent"] for s in runs)
        return f"{n / sent:.2%} ({n:.0f} of {sent:.0f})"

    for (model, rho, engine), runs in groups.items():
        long = sorted(r for s in runs for r in s["pi"]["long_rtts_ms"])
        spread = (
            f"{len(long)}, {long[0]:.1f}, {statistics.median(long):.1f}, {long[-1]:.1f}"
            if long
            else "0"
        )
        lines.append(
            f"| {model} | {rho:.3f} | {engine} | {affected(runs, 'pi')} | {spread}"
            f" | {affected(runs, 'waveshare')}"
            f" | {statistics.fmean(s['pi']['rtt_median_ms'] for s in runs):.2f},"
            f" {statistics.fmean(s['pi']['rtt_p99_ms'] for s in runs):.2f}"
            f" | {statistics.fmean(s['dropped'] for s in runs):.3%} |"
        )
    return "\n".join(lines)
