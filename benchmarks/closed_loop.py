"""One Modbus request in flight on the three engines: the testbed of ADR-0012.

Scenario T2 is T1 of ``benchmarks.loss`` as the testbed measured it with
egress buffers of 60 frames (``buffer=91080``): Poisson UDP of 1 518 B
frames at rho of the laptop's 100 Mb/s port, and the laptop polling the
Modbus TCP server every 10 ms with one request in flight (``outstanding=1``),
waiting for each response.

On the hardware, 3 of 1 680 polls (0.18 %) lost their response, and TCP
brought each back after its retransmission timeout: a round trip of about
215 ms, Linux's minimum RTO of 200 ms plus the RTT. With one request in
flight no later segment follows a lost response, so no duplicate ACKs
trigger a fast retransmit.

Each load runs with several seeds, with no timeout and with a timeout of
1 s, on every engine:
- ns-3 runs Modbus over its TCP, set to Linux's (``smolpy.ns3.TcpSettings``):
  a lost response comes back after the RTO. A timeout of 1 s never fires;
- SimPy and Rust have no TCP: a lost request or response is a timeout, or,
  with none, stops the polling for the rest of the run.

The record keeps, per run:
- the polls sent, and the poll instants skipped while a request was
  outstanding;
- the round trip's median and p99 of its means over 1 ms windows;
- every long round trip, over ``LONG_MS``, in ms. Each has a window of its
  own: no other request is in flight while it lasts;
- the polls affected: long round trips, timeouts, and polls unanswered at
  the end, as a share of the polls sent;
- the share of frames the switch dropped, of the frames the Pi sent.

    python -m benchmarks closed-loop [--loads 0.99] [--duration 30000]
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from pathlib import Path

from benchmarks import loss
from benchmarks.runner import RESULTS_DIR, environment
from smolpy import backends
from smolpy.model import timebase as tb

BUFFER = 60 * loss.FRAME  # 91 080 B, as the testbed's switch was measured
LOADS = (0.985, 0.99, 0.996)
TIMEOUTS: tuple[float | None, ...] = (None, 1_000.0)
DURATION_MS = loss.DURATION_MS
ENGINES = loss.ENGINES
SEEDS = loss.SEEDS
LONG_MS = 100.0  # a round trip far over any queueing delay (at most 7.4 ms)
# the testbed, at rho = 0.985 and 0.996
MEASURED = {
    "affected": "3 of 1 680 polls (0.18 %)",
    "long_rtt_ms": "about 215",
}


def _instants(until_ms: float) -> int:
    """The poll instants up to *until_ms*, which falls on none."""
    end = tb.ms_to_ns(until_ms)
    n = 0
    for t in tb.instants(0, tb.interval_ns(loss.POLL_RATE)):
        if t > end:
            return n
        n += 1
    raise AssertionError("unreachable")


def _summary(metrics: dict[str, list[tuple[float, float]]]) -> dict[str, object]:
    rtt = [v / 1_000 for _, v in metrics["modbus_latency:laptop"]]  # ms
    last = {k: (s[-1][1] if s else math.nan) for k, s in metrics.items()}
    long = sorted(r for r in rtt if r > LONG_MS)
    timeouts = last["modbus_timeouts:laptop"]
    unanswered = last["modbus_unanswered:laptop"]
    at, skipped = metrics["modbus_skipped:laptop"][-1]
    sent = _instants(at) - skipped
    affected = len(long) + unanswered  # timed out or still unanswered at the end
    q = statistics.quantiles(rtt, n=100) if len(rtt) > 1 else [math.nan] * 99
    return {
        "sent": sent,
        "skipped": skipped,
        "rtt_median_ms": q[49],
        "rtt_p99_ms": q[98],
        "long_rtts_ms": long,
        "timeouts": timeouts,
        "unanswered": unanswered,
        "affected": affected / sent if sent else math.nan,
        "dropped": last["drops_total:sw"] / last["frames_sent:pi"],
    }


def run_closed_loop(
    loads: list[float],
    duration_ms: float,
    engines: list[str],
    label: str = "one-request-in-flight",
    out: Path | None = None,
    seeds: tuple[int, ...] = SEEDS,
    timeouts: tuple[float | None, ...] = TIMEOUTS,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = {
        "format": "smolpy-closed-loop",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "scenario": "T2",
        "buffer_bytes": BUFFER,
        "duration_ms": duration_ms,
        "seeds": list(seeds),
        "measured": MEASURED,
        "runs": [],
    }
    runs: list[dict[str, object]] = []
    doc["runs"] = runs
    for timeout in timeouts:
        for rho in loads:
            for seed in seeds:
                sc = loss.scenario(
                    rho, duration_ms, BUFFER, seed, outstanding=1, timeout_ms=timeout
                )
                for engine in engines:
                    result = backends.get(engine).run(sc)
                    s = _summary(result.metrics)
                    runs.append(
                        {
                            "timeout_ms": timeout,
                            "rho": rho,
                            "seed": seed,
                            "backend": engine,
                            "events": result.stats.events,
                            "wall_s": result.stats.wall_s,
                            "summary": s,
                        }
                    )
                    out.write_text(json.dumps(doc, indent=1))  # keep partial progress
                    long = s["long_rtts_ms"]
                    assert isinstance(long, list)
                    print(
                        f"timeout {timeout} rho {rho:5.3f} seed {seed} {engine:6s}"
                        f" sent {s['sent']:5.0f} skipped {s['skipped']:5.0f}"
                        f" long {[round(r) for r in long]} timeouts {s['timeouts']:3.0f}"
                        f" unanswered {s['unanswered']:2.0f} affected {s['affected']:.2%}"
                        f" dropped {s['dropped']:.3%} ({runs[-1]['wall_s']:.1f} s)",
                        flush=True,
                    )
    return out


def summarize_closed_loop(path: Path) -> str:
    """Markdown tables, one per timeout: each load and engine over the seeds."""
    doc = json.loads(path.read_text())
    groups: dict[tuple[float | None, float, str], list[dict]] = {}
    for r in doc["runs"]:
        groups.setdefault((r["timeout_ms"], r["rho"], r["backend"]), []).append(r["summary"])
    lines = [
        f"T2, buffer {doc['buffer_bytes']} B, {doc['duration_ms'] / 1000:g} s per run,"
        f" seeds {doc['seeds']}: sums or means over the seeds. The testbed:"
        f" {doc['measured']['affected']} affected, long RTTs {doc['measured']['long_rtt_ms']} ms."
    ]
    for timeout in dict.fromkeys(t for t, _, _ in groups):
        lines += [
            "",
            f"Timeout: {'none' if timeout is None else f'{timeout:g} ms'}.",
            "",
            "| rho | engine | polls sent | skipped | RTT median (ms) | RTT p99 (ms)"
            " | long RTTs (ms): n, min, median, max | timeouts | unanswered | affected"
            " | dropped |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for (t, rho, engine), runs in groups.items():
            if t != timeout:
                continue
            long = sorted(r for s in runs for r in s["long_rtts_ms"])
            spread = (
                f"{len(long)}, {long[0]:.1f}, {statistics.median(long):.1f}, {long[-1]:.1f}"
                if long
                else "0"
            )
            sent = sum(s["sent"] for s in runs)
            affected = sum(s["affected"] * s["sent"] for s in runs)
            lines.append(
                f"| {rho:.3f} | {engine} | {sent:.0f} | {sum(s['skipped'] for s in runs):.0f}"
                f" | {statistics.fmean(s['rtt_median_ms'] for s in runs):.3f}"
                f" | {statistics.fmean(s['rtt_p99_ms'] for s in runs):.3f}"
                f" | {spread} | {sum(s['timeouts'] for s in runs):.0f}"
                f" | {sum(s['unanswered'] for s in runs):.0f}"
                f" | {affected / sent:.2%} ({affected:.0f})"
                f" | {statistics.fmean(s['dropped'] for s in runs):.3%} |"
            )
    return "\n".join(lines)
