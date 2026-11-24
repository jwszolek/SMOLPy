"""A shared switch buffer pool, a reserve per ingress port and a drop state
ended by a drain, on the testbed of ADR-0015: calibration on one set of
measurements, validation on another.

The LS108GP as ADR-0015 models it: ``cell=128 pool=P ingress_reserve=R
ingress_drain=D``, in cells.

**Calibration**, on the single-source measurements and the reserve test:
1. ``L = P + R`` and ``D``. With one source the pool and reserve are an
   ingress limit of L, so they decide alone:
   - the burst tests (``2026-11-23_bufor_paczki.csv``,
     ``2026-11-24_bufor_paczki_{512B,64B}.csv``): N frames back to back at
     the pace the Pi put them on the wire;
   - the single-source loss runs (``2026-11-24_serie_strat/``): loss, mean
     run and single-frame share at 8 100 frames/s.
2. ``R``, with L and D fixed: the reserve test with a constant 9 000 frames/s
   background from another port (``2026-11-24_rezerwa/``). Each repetition
   starts its burst at another phase of the background's cycle of refill
   and drain, about 7.7 ms, as the hardware's repetitions did.

**Validation**, with the fitted values and no refitting: two Pis at
2 x 4 050 and 2 x 3 900 frames/s, 10 Mb/s at 808 and 846 frames/s, 512 B and
mixed sizes, the ADR-0013 ingress test at 8 100 frames/s, and the batch case
(K = 8 at 7 500 frames/s).

The hardware's numbers are computed from the raw files by this module:
losses and runs from the ``.seq`` files, round trips from the ``.csv``
files. Each run's length, the overlap of two ports' drop states and the
loss by frame size come from an audit of SimPy's decisions.

    python -m benchmarks pool [--duration 60000] [--backend simpy rust ns3]
"""

from __future__ import annotations

import array
import contextlib
import csv
import datetime as dt
import json
import math
import statistics
from collections.abc import Iterator
from pathlib import Path

from benchmarks import cells
from benchmarks.runner import RESULTS_DIR, environment
from smolpy import Cells, Network, backends
from smolpy.model import Scenario
from smolpy.model import timebase as tb
from smolpy.model.frames import WIRE_OVERHEAD_BYTES

DATA = cells.DATA
CELL = 128
FRAME = 1_518
FIRST = (710, 30, 72)  # P, R, D in cells: ADR-0015's first estimates
L_GRID = tuple(range(730, 801, 10))
D_GRID = (36, 48, 60, 72, 84, 96)
R_GRID = (24, 30, 36, 42, 48, 54, 60, 72)
# the pace of Pi 2's 64 B bursts in the reserve test was not recorded: the
# burst test saw 63 000 to 340 000 frames/s, so it is fitted with R
RATE64_GRID = (110_000, 130_000, 150_000, 180_000, 230_000)
# the background's cycle, refill and drain, is about 7.7 ms: ten phases
# spread over a few cycles, at no common period
PHASES_MS = tuple(k * 1.913 for k in range(10))
ENGINES = ("simpy", "rust", "ns3")
SEEDS = (1, 2, 3)
LONG_MS = 100.0


def switch(p: int, r: int, d: int) -> dict:
    """The LS108GP's switch parameters; a reserve of 0 is none."""
    kw = {"cell": CELL, "pool": Cells(p), "ingress_drain": Cells(d)}
    if r:
        kw["ingress_reserve"] = Cells(r)
    return kw


# --- the hardware, from the raw files ---


def _read(path: Path) -> array.array:
    a = array.array("Q")
    a.frombytes(path.read_bytes())
    return a


def seq_stats(name: str, data: Path = DATA) -> dict[str, float]:
    """Loss, runs and single-frame share of a stream, from its received
    sequence numbers: a run is consecutive missing numbers."""
    s = _read(data / f"{name}.seq")
    sent = s[-1] + 1
    runs, prev = [], -1
    for x in s:
        if x - prev > 1:
            runs.append(x - prev - 1)
        prev = x
    return {
        "loss": sum(runs) / sent,
        "mean_run": statistics.mean(runs) if runs else 0.0,
        "single": sum(r == 1 for r in runs) / len(runs) if runs else 0.0,
        "runs": len(runs),
    }


def rtt_stats(name: str, data: Path = DATA, long_ms: float = LONG_MS) -> dict[str, float]:
    """Polls sent and retransmitted (a round trip of *long_ms* or more), and
    the p99 and maximum of the others, in ms."""
    with open(data / name, newline="") as f:
        v = [float(r["rtt_us"]) / 1_000 for r in csv.DictReader(f)]
    short = sorted(x for x in v if x < long_ms)
    q = statistics.quantiles(short, n=100)
    long = sum(x >= long_ms for x in v)
    return {"sent": len(v), "affected": long / len(v), "p99": q[98], "max": short[-1]}


def hardware(data: Path = DATA, long_ms: float = LONG_MS) -> dict[str, dict]:
    two = "2026-11-24_dwa_rpi/powtorka"
    adr14 = "2026-11-24_prognoza_adr0014"
    out = {
        "single 8100": {
            "udp": [
                seq_stats(f"2026-11-24_serie_strat/{n}", data)
                for n in ("r8100_1518", "r8100_1518_tx")
            ],
        },
        "two 4050": {
            "udp": [seq_stats(f"{two}/r4050_{p}", data) for p in ("pi1", "pi2")],
            "pi": [rtt_stats(f"{two}/r4050_{p}.csv", data, long_ms) for p in ("pi1", "pi2")],
            "waveshare": rtt_stats(f"{two}/r4050_ws.csv", data, long_ms),
            # ADR-0015's "0 of 2 072" merged two Pis' unaligned clocks; with
            # the clocks aligned from the losses the overlap is only bounded,
            # about 18 % of runs at ±1 ms (ADR-0016)
            "overlap": "not measurable: no receive times; about 18 % of runs with clocks"
            " aligned to ±1 ms (ADR-0016)",
        },
        "two 3900": {
            "udp": [seq_stats(f"{two}/r3900_{p}", data) for p in ("pi1", "pi2")],
            "pi": [rtt_stats(f"{two}/r3900_{p}.csv", data, long_ms) for p in ("pi1", "pi2")],
            "waveshare": rtt_stats(f"{two}/r3900_ws.csv", data, long_ms),
        },
        "10Mb 808": {
            "udp": [seq_stats("2026-11-24_10Mb/r809", data)],
            "pi": [rtt_stats("2026-11-24_10Mb/r809_pi1.csv", data, long_ms)],
        },
        "10Mb 846": {
            "udp": [seq_stats("2026-11-24_10Mb/r850", data)],
            "pi": [rtt_stats("2026-11-24_10Mb/r850_pi1.csv", data, long_ms)],
        },
        "512 B": {
            "udp": [seq_stats(f"{adr14}/s512", data)],
            "pi": [rtt_stats(f"{adr14}/s512_rpi.csv", data, long_ms)],
            "waveshare": rtt_stats(f"{adr14}/s512_ws.csv", data, long_ms),
        },
        "mixed": {
            "udp": [seq_stats(f"{adr14}/mix1518", data), seq_stats(f"{adr14}/mix512", data)],
            "pi": [rtt_stats(f"{adr14}/mix_rpi.csv", data, long_ms)],
            "waveshare": rtt_stats(f"{adr14}/mix_ws.csv", data, long_ms),
        },
        "ingress 8100": {
            "udp_loss": 16_630 / 2_510_616,  # sink.txt
            "pi": [rtt_stats("2026-11-24_ingress_test/rpi.csv", data, long_ms)],
            "waveshare": rtt_stats("2026-11-24_ingress_test/waveshare.csv", data, long_ms),
        },
        "batch": {"ratio": 0.55},
    }
    return out


# --- the audit ---


@contextlib.contextmanager
def audit() -> Iterator[dict]:
    """SimPy's ingress decisions, by port: the runs of drops, the drops by
    frame size, and the drop-state intervals."""
    from smolpy.sim import engine

    found: dict = {"runs": {}, "drops_by_size": {}, "decided_by_size": {}, "intervals": {}}
    running: dict[int, int] = {}
    entered: dict[int, int] = {}
    admit, leave = engine._Account.admit, engine._Account.leave

    def port_of(account) -> int:
        if account.switch is None:
            return -1
        return next(p for p, a in account.switch.accounts.items() if a is account)

    def counting(account, size: int) -> bool:
        dropping = account.dropping
        ok = admit(account, size)
        if account.switch is None:
            return ok
        port = port_of(account)
        key = (port, size)
        found["decided_by_size"][key] = found["decided_by_size"].get(key, 0) + 1
        if ok:
            if port in running:
                found["runs"].setdefault(port, []).append(running.pop(port))
        else:
            running[port] = running.get(port, 0) + 1
            found["drops_by_size"][key] = found["drops_by_size"].get(key, 0) + 1
        if not dropping and account.dropping:
            entered[port] = account.env._now
        return ok

    def leaving(account) -> None:
        if account.switch is not None:
            port = port_of(account)
            found["intervals"].setdefault(port, []).append(
                (entered.pop(port, account.env._now), account.env._now)
            )
        leave(account)

    engine._Account.admit = counting  # type: ignore[method-assign]
    engine._Account.leave = leaving  # type: ignore[method-assign]
    try:
        yield found
    finally:
        engine._Account.admit = admit  # type: ignore[method-assign]
        engine._Account.leave = leave  # type: ignore[method-assign]


def overlaps(intervals: dict[int, list[tuple[int, int]]]) -> tuple[int, int]:
    """The drop states of ports 0 and 1 that overlap one of the other port's,
    and their number."""
    a, b = intervals.get(0, []), intervals.get(1, [])
    hit = sum(1 for s, e in a if any(s < e2 and s2 < e for s2, e2 in b))
    hit += sum(1 for s, e in b if any(s < e2 and s2 < e for s2, e2 in a))
    return hit, len(a) + len(b)


# --- calibration ---


def burst(kw: dict, frame_bytes: int, n: int, send_rate: float, engine: str = "rust") -> int:
    """The frames received of N sent back to back at the Pi's pace, with
    the switch *kw* (as ``benchmarks.cells.burst``)."""
    line = 1e9 / ((frame_bytes + WIRE_OVERHEAD_BYTES) * 8)
    rate = min(send_rate, line)
    period = tb.interval_ns(rate)
    tx = tb.transmission_ns(8 + frame_bytes, 1_000)
    at = tb.round_ns(n * period + tx + tb.propagation_ns(2) + period / 2)
    net = Network("burst")
    sw = net.switch("sw", ports=2, mac_table="topology", **kw)
    pi = net.adapter("pi", ip="10.0.0.1")
    laptop = net.adapter("laptop", ip="10.0.0.2")
    net.link(pi, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=100, length=2)
    pi.sends(laptop, rate=rate, size=frame_bytes, pattern="constant")
    net.observe("ingress_drops_total", on=sw, every=tb.ns_to_ms(at))
    sc = net.to_scenario(duration=tb.ns_to_ms(at) + 0.001)
    (_, dropped), *_ = backends.get(engine).run(sc).metrics["ingress_drops_total:sw"]
    return n - int(dropped)


def loss_runs(kw: dict, seeds=(1, 2), duration_ms: float = 30_000, engine: str = "rust") -> dict:
    """Loss and mean run of one Pi at 8 100 frames/s, the serie_strat runs."""
    loss, runs = [], []
    for seed in seeds:
        net = Network("runs")
        sw = net.switch("sw", ports=2, **kw)
        pi = net.adapter("pi", ip="10.0.0.1")
        laptop = net.adapter("laptop", ip="10.0.0.2")
        net.link(pi, sw, speed=1_000, length=2)
        net.link(laptop, sw, speed=100, length=2)
        pi.sends(laptop, rate=8_100, size=FRAME, pattern="poisson")
        end = duration_ms - 1
        for m in ("ingress_drops_total", "ingress_drop_episodes_total"):
            net.observe(m, on=sw, every=end)
        net.observe("frames_sent", on=pi, every=end)
        m = backends.get(engine).run(net.to_scenario(duration=duration_ms, seed=seed)).metrics
        drops = m["ingress_drops_total:sw"][-1][1]
        loss.append(drops / m["frames_sent:pi"][-1][1])
        runs.append(drops / max(m["ingress_drop_episodes_total:sw"][-1][1], 1))
    return {"loss": statistics.mean(loss), "mean_run": statistics.mean(runs)}


RESERVE_RATES = {1_518: 1e9 / ((1_518 + WIRE_OVERHEAD_BYTES) * 8), 64: 230_000.0}


def reserve_rows(data: Path = DATA) -> list[dict]:
    rows = []
    with open(data / "2026-11-24_rezerwa/rezerwa_tlo9000_const.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({k: int(v) if v.isdigit() else v for k, v in r.items()})
    # the last three repetitions of N = 400 ran after the background had ended
    return [r for r in rows if not (r["N"] == 400 and r["rep"] >= 7)]


def reserve(kw: dict, frame_bytes: int, n: int, phase_ms: float) -> int:
    """The frames of Pi 2's burst of N that arrive, while Pi 1 holds the
    laptop's port full with a constant 9 000 frames/s: SimPy, by an audit of
    Pi 2's port's decisions on its first N frames."""
    from smolpy.sim import engine

    start = 100.0 + phase_ms
    rate = RESERVE_RATES[frame_bytes]
    net = Network("reserve")
    sw = net.switch("sw", ports=3, mac_table="topology", **kw)
    pi1 = net.adapter("pi1", ip="10.0.0.1")
    pi2 = net.adapter("pi2", ip="10.0.0.2")
    laptop = net.adapter("laptop", ip="10.0.0.3")
    net.link(pi1, sw, speed=1_000, length=2)
    net.link(pi2, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=100, length=2)
    pi1.sends(laptop, rate=9_000, size=FRAME, pattern="constant")
    # the burst: a constant flow at its pace from start; its first N frames
    pi2.sends(laptop, rate=rate, size=frame_bytes, pattern="constant", delay_ms=start)
    admitted = [0, 0]  # of Pi 2's port: decided, admitted
    admit = engine._Account.admit

    def counting(account, size: int) -> bool:
        ok = admit(account, size)
        sw_ = account.switch
        if sw_ is not None and sw_.accounts.get(1) is account and admitted[0] < n:
            admitted[0] += 1
            admitted[1] += ok
        return ok

    engine._Account.admit = counting  # type: ignore[method-assign]
    try:
        end = start + n / rate * 1_000 + 1
        net.simulate(duration=end)
    finally:
        engine._Account.admit = admit  # type: ignore[method-assign]
    return admitted[1]


def calibrate(data: Path = DATA, log=print) -> dict:
    """The grid search: L and D on the single-source set, then R."""
    bursts = [r for r in cells.burst_rows(data)]
    hw_runs = [
        seq_stats(f"2026-11-24_serie_strat/{n}", data) for n in ("r8100_1518", "r8100_1518_tx")
    ]
    want_loss = statistics.mean(h["loss"] for h in hw_runs)
    want_run = statistics.mean(h["mean_run"] for h in hw_runs)
    stage1 = []
    for total in L_GRID:
        for d in D_GRID:
            kw = switch(total, 0, d)  # one source: P + R decides alone
            err = []
            for row in bursts:
                got = burst(kw, row["frame_bytes"], row["n"], row["send_rate"])
                sigma = max(2.0, 0.02 * row["received"])
                err.append(((got - row["received"]) / sigma) ** 2)
            lr = loss_runs(kw)
            e_burst = statistics.mean(err)
            e_runs = ((lr["loss"] - want_loss) / (0.05 * want_loss)) ** 2 + (
                (lr["mean_run"] - want_run) / 0.15
            ) ** 2
            stage1.append(
                {"L": total, "D": d, "burst": e_burst, "runs": e_runs, "loss": lr["loss"],
                 "mean_run": lr["mean_run"], "score": e_burst + e_runs}
            )  # fmt: skip
            log(f"L {total} D {d}: burst {e_burst:.2f} runs {e_runs:.2f}"
                f" (loss {lr['loss']:.3%}, run {lr['mean_run']:.2f})")  # fmt: skip
    best = min(stage1, key=lambda s: s["score"])
    rows = reserve_rows(data)
    groups: dict[tuple[int, int], list[int]] = {}
    for r in rows:
        groups.setdefault((r["frame_B"], r["N"]), []).append(r["received"])
    stage2 = []
    for rate64 in RATE64_GRID:
        RESERVE_RATES[64] = rate64
        for r in R_GRID:
            kw = switch(best["L"] - r, r, best["D"])
            err, model = [], {}
            for (size, n), measured in groups.items():
                got = [reserve(kw, size, n, phase) for phase in PHASES_MS]
                model[f"{size} {n}"] = got
                sigma = max(1.0, (max(measured) - min(measured)) / 2)
                err.append(((statistics.median(got) - statistics.median(measured)) / sigma) ** 2)
            stage2.append({"R": r, "rate64": rate64, "score": statistics.mean(err), "model": model})
            log(f"R {r} at {rate64} frames/s: reserve {statistics.mean(err):.2f}")
    r_best = min(stage2, key=lambda s: s["score"])
    RESERVE_RATES[64] = r_best["rate64"]
    return {
        "stage1": stage1,
        "stage2": stage2,
        "measured_reserve": {f"{s} {n}": v for (s, n), v in groups.items()},
        "fitted": {"P": best["L"] - r_best["R"], "R": r_best["R"], "D": best["D"]},
        "rate64": r_best["rate64"],
    }


# --- validation ---


def testbed(
    kw: dict,
    pis: list[list[tuple[float, int, str]]],
    duration_ms: float,
    seed: int,
    laptop_mbps: float = 100,
    poll_ms: float = 10,
    registers: int = 10,
    waveshare: bool = True,
) -> Scenario:
    """The testbed: each Pi a Modbus slave with its flows (rate, size,
    pattern), the Waveshare, and the laptop polling them."""
    net = Network("LS108GP")
    sw = net.switch("sw", ports=2 + len(pis), **kw)
    nodes = [
        net.modbus_slave(f"pi{i + 1}", ip=f"10.0.0.{i + 1}", unit_id=1) for i in range(len(pis))
    ]
    ws = net.modbus_slave("waveshare", ip="10.0.0.9", unit_id=1) if waveshare else None
    laptop = net.adapter("laptop", ip="10.0.0.100")
    for node in nodes:
        net.link(node, sw, speed=1_000, length=2)
    if ws is not None:
        net.link(ws, sw, speed=1_000, length=2)
    net.link(laptop, sw, speed=laptop_mbps, length=2)
    for node, flows in zip(nodes, pis, strict=True):
        for rate, size, pattern in flows:
            if pattern.startswith("batch"):
                node.sends(laptop, rate=rate, size=size, pattern="batch", batch=int(pattern[5:]))
            else:
                node.sends(laptop, rate=rate, size=size, pattern=pattern)
    servers = [(n, 1000 / poll_ms, registers) for n in nodes]
    if ws is not None:
        servers.append((ws, 50, 1))
    end = duration_ms - 1
    for node, rate, count in servers:
        laptop.polls(node, register=0, count=count, rate=rate, outstanding=1, timeout_ms=1_000)
        net.observe("modbus_latency", on=node, every=1)
        for m in ("modbus_unanswered", "modbus_skipped", "modbus_timeouts"):
            net.observe(m, on=node, every=end)
    for node in nodes:
        net.observe("frames_sent", on=node, every=end)
    for m in ("ingress_drops_total", "ingress_drop_episodes_total", "ingress_drop_time_total"):
        net.observe(m, on=sw, every=end)
    return net.to_scenario(duration=duration_ms, seed=seed)


def _polls(metrics: dict, server: str, poll_rate: float, long_ms: float = LONG_MS) -> dict:
    """Polls sent and affected, a round trip of *long_ms* or more or none,
    and the p99 and maximum of the others, in ms."""
    rtt = [v / 1_000 for _, v in metrics[f"modbus_latency:{server}"]]
    short = sorted(x for x in rtt if x < long_ms)
    at, skipped = metrics[f"modbus_skipped:{server}"][-1]
    sent = cells._instants(at, poll_rate) - skipped
    affected = sum(x >= long_ms for x in rtt) + metrics[f"modbus_unanswered:{server}"][-1][1]
    q = statistics.quantiles(short, n=100) if len(short) > 1 else [math.nan] * 99
    return {
        "sent": sent,
        "affected": affected,
        "p99_ms": q[98],
        "max_ms": short[-1] if short else math.nan,
    }


CASES = {
    # name: (pis, laptop Mb/s, poll ms, registers, waveshare)
    "two 4050": ([[(4_050, FRAME, "poisson")], [(4_040, FRAME, "poisson")]], 100, 10, 10, True),
    "two 3900": ([[(3_907, FRAME, "poisson")], [(3_905, FRAME, "poisson")]], 100, 10, 10, True),
    "10Mb 808": ([[(808, FRAME, "poisson")]], 10, 100, 4, False),
    "10Mb 846": ([[(846, FRAME, "poisson")]], 10, 100, 4, False),
    "512 B": ([[(23_383, 512, "poisson")]], 100, 10, 10, True),
    "mixed": ([[(6_999, FRAME, "poisson"), (3_154, 512, "poisson")]], 100, 10, 10, True),
    "ingress 8100": ([[(8_099, FRAME, "poisson")]], 100, 10, 10, True),
    "batch": ([[(7_500, FRAME, "batch8")]], 100, 10, 10, True),
}


def validate_case(kw: dict, case: str, duration_ms: float, seeds, engines, log=print) -> list[dict]:
    pis, mbps, poll_ms, registers, ws = CASES[case]
    duration = duration_ms * (4 if mbps == 10 else 1)  # 10 Mb/s: ten times fewer frames
    out = []
    for seed in seeds:
        sc = testbed(kw, pis, duration, seed, mbps, poll_ms, registers, ws)
        for engine in engines:
            if engine == "simpy":
                with audit() as found:
                    result = backends.get(engine).run(sc)
            else:
                found = None
                result = backends.get(engine).run(sc)
            m = result.metrics
            last = {k: s[-1][1] for k, s in m.items() if s}
            sent = sum(last[f"frames_sent:pi{i + 1}"] for i in range(len(pis)))
            drops = last["ingress_drops_total:sw"]
            row = {
                "case": case,
                "seed": seed,
                "backend": engine,
                "loss": drops / sent,
                "mean_run": drops / max(last["ingress_drop_episodes_total:sw"], 1),
                "pi": [_polls(m, f"pi{i + 1}", 1000 / poll_ms) for i in range(len(pis))],
                "waveshare": _polls(m, "waveshare", 50) if ws else None,
            }
            if found is not None:
                runs = [n for rs in found["runs"].values() for n in rs]
                row["single"] = sum(n == 1 for n in runs) / len(runs) if runs else 0.0
                row["runs_by_port"] = {
                    str(p): statistics.mean(rs) for p, rs in found["runs"].items() if rs
                }
                row["overlap"] = overlaps(found["intervals"])
                by_size = {}
                for (port, size), k in found["decided_by_size"].items():
                    if port < len(pis):
                        d = found["drops_by_size"].get((port, size), 0)
                        prev = by_size.get(str(size), (0, 0))
                        by_size[str(size)] = (prev[0] + d, prev[1] + k)
                row["loss_by_size"] = {s: d / k for s, (d, k) in by_size.items() if k}
            out.append(row)
            pi_share = [round(p["affected"] / p["sent"], 4) for p in row["pi"]]
            log(
                f"{case:12s} seed {seed} {engine:6s} loss {row['loss']:.3%}"
                f" run {row['mean_run']:.2f} pi {pi_share}"
                f" ws {row['waveshare']['affected'] if ws else '-'}"
            )
    return out


def run_pool(
    duration_ms: float,
    engines: list[str],
    label: str = "shared-pool",
    out: Path | None = None,
    seeds: tuple[int, ...] = SEEDS,
    data: Path = DATA,
    fitted: dict | None = None,
    log=print,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {
        "format": "smolpy-pool",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "hardware": hardware(data),
        "duration_ms": duration_ms,
        "seeds": list(seeds),
    }
    if fitted is None:
        doc["calibration"] = calibrate(data, log)
        fitted = doc["calibration"]["fitted"]
    doc["fitted"] = fitted
    out.write_text(json.dumps(doc, indent=1))
    kw = switch(fitted["P"], fitted["R"], fitted["D"])
    doc["validation"] = []
    for case in CASES:
        doc["validation"] += validate_case(kw, case, duration_ms, seeds, engines, log)
        out.write_text(json.dumps(doc, indent=1))
    return out


def summarize_pool(path: Path) -> str:
    doc = json.loads(path.read_text())
    f = doc["fitted"]
    lines = [f"Fitted: pool {f['P']}, reserve {f['R']}, drain {f['D']} cells of 128 B."]
    cal = doc.get("calibration")
    if cal:
        best = min(cal["stage1"], key=lambda s: s["score"])
        lines += [
            "",
            f"Stage 1 (L = P + R, D), best: L {best['L']}, D {best['D']}: loss {best['loss']:.3%},"
            f" mean run {best['mean_run']:.2f}; burst error {best['burst']:.2f}, runs error"
            f" {best['runs']:.2f} (squared, in sigmas).",
            "",
            "| R | reserve error | " + " | ".join(cal["measured_reserve"]) + " |",
            "|---" * (2 + len(cal["measured_reserve"])) + "|",
            "| measured | | "
            + " | ".join(
                f"{min(v)}–{max(v)} ({statistics.median(v):g})"
                for v in cal["measured_reserve"].values()
            )
            + " |",
        ]
        for s in sorted(cal["stage2"], key=lambda s: s["score"])[:6]:
            lines.append(
                f"| {s['R']} at {s['rate64']} | {s['score']:.2f} | "
                + " | ".join(
                    f"{min(v)}–{max(v)} ({statistics.median(v):g})" for v in s["model"].values()
                )
                + " |"
            )
    lines += [
        "",
        "| case | engine | loss | mean run | single | overlap | Pi polls | p99 / max (ms)"
        " | Waveshare |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in doc["validation"]:
        groups.setdefault((r["case"], r["backend"]), []).append(r)
    for (case, engine), rows in groups.items():
        pi_sent = sum(p["sent"] for r in rows for p in r["pi"])
        pi_aff = sum(p["affected"] for r in rows for p in r["pi"])
        p99 = statistics.fmean(p["p99_ms"] for r in rows for p in r["pi"])
        mx = max(p["max_ms"] for r in rows for p in r["pi"])
        ws = sum(r["waveshare"]["affected"] for r in rows) if rows[0]["waveshare"] else "–"
        single = (
            f"{statistics.fmean(r['single'] for r in rows):.1%}" if "single" in rows[0] else "–"
        )
        ov = (
            f"{sum(r['overlap'][0] for r in rows)} of {sum(r['overlap'][1] for r in rows)}"
            if "overlap" in rows[0] and len(rows[0]["pi"]) > 1
            else "–"
        )
        lines.append(
            f"| {case} | {engine} | {statistics.fmean(r['loss'] for r in rows):.3%}"
            f" | {statistics.fmean(r['mean_run'] for r in rows):.2f} | {single} | {ov}"
            f" | {pi_aff / pi_sent:.2%} | {p99:.2f} / {mx:.2f} | {ws} |"
        )
    return "\n".join(lines)
