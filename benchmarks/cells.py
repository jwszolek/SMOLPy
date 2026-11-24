"""Buffer limits in cells with a resume threshold on the three engines: the
testbed of ADR-0014.

Scenario T4 is T3 of ``benchmarks.ingress``, the testbed as measured: the Pi
is one Modbus slave on a 1 Gb/s port that sends Poisson UDP of 1 518 B
frames to the laptop and serves its polls every 10 ms, and a Waveshare relay
module on another port is polled every 20 ms, both with one request in
flight and a timeout of 1 s. The switch models:
- ``cells``: 740 cells of 128 B per ingress port, resuming at 668, the
  calibration of ADR-0014;
- ``cells-tail``: the same 740 cells, tail drop: no resume level;
- ``frames``: 60 frames per ingress port, the model of ADR-0013.

Three parts:
- **the testbed** at 8 100 frames/s: the UDP loss, the mean loss run
  (ingress drops over drop episodes), the Pi's and the Waveshare's polls
  affected; and on SimPy, by an audit of its decisions, each run's length;
- **the burst test**: N frames back to back from the Pi at the pace it put
  them on the wire, its measured send rate or the 1 Gb/s line rate if lower,
  for each row of the testbed's burst CSVs; the frames received are N less
  the frames dropped among the first N;
- **the batch case**: Poisson groups of 8 back-to-back 1 518 B frames at
  7 500 frames/s; the Pi's polls affected over its UDP loss.

The record keeps every run and burst; ``summarize_cells`` prints the tables.

    python -m benchmarks cells [--duration 30000] [--backend simpy rust ns3]
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import json
import math
import statistics
from collections.abc import Iterator
from pathlib import Path

from benchmarks.runner import RESULTS_DIR, environment
from smolpy import Cells, Frames, Network, backends
from smolpy.model import Scenario
from smolpy.model import timebase as tb
from smolpy.model.frames import WIRE_OVERHEAD_BYTES

LAPTOP_MBPS = 100
PI_MBPS = 1_000
FRAME = 1_518
CELL = 128
LIMIT, RESUME = 740, 668
MODELS = {
    "cells": {"cell": CELL, "ingress_buffer": Cells(LIMIT), "ingress_resume": Cells(RESUME)},
    "cells-tail": {"cell": CELL, "ingress_buffer": Cells(LIMIT)},
    "frames": {"ingress_buffer": Frames(60)},
}
SERVERS = {"pi": (100, 10), "waveshare": (50, 1)}  # polls/s, registers
TIMEOUT_MS = 1_000.0
RATE = 8_100  # UDP frames/s, the testbed's
BATCH = (8, 7_500)  # group size and UDP frames/s of the batch case
DURATION_MS = 30_000
ENGINES = ("simpy", "rust", "ns3")
SEEDS = (1, 2, 3, 4, 5)
LONG_MS = 100.0  # a round trip far over any queueing delay
# the testbed: the paper's repository, stanowisko/pomiary/wyniki/
DATA = Path.home() / (
    "Documents/publications/smolpy-heterogeneous-networks/SMOLPy_TII_dla_ZK/stanowisko/pomiary/wyniki"
)
BURSTS = (
    "2026-11-23_bufor_paczki.csv",
    "2026-11-24_bufor_paczki_512B.csv",
    "2026-11-24_bufor_paczki_64B.csv",
)
MEASURED = {
    "udp_loss": "0.55-0.66 %",
    "mean_run": "6.4-6.7 frames, from 2 to 15, no single-frame run",
    "pi": "0.44 % of polls retransmitted",
    "waveshare": "0 of 15 000 polls",
    "batch_ratio": "0.55 (K = 8, 7 500 frames/s)",
}


def scenario(
    model: str,
    rate: float = RATE,
    duration_ms: float = DURATION_MS,
    seed: int = SEEDS[0],
    batch: int | None = None,
) -> Scenario:
    """T4: the testbed with the switch *model*, at *rate* UDP frames/s, in
    Poisson groups of *batch* frames if given."""
    net = Network("T4")
    sw = net.switch("sw", ports=3, **MODELS[model])
    pi = net.modbus_slave("pi", ip="10.0.0.1", unit_id=1)
    waveshare = net.modbus_slave("waveshare", ip="10.0.0.3", unit_id=1)
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=PI_MBPS, length=2)
    net.link(waveshare, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=LAPTOP_MBPS, length=2)
    if batch is None:
        pi.sends(laptop, rate=rate, size=FRAME, pattern="poisson")
    else:
        pi.sends(laptop, rate=rate, size=FRAME, pattern="batch", batch=batch)
    for server, (poll_rate, count) in SERVERS.items():
        node = pi if server == "pi" else waveshare
        laptop.polls(
            node, register=0, count=count, rate=poll_rate, outstanding=1, timeout_ms=TIMEOUT_MS
        )
    end = duration_ms - 1  # the last sample, at the end of the run
    for node in (pi, waveshare):
        net.observe("modbus_latency", on=node, every=1)
        for metric in ("modbus_unanswered", "modbus_skipped"):
            net.observe(metric, on=node, every=end)
    net.observe("frames_sent", on=pi, every=end)
    for metric in ("ingress_drops_total", "ingress_drop_episodes_total", "ingress_drop_time_total"):
        net.observe(metric, on=sw, every=end)
    return net.to_scenario(duration=duration_ms, seed=seed)


def _instants(until_ms: float, rate: float) -> int:
    end = tb.ms_to_ns(until_ms)
    n = 0
    for t in tb.instants(0, tb.interval_ns(rate)):
        if t > end:
            return n
        n += 1
    raise AssertionError("unreachable")


@contextlib.contextmanager
def _runs() -> Iterator[list[int]]:
    """The length of each run of drops by SimPy's accounts, by an audit of
    their decisions (ADR-0014): no metric gives each run's length."""
    from smolpy.sim import engine

    lengths: list[int] = []
    running: dict[int, int] = {}
    admit = engine._Account.admit

    def counting(account: engine._Account, size: int) -> bool:
        ok = admit(account, size)
        if ok:
            if id(account) in running:
                lengths.append(running.pop(id(account)))
        else:
            running[id(account)] = running.get(id(account), 0) + 1
        return ok

    engine._Account.admit = counting  # type: ignore[method-assign]
    try:
        yield lengths
    finally:
        engine._Account.admit = admit  # type: ignore[method-assign]


def _summary(metrics: dict[str, list[tuple[float, float]]]) -> dict[str, object]:
    last = {k: (s[-1][1] if s else math.nan) for k, s in metrics.items()}
    out: dict[str, object] = {}
    for server, (rate, _) in SERVERS.items():
        rtt = [v / 1_000 for _, v in metrics[f"modbus_latency:{server}"]]
        long = sum(r > LONG_MS for r in rtt)
        at, skipped = metrics[f"modbus_skipped:{server}"][-1]
        sent = _instants(at, rate) - skipped
        affected = long + last[f"modbus_unanswered:{server}"]
        out[server] = {"sent": sent, "affected": affected}
    drops = last["ingress_drops_total:sw"]
    out["dropped"] = drops / last["frames_sent:pi"]
    out["drops"] = drops
    out["episodes"] = last["ingress_drop_episodes_total:sw"]
    out["drop_time_ms"] = last["ingress_drop_time_total:sw"]
    return out


# --- the burst test ---


def burst_rows(data: Path = DATA) -> list[dict[str, float]]:
    """Every row of the burst CSVs: frame size, N, received and the send rate."""
    rows = []
    for name in BURSTS:
        with open(data / name, newline="") as f:
            for r in csv.DictReader(f):
                gen = r["gen"].split("=")[1].split()[0]  # "... = 119166 ramek/s; ..."
                rows.append(
                    {
                        "frame_bytes": int(r.get("frame_B") or FRAME),
                        "n": int(r["N"]),
                        "received": int(r["received"]),
                        "send_rate": float(gen),
                    }
                )
    return rows


def burst(model: str, frame_bytes: int, n: int, send_rate: float, engine: str = "simpy") -> int:
    """The frames the laptop receives of N sent back to back at the pace the
    Pi put them on the wire: its send rate, or the line rate if lower."""
    line = PI_MBPS * 1e6 / ((frame_bytes + WIRE_OVERHEAD_BYTES) * 8)
    rate = min(send_rate, line)
    period = tb.interval_ns(rate)
    # the N-th frame is sent at N T and decided after its transmission and
    # propagation; the sample falls half a period later, before the next
    tx = tb.transmission_ns(8 + frame_bytes, PI_MBPS)
    at = tb.round_ns(n * period + tx + tb.propagation_ns(2) + period / 2)
    net = Network("burst")
    sw = net.switch("sw", ports=2, mac_table="topology", **MODELS[model])
    pi = net.adapter("pi", ip="10.0.0.1")
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=PI_MBPS, length=2)
    net.link(laptop, sw, speed=LAPTOP_MBPS, length=2)
    pi.sends(laptop, rate=rate, size=frame_bytes, pattern="constant")
    net.observe("ingress_drops_total", on=sw, every=tb.ns_to_ms(at))
    sc = net.to_scenario(duration=tb.ns_to_ms(at) + 0.001)
    (_, dropped), *_ = backends.get(engine).run(sc).metrics["ingress_drops_total:sw"]
    return n - int(dropped)


def run_cells(
    duration_ms: float,
    engines: list[str],
    label: str = "buffer-cells",
    out: Path | None = None,
    seeds: tuple[int, ...] = SEEDS,
    models: tuple[str, ...] = tuple(MODELS),
    data: Path = DATA,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = {
        "format": "smolpy-cells",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "scenario": "T4",
        "models": {k: {a: str(v) for a, v in m.items()} for k, m in MODELS.items()},
        "duration_ms": duration_ms,
        "seeds": list(seeds),
        "measured": MEASURED,
        "runs": [],
        "bursts": [],
    }
    runs: list[dict[str, object]] = []
    doc["runs"] = runs
    for case, rate, batch in (("testbed", RATE, None), ("batch", BATCH[1], BATCH[0])):
        for model in models:
            for seed in seeds:
                sc = scenario(model, rate, duration_ms, seed, batch)
                for engine in engines:
                    lengths: list[int] = []
                    if engine == "simpy":
                        with _runs() as lengths:
                            result = backends.get(engine).run(sc)
                    else:
                        result = backends.get(engine).run(sc)
                    s = _summary(result.metrics)
                    if engine == "simpy":  # length -> runs
                        s["run_lengths"] = {str(n): lengths.count(n) for n in sorted(set(lengths))}
                    runs.append(
                        {
                            "case": case,
                            "model": model,
                            "seed": seed,
                            "backend": engine,
                            "events": result.stats.events,
                            "wall_s": result.stats.wall_s,
                            "summary": s,
                        }
                    )
                    out.write_text(json.dumps(doc, indent=1))  # keep partial progress
                    pi = s["pi"]
                    assert isinstance(pi, dict)
                    print(
                        f"{case:7s} {model:10s} seed {seed} {engine:6s}"
                        f" dropped {s['dropped']:.3%} run {s['drops'] / max(s['episodes'], 1):.2f}"
                        f" pi {pi['affected'] / pi['sent']:.2%} ({runs[-1]['wall_s']:.1f} s)",
                        flush=True,
                    )
    bursts: list[dict[str, object]] = []
    doc["bursts"] = bursts
    if data.is_dir():
        for row in burst_rows(data):
            got = {
                model: burst(model, int(row["frame_bytes"]), int(row["n"]), row["send_rate"])
                for model in models
            }
            bursts.append({**row, "model": got})
        out.write_text(json.dumps(doc, indent=1))
    return out


def _held(frame_bytes: int, n: int, received: float, send_rate: float) -> float:
    """What the switch held: the frames received less those its 100 Mb/s port
    drained while the burst lasted on the wire."""
    line = PI_MBPS * 1e6 / ((frame_bytes + WIRE_OVERHEAD_BYTES) * 8)
    seconds = n / min(send_rate, line)
    drained = seconds * LAPTOP_MBPS * 1e6 / ((frame_bytes + WIRE_OVERHEAD_BYTES) * 8)
    return received - drained


def summarize_cells(path: Path) -> str:
    doc = json.loads(path.read_text())
    m = doc["measured"]
    lines = [
        f"T4, {doc['duration_ms'] / 1000:g} s per run, seeds {doc['seeds']}: sums or means over"
        f" the seeds. The testbed: UDP loss {m['udp_loss']}; runs {m['mean_run']}; Pi {m['pi']};"
        f" Waveshare {m['waveshare']}; batch ratio {m['batch_ratio']}.",
        "",
        "| case | model | engine | UDP loss | mean run | single-frame runs | Pi: polls affected"
        " | Waveshare | Pi ratio to UDP loss |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in doc["runs"]:
        groups.setdefault((r["case"], r["model"], r["backend"]), []).append(r["summary"])
    for (case, model, engine), runs in groups.items():
        sent = sum(s["pi"]["sent"] for s in runs)
        pi = sum(s["pi"]["affected"] for s in runs) / sent
        ws = sum(s["waveshare"]["affected"] for s in runs)
        loss = statistics.fmean(s["dropped"] for s in runs)
        run = sum(s["drops"] for s in runs) / max(sum(s["episodes"] for s in runs), 1)
        hist: dict[str, int] = {}
        for s in runs:
            for n, k in s.get("run_lengths", {}).items():
                hist[n] = hist.get(n, 0) + k
        total = sum(hist.values())
        single = f"{hist.get('1', 0) / total:.1%} of {total}" if total else "–"
        lines.append(
            f"| {case} | {model} | {engine} | {loss:.3%} | {run:.2f} | {single} | {pi:.2%}"
            f" | {ws:.0f} | {pi / loss:.2f} |"
        )
    if doc["bursts"]:
        models = list(doc["bursts"][0]["model"])
        lines += [
            "",
            "Burst test: frames received of N, and what the switch held"
            " (received less what drained during the burst).",
            "",
            "| frame (B) | N | send rate (frames/s) | received | "
            + " | ".join(f"{m}: received" for m in models)
            + " | held, measured | "
            + " | ".join(f"held, {m}" for m in models)
            + " |",
            "|---" * (5 + 2 * len(models)) + "|",
        ]
        for b in doc["bursts"]:
            held = _held(b["frame_bytes"], b["n"], b["received"], b["send_rate"])
            model_held = [
                _held(b["frame_bytes"], b["n"], b["model"][m], b["send_rate"]) for m in models
            ]
            lines.append(
                f"| {b['frame_bytes']} | {b['n']} | {b['send_rate']:.0f} | {b['received']} | "
                + " | ".join(str(b["model"][m]) for m in models)
                + f" | {held:.0f} | "
                + " | ".join(f"{h:.0f}" for h in model_held)
                + " |"
            )
    return "\n".join(lines)
