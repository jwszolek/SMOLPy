"""A drop state shared by the whole switch, on the testbed of ADR-0016: the
reserve refitted under it, then a validation against two Pis at 10 and at
100 Mb/s and ADR-0015's single-source cases.

The LS108GP as ADR-0016 models it: ``cell=128 pool=P ingress_reserve=R
ingress_drain=D ingress_drop=switch``, in cells.

**Calibration.** With one sender the switch's drop state is the port's
(ADR-0016's identity), so ADR-0015's stage 1, L = P + R = 770 and D = 72,
stands; this module checks the identity on its bursts and loss runs. R, and
the pace of Pi 2's 64 B bursts, are refitted on the reserve test with its
constant background (``2026-11-24_rezerwa/``), as in ADR-0015's stage 2.

**Validation**, with no refitting:
- two Pis at 10 Mb/s, 2 x 405 and 2 x 425 frames/s
  (``2026-11-24_dwa_rpi_10Mb/``), with the overlap of their loss runs from
  the laptop's receive times (``.seq.r``);
- two Pis at 100 Mb/s, 2 x 4 050 and 2 x 3 900 (no receive times: the
  model's overlap only);
- ADR-0015's single-source cases, which the identity leaves unchanged.

**The measure of a run**, the same on the hardware and in the model: a run
of a stream is a maximal sequence of lost sequence numbers; its interval
runs from the stream's last frame before it to its first frame after it, by
the laptop's receive times on the hardware and by the switch's admission
times in the model (a FIFO egress maps one to the other monotonically). A
run overlaps if its interval meets one of the other stream's; an episode is
the overlapping intervals of both streams, merged.

A poll is retransmitted if its round trip is 200 ms or more, the TCP RTO:
at 10 Mb/s a round trip with no retransmission reaches about 170 ms.

    python -m benchmarks switch-drop [--duration 60000] [--backend simpy rust ns3]
"""

from __future__ import annotations

import bisect
import contextlib
import datetime as dt
import json
import statistics
from collections.abc import Iterator
from pathlib import Path

from benchmarks import cells, pool
from benchmarks.runner import RESULTS_DIR, environment
from smolpy import backends

DATA = pool.DATA
TEN = "2026-11-24_dwa_rpi_10Mb"
FRAME = pool.FRAME
L, D = 770, 72  # ADR-0015's stage 1, which the identity leaves standing
R_GRID = (0, 12, 24, 30, 36, 42, 48, 54, 60, 66, 72, 84)
RATE64_GRID = (70_000, 90_000, 110_000, 130_000, 150_000, 180_000, 230_000, 340_000)
RTO_MS = 200.0
SHIFT_S = 0.5  # the control: one stream shifted by 500 ms
ENGINES = ("simpy", "rust", "ns3")
SEEDS = (1, 2, 3)


def switch(p: int, r: int, d: int) -> dict:
    """The LS108GP's switch parameters, with a drop state for the switch."""
    return {**pool.switch(p, r, d), "ingress_drop": "switch"}


# --- the measure of a run ---

Run = tuple[float, float, int]  # interval start, end (s), frames lost


def runs_from_decisions(decisions: list[tuple[float, bool]]) -> list[Run]:
    """A stream's runs from its admission decisions (t, admitted), in order:
    those with an admitted frame on both sides."""
    out, last_ok, k = [], None, 0
    for t, ok in decisions:
        if ok:
            if k and last_ok is not None:
                out.append((last_ok, t, k))
            k, last_ok = 0, t
        else:
            k += 1
    return out


def overlap_stats(r1: list[Run], r2: list[Run]) -> dict:
    """Runs overlapping one of the other stream's, and episodes with both,
    over the window both streams cover."""
    lo, hi = max(r1[0][0], r2[0][0]), min(r1[-1][1], r2[-1][1])
    r1 = [x for x in r1 if lo <= x[0] and x[1] <= hi]
    r2 = [x for x in r2 if lo <= x[0] and x[1] <= hi]

    def hits(a: list[Run], b: list[Run]) -> int:
        # one stream's intervals are disjoint and in order: the only
        # candidate is the last of b to start before a run ends
        starts = [x[0] for x in b]
        n = 0
        for s, e, _ in a:
            i = bisect.bisect_left(starts, e) - 1
            n += i >= 0 and s < b[i][1]
        return n

    episodes: list[list] = []
    for s, e, _, p in sorted([(*x, 1) for x in r1] + [(*x, 2) for x in r2]):
        if episodes and s < episodes[-1][1]:
            episodes[-1][1] = max(episodes[-1][1], e)
            episodes[-1][2].add(p)
        else:
            episodes.append([s, e, {p}])
    lengths = [k for *_, k in r1 + r2]
    return {
        "runs": [len(r1), len(r2)],
        "mean_run": [statistics.mean(k for *_, k in r) if r else 0.0 for r in (r1, r2)],
        "single": sum(k == 1 for k in lengths) / len(lengths),
        "overlapping": hits(r1, r2) + hits(r2, r1),
        "runs_total": len(r1) + len(r2),
        "episodes": len(episodes),
        "episodes_both": sum(len(e[2]) == 2 for e in episodes),
    }


def _shifted(runs: list[Run], by: float) -> list[Run]:
    return [(s + by, e + by, k) for s, e, k in runs]


# --- the hardware, from the raw files ---


def rx_runs(name: str, data: Path = DATA) -> list[Run]:
    """A stream's runs, with the laptop's receive times (``.seq.r``)."""
    seq = pool._read(data / f"{name}.seq")
    rx = pool._read(data / f"{name}.seq.r")
    at = dict(zip(seq, rx, strict=True))
    got = sorted(at)
    return [(at[a] / 1e9, at[b] / 1e9, b - a - 1) for a, b in zip(got, got[1:]) if b - a > 1]


def _two_ten(rate: int, data: Path) -> dict:
    prefix = f"{TEN}/r{rate}"
    runs = [rx_runs(f"{prefix}_{p}", data) for p in ("pi1", "pi2")]
    return {
        "udp": [pool.seq_stats(f"{prefix}_{p}", data) for p in ("pi1", "pi2")],
        "overlap": overlap_stats(*runs),
        "control": overlap_stats(runs[0], _shifted(runs[1], SHIFT_S)),
        "pi": [pool.rtt_stats(f"{prefix}_{p}.csv", data, RTO_MS) for p in ("pi1", "pi2")],
        "waveshare": pool.rtt_stats(f"{prefix}_ws.csv", data, RTO_MS),
    }


def hardware(data: Path = DATA) -> dict[str, dict]:
    """The validation set's hardware numbers: ADR-0015's, the polls at the
    200 ms threshold, and the two Pis at 10 Mb/s."""
    out = pool.hardware(data, RTO_MS)
    out["10Mb two 405"] = _two_ten(405, data)
    out["10Mb two 425"] = _two_ten(425, data)
    return out


# --- the audit ---


@contextlib.contextmanager
def audit() -> Iterator[dict]:
    """SimPy's ingress decisions, by port and frame size: (t in s, admitted)."""
    from smolpy.sim import engine

    found: dict[tuple[int, int], list[tuple[float, bool]]] = {}
    admit = engine._Account.admit

    def recording(account, size: int) -> bool:
        ok = admit(account, size)
        sw = account.switch
        if sw is not None:
            port = next(p for p, a in sw.accounts.items() if a is account)
            found.setdefault((port, size), []).append((account.env._now / 1e9, ok))
        return ok

    engine._Account.admit = recording  # type: ignore[method-assign]
    try:
        yield found
    finally:
        engine._Account.admit = admit  # type: ignore[method-assign]


# --- calibration ---


def identity(data: Path = DATA, log=print) -> dict:
    """ADR-0015's stage 1 at its best point, L = 770 and D = 72, under both
    scopes: the bursts and the loss runs are one sender's, so they agree."""
    port, whole = pool.switch(L, 0, D), switch(L, 0, D)
    rows = cells.burst_rows(data)
    bursts = [
        (
            pool.burst(port, r["frame_bytes"], r["n"], r["send_rate"]),
            pool.burst(whole, r["frame_bytes"], r["n"], r["send_rate"]),
        )
        for r in rows
    ]
    runs = (pool.loss_runs(port), pool.loss_runs(whole))
    same = all(a == b for a, b in bursts) and runs[0] == runs[1]
    log(f"stage 1 identity at L {L}, D {D}: {'the same' if same else 'DIFFERENT'};"
        f" loss {runs[1]['loss']:.3%}, run {runs[1]['mean_run']:.2f}")  # fmt: skip
    return {"same": same, "bursts": bursts, "loss_runs": list(runs)}


def calibrate(data: Path = DATA, log=print) -> dict:
    """R, and the pace of Pi 2's 64 B bursts, on the reserve test, with
    P = L - R and the switch's drop state."""
    groups: dict[tuple[int, int], list[int]] = {}
    for r in pool.reserve_rows(data):
        groups.setdefault((r["frame_B"], r["N"]), []).append(r["received"])

    def error(got: list[int], measured: list[int]) -> float:
        sigma = max(1.0, (max(measured) - min(measured)) / 2)
        return ((statistics.median(got) - statistics.median(measured)) / sigma) ** 2

    grid = []
    for r in R_GRID:
        kw = switch(L - r, r, D)
        full = {
            k: [pool.reserve(kw, k[0], k[1], ph) for ph in pool.PHASES_MS]
            for k in groups
            if k[0] != 64
        }
        for rate64 in RATE64_GRID:
            pool.RESERVE_RATES[64] = rate64
            model = dict(full)
            for k in groups:
                if k[0] == 64:
                    model[k] = [pool.reserve(kw, 64, k[1], ph) for ph in pool.PHASES_MS]
            score = statistics.mean(error(model[k], v) for k, v in groups.items())
            grid.append(
                {"R": r, "rate64": rate64, "score": score,
                 "model": {f"{s} {n}": v for (s, n), v in model.items()}}
            )  # fmt: skip
            log(f"R {r} at {rate64} frames/s: reserve {score:.2f}")
    best = min(grid, key=lambda g: g["score"])
    pool.RESERVE_RATES[64] = best["rate64"]
    return {
        "grid": grid,
        "measured": {f"{s} {n}": v for (s, n), v in groups.items()},
        "fitted": {"P": L - best["R"], "R": best["R"], "D": D},
        "rate64": best["rate64"],
    }


# --- validation ---


def polls(metrics: dict, server: str, poll_rate: float) -> dict:
    """``benchmarks.pool._polls`` with a poll lost counted by its timeout,
    not as unanswered: the request in flight when the run ends is neither
    lost nor retransmitted, and at 10 Mb/s one nearly always is."""
    out = pool._polls(metrics, server, poll_rate, RTO_MS)
    unanswered = metrics[f"modbus_unanswered:{server}"][-1][1]
    timeouts = metrics[f"modbus_timeouts:{server}"][-1][1]
    out["affected"] += timeouts - unanswered
    return out


CASES = {
    # name: (pis, laptop Mb/s, poll ms, registers, waveshare, duration in s
    # or None for the run's), the 10 Mb/s rates as the Pis sent them
    "10Mb two 405": (
        [[(363_424 / 900, FRAME, "poisson")], [(364_121 / 899.99, FRAME, "poisson")]],
        10, 100, 4, True, 900,
    ),
    "10Mb two 425": (
        [[(127_733 / 300, FRAME, "poisson")], [(127_589 / 299.99, FRAME, "poisson")]],
        10, 100, 4, True, 300,
    ),
    **{name: (*case, None) for name, case in pool.CASES.items()},
}  # fmt: skip


def validate_case(kw: dict, case: str, duration_ms: float, seeds, engines, log=print) -> list[dict]:
    pis, mbps, poll_ms, registers, ws, seconds = CASES[case]
    if seconds is not None:
        duration = seconds * 1_000.0
    else:
        duration = duration_ms * (4 if mbps == 10 else 1)  # as benchmarks.pool
    out = []
    for seed in seeds:
        sc = pool.testbed(kw, pis, duration, seed, mbps, poll_ms, registers, ws)
        for engine in engines:
            found: dict | None = None
            if engine == "simpy":
                with audit() as found:
                    result = backends.get(engine).run(sc)
            else:
                result = backends.get(engine).run(sc)
            m = result.metrics
            last = {k: s[-1][1] for k, s in m.items() if s}
            sent = sum(last[f"frames_sent:pi{i + 1}"] for i in range(len(pis)))
            drops = last["ingress_drops_total:sw"]
            row: dict = {
                "case": case,
                "seed": seed,
                "backend": engine,
                "loss": drops / sent,
                "episodes": last["ingress_drop_episodes_total:sw"],
                "drop_time_ms": last["ingress_drop_time_total:sw"],
                "pi": [polls(m, f"pi{i + 1}", 1000 / poll_ms) for i in range(len(pis))],
                "waveshare": polls(m, "waveshare", 50) if ws else None,
            }
            if found is not None:
                # each Pi's stream: its first flow's frames
                streams = [found.get((p, flows[0][1]), []) for p, flows in enumerate(pis)]
                row["loss_by_stream"] = [
                    sum(not ok for _, ok in s) / len(s) if s else 0.0 for s in streams
                ]
                runs = [runs_from_decisions(s) for s in streams]
                lengths = [k for r in runs for *_, k in r]
                row["mean_run"] = [statistics.mean(k for *_, k in r) if r else 0.0 for r in runs]
                row["single"] = sum(k == 1 for k in lengths) / len(lengths) if lengths else 0.0
                if len(pis) == 2 and all(runs):
                    row["overlap"] = overlap_stats(*runs)
                    row["control"] = overlap_stats(runs[0], _shifted(runs[1], SHIFT_S))
                responses = {
                    f"{port}:{size}": sum(not ok for _, ok in d) / len(d)
                    for (port, size), d in found.items()
                    if port < len(pis) and size not in {f[1] for f in pis[port]}
                }
                row["pi_responses_lost"] = responses
            out.append(row)
            share = [round(p["affected"] / p["sent"], 4) for p in row["pi"]]
            extra = ""
            if "overlap" in row:
                o = row["overlap"]
                extra = (f" overlap {o['overlapping']}/{o['runs_total']}"
                         f" both {o['episodes_both']}/{o['episodes']}")  # fmt: skip
            log(
                f"{case:13s} seed {seed} {engine:6s} loss {row['loss']:.3%}"
                f" pi {share} ws {row['waveshare']['affected'] if ws else '-'}{extra}"
            )
    return out


def run_switch_drop(
    duration_ms: float,
    engines: list[str],
    label: str = "switch-drop-state",
    out: Path | None = None,
    seeds: tuple[int, ...] = SEEDS,
    data: Path = DATA,
    fitted: dict | None = None,
    calibration: Path | None = None,
    log=print,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {
        "format": "smolpy-switch-drop",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "hardware": hardware(data),
        "duration_ms": duration_ms,
        "seeds": list(seeds),
    }
    if calibration is not None:  # an earlier run's calibration, reused
        earlier = json.loads(calibration.read_text())
        doc["identity"], doc["calibration"] = earlier["identity"], earlier["calibration"]
        fitted = doc["calibration"]["fitted"]
        pool.RESERVE_RATES[64] = doc["calibration"]["rate64"]
    if fitted is None:
        doc["identity"] = identity(data, log)
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


# --- the summary ---


def _range(values: list[float], fmt: str) -> str:
    lo, hi = min(values), max(values)
    return format(lo, fmt) if format(lo, fmt) == format(hi, fmt) else f"{lo:{fmt}}–{hi:{fmt}}"


def summarize(path: Path) -> str:
    doc = json.loads(path.read_text())
    f = doc["fitted"]
    lines = [f"Fitted: pool {f['P']}, reserve {f['R']}, drain {f['D']} cells of 128 B."]
    if "identity" in doc:
        lines.append(f"Stage 1 identity at L {L}, D {D}: {doc['identity']['same']}.")
    cal = doc.get("calibration")
    if cal:
        best_rate = cal["rate64"]
        lines += [
            "",
            f"Reserve test, by R, at the best 64 B pace for each R (overall best {best_rate}/s):",
            "",
            "| R | pace | error | " + " | ".join(cal["measured"]) + " |",
            "|---" * (3 + len(cal["measured"])) + "|",
            "| measured | | | "
            + " | ".join(
                f"{min(v)}–{max(v)} ({statistics.median(v):g})" for v in cal["measured"].values()
            )
            + " |",
        ]
        for r in R_GRID:
            g = min((g for g in cal["grid"] if g["R"] == r), key=lambda g: g["score"])
            lines.append(
                f"| {r} | {g['rate64']} | {g['score']:.2f} | "
                + " | ".join(
                    f"{min(v)}–{max(v)} ({statistics.median(v):g})" for v in g["model"].values()
                )
                + " |"
            )
    hw = doc["hardware"]
    lines += [
        "",
        "| case | source | loss per stream | mean run | single | overlapping runs"
        " | episodes with both | Pi polls ≥ 200 ms | Pi p99 / max (ms) | Waveshare |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in doc["validation"]:
        groups.setdefault((r["case"], r["backend"]), []).append(r)
    for case in CASES:
        h = hw.get(case)
        if h is not None and "udp" in h:
            udp = h["udp"]
            ov = h.get("overlap")
            if not isinstance(ov, dict):
                ov = None  # no receive times: not measurable
            pi = h.get("pi", [])
            ws = h.get("waveshare")

            def both(key: str, fmt: str, items: list[dict]) -> str:
                return " / ".join(format(x[key], fmt) for x in items)

            cells_ = [
                case,
                "hardware",
                both("loss", ".2%", udp),
                both("mean_run", ".2f", udp),
                format(statistics.mean(u["single"] for u in udp), ".0%"),
            ]
            if ov:
                ctl = h["control"]
                cells_ += [
                    f"{ov['overlapping'] / ov['runs_total']:.0%} (shifted"
                    f" {ctl['overlapping'] / ctl['runs_total']:.0%})",
                    f"{ov['episodes_both'] / ov['episodes']:.0%}",
                ]
            else:
                cells_ += ["–", "–"]
            if pi:
                p99 = statistics.mean(p["p99"] for p in pi)
                cells_ += [
                    both("affected", ".2%", pi),
                    f"{p99:.1f} / {max(p['max'] for p in pi):.1f}",
                ]
            else:
                cells_ += ["–", "–"]
            cells_.append(f"{round(ws['affected'] * ws['sent'])} of {ws['sent']}" if ws else "–")
            lines.append("| " + " | ".join(cells_) + " |")
        for engine in ENGINES:
            rows = groups.get((case, engine))
            if not rows:
                continue
            loss = (
                _range([x for r in rows for x in r["loss_by_stream"]], ".2%")
                if "loss_by_stream" in rows[0]
                else _range([r["loss"] for r in rows], ".2%")
            )
            run = (
                _range([x for r in rows for x in r["mean_run"]], ".2f")
                if "mean_run" in rows[0]
                else "–"
            )
            single = _range([r["single"] for r in rows], ".0%") if "single" in rows[0] else "–"
            if "overlap" in rows[0]:

                def share(key: str, a: str, b: str) -> str:
                    return _range([r[key][a] / r[key][b] for r in rows], ".0%")

                ov = share("overlap", "overlapping", "runs_total")
                ov = f"{ov} (shifted {share('control', 'overlapping', 'runs_total')})"
                with_both = share("overlap", "episodes_both", "episodes")
            else:
                ov = with_both = "–"
            pi = _range([p["affected"] / p["sent"] for r in rows for p in r["pi"]], ".2%")
            p99 = statistics.fmean(p["p99_ms"] for r in rows for p in r["pi"])
            mx = max(p["max_ms"] for r in rows for p in r["pi"])
            ws = (
                f"{sum(r['waveshare']['affected'] for r in rows):g} of"
                f" {sum(r['waveshare']['sent'] for r in rows):g}"
                if rows[0]["waveshare"]
                else "–"
            )
            lines.append(
                f"| {case} | {engine} | {loss} | {run} | {single} | {ov} | {with_both} | {pi}"
                f" | {p99:.1f} / {mx:.1f} | {ws} |"
            )
    return "\n".join(lines)
