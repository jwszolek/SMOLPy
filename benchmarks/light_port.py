"""The protection of a light port (ADR-0017 Part 2) on the testbed.

The LS108GP as ADR-0017 models it: ``cell=128 pool=740cells
ingress_reserve=60cells ingress_drain=72cells ingress_drop=switch
ingress_drain_count=net ingress_pool_count=total ingress_alpha=24
ingress_mark=204cells``. Nothing is refitted here: L = 770, R = 60 and
D = 72 are ADR-0015's and ADR-0016's, and α, P and the mark were fitted on
the quick model's five campaigns (ADR-0017 §6.5).

**One sender.** With α the identity with ADR-0016 holds only by its bound:
beyond its reserve a frame of u cells is admitted while the port holds at
most ⌊(α (P + R) + R − u) / (1 + α)⌋, 769 for u = 12, so a Pi alone holds
780 cells after a frame where ADR-0016's holds 768. The module runs one Pi
at 8 090/s under both.

**Against the quick model** (``stanowisko/model/symulacja_puli.py``,
``res_shared=True``, ``alpha``, ``snap``): SMOLPy with the quick model's
streams, a heavy Poisson Pi, the light Pi, and the Modbus responses as
constant streams of 1 cell on ports of their own (50/s from the Waveshare,
100/s from each Pi), each stream's loss and mean run against the quick
model's, with the sampling errors of both.

**Against the hardware**, with no refitting: the testbed's scenario, each
Pi a Modbus slave polled every 10 ms for 10 registers and the Waveshare
every 20 ms, on the blind campaign ``2026-11-26_ciemno_mac`` (21 cases) and
``2026-11-25_noc2_mac`` (44 cases), at the rates the Pis sent. The hardware
is each case's ``RAPORT.md`` row, the middle of its repetitions' range:
each Pi's loss and mean run, in the window in which both send. The model's
streams are measured by SimPy's admission decisions, as in
``benchmarks.switch_drop``.

    python -m benchmarks light-port [--duration 56000] [--seeds 1 2 3]
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import math
import re
import statistics
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from benchmarks import pool, switch_drop
from benchmarks.runner import RESULTS_DIR, environment
from smolpy import Cells, Network, backends

DATA = pool.DATA
QUICK_MODEL = DATA.parent.parent / "model" / "symulacja_puli.py"
CAMPAIGNS = ("2026-11-26_ciemno_mac", "2026-11-25_noc2_mac")
CELL = 128
P, R, D, ALPHA, MARK = 740, 60, 72, 24.0, 204
LS108GP = {
    "cell": CELL,
    "pool": Cells(P),
    "ingress_reserve": Cells(R),
    "ingress_drain": Cells(D),
    "ingress_drop": "switch",
    "ingress_drain_count": "net",
    "ingress_pool_count": "total",
    "ingress_alpha": ALPHA,
    "ingress_mark": Cells(MARK),
}
ADR_0016 = {**switch_drop.switch(710, R, D)}
QUICK = {
    "pool": P,
    "R": R,
    "D": D,
    "alpha": ALPHA,
    "end": "zajecie",
    "snap": MARK,
    "res_shared": True,
}
RESPONSES = (50, 100, 100)  # 1-cell Modbus responses/s: the Waveshare, Pi 1, Pi 2
S_FRAME = 123.04e-6  # 1 538 B on the wire at 100 Mb/s
SEEDS = (1, 2, 3)
DURATION_MS = 56_000.0  # the window in which both Pis send


def frame(payload: int) -> int:
    """The Ethernet frame of a UDP payload: 8 + 20 + 14 + 4 bytes more."""
    return payload + 46


# --- the campaigns ---

Case = tuple[float, float, int, str]  # Pi 1/s, Pi 2/s, Pi 2's payload, its pattern


def plan(campaign: str, data: Path = DATA) -> dict[str, Case]:
    """A campaign's cases, from its ``plan.txt``: Pi 1 always Poisson 1 472 B."""
    out = {}
    for line in (data / campaign / "plan.txt").read_text().splitlines():
        f = line.split()
        if not f or f[0].startswith("#") or len(f) < 5:
            continue
        out[f[0]] = (float(f[1]), float(f[2]), int(f[3]), f[4])
    return out


def sent_rates(campaign: str, case: str, data: Path = DATA) -> tuple[float, float]:
    """The rates the Pis sent, from ``*_gen.txt``, over the repetitions."""
    rates = []
    for pi in ("pi1", "pi2"):
        v = []
        for path in sorted((data / campaign).glob(f"{case}_r*_{pi}_gen.txt")):
            m = re.search(r"(\d+) ramek w ([\d.]+) s", path.read_text())
            if m:
                v.append(int(m[1]) / float(m[2]))
        rates.append(statistics.fmean(v) if v else 0.0)
    return rates[0], rates[1]


def _middle(cell: str) -> float | None:
    numbers = re.findall(r"\d+(?:\.\d+)?", cell.split("(")[0])
    return statistics.fmean(map(float, numbers[:2])) if numbers else None


def hardware(campaign: str, data: Path = DATA) -> dict[str, dict]:
    """Each case's hardware row in ``RAPORT.md``: each Pi's loss (a
    fraction) and mean run, the middle of the repetitions' range."""
    out = {}
    case = None
    for line in (data / campaign / "RAPORT.md").read_text().splitlines():
        if not line.startswith("|"):
            continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) < 6:
            continue
        if c[0]:
            case = c[0].replace("*", "").split()[0]
        if c[1] == "sprzęt" and case is not None:
            loss = [_middle(c[2]), _middle(c[3])]
            out[case] = {
                "loss": [None if x is None else x / 100 for x in loss],
                "mean_run": [_middle(c[4]), _middle(c[5])],
            }
    return out


# --- SMOLPy's streams ---


def _streams(found: dict, ports: Iterable[int], sizes: dict[int, int] | None = None) -> list[dict]:
    """Each port's stream, by SimPy's decisions: its loss and mean run; with
    *sizes*, only its frames of that size."""
    out = []
    for port in ports:
        decisions = sorted(
            d
            for (p, size), ds in found.items()
            if p == port and (sizes is None or size == sizes[port])
            for d in ds
        )
        runs = switch_drop.runs_from_decisions(decisions)
        out.append(
            {
                "loss": sum(not ok for _, ok in decisions) / len(decisions) if decisions else 0.0,
                "mean_run": statistics.fmean(k for *_, k in runs) if runs else 0.0,
                "frames": len(decisions),
            }
        )
    return out


def quick_net(case: Case, kw: dict = LS108GP, source_mbps: float = 10_000) -> Network:
    """The quick model's streams on SMOLPy: a heavy Poisson Pi of 1 518 B,
    the light Pi, and the three Modbus response streams of 64 B (1 cell, 84 B
    on the wire), constant, each on a port of its own, into one 100 Mb/s
    port. The sources' links are fast, as the quick model has none."""
    r1, r2, payload, pattern = case
    net = Network("quick-model")
    sw = net.switch("sw", ports=6, mac_table="topology", **kw)
    sink = net.adapter("mac", ip="10.0.0.100")
    flows = [(r1, 1_518, "poisson"), (r2, frame(payload), _pattern(pattern))]
    flows += [(rate, 64, "constant") for rate in RESPONSES]
    for i, (rate, size, pat) in enumerate(flows):  # stream i on port i
        src = net.adapter(f"s{i}", ip=f"10.0.0.{i + 1}")
        net.link(src, sw, speed=source_mbps, length=2)
        src.sends(sink, rate=rate, size=size, pattern=pat)  # type: ignore[arg-type]
    net.link(sink, sw, speed=100, length=2)
    return net


def _pattern(pattern: str) -> str:
    return {"const": "constant"}.get(pattern, pattern)


def _smolpy_quick(job: tuple) -> list[dict]:
    case, seed, seconds = job
    sc = quick_net(case).to_scenario(duration=seconds * 1_000, seed=seed)
    with switch_drop.audit() as found:
        backends.get("simpy").run(sc)
    return _streams(found, range(5))


def _quick(job: tuple) -> list[dict]:
    path, case, seed, seconds = job
    spec = importlib.util.spec_from_file_location("symulacja_puli", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    import sys

    sys.path.insert(0, str(Path(path).parent))  # it imports nakladanie beside it
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    r1, r2, payload, pattern = case
    fr = frame(payload)
    streams = [(r1, 12), (r2, math.ceil(fr / CELL), fr + 20, pattern)]
    streams += [(rate, 1, 84, "const") for rate in RESPONSES]
    n = round(sum(s[0] for s in streams) * seconds)
    r = module.sim(streams, "dt_glob", n=n, seed=seed, s=S_FRAME, **QUICK)
    return [{"loss": r["loss"][i], "mean_run": r["mean_run"][i]} for i in range(len(streams))]


def _mean_se(values: list[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return mean, se


def _summary(per_seed: list[list[dict]], stream: int) -> dict:
    loss, loss_se = _mean_se([s[stream]["loss"] for s in per_seed])
    run, run_se = _mean_se([s[stream]["mean_run"] for s in per_seed])
    return {"loss": loss, "loss_se": loss_se, "mean_run": run, "mean_run_se": run_se}


def against_quick_model(
    path: Path,
    cases: list[Case],
    seeds: int = 3,
    seconds: float = 56,
    names: list[str] | None = None,
    workers: int | None = None,
) -> list[dict]:
    """Each case on SMOLPy (SimPy) and on the quick model, *seeds* runs of
    *seconds* each: the heavy and the light stream's loss and mean run."""
    jobs_s = [(c, s, seconds) for c in cases for s in range(1, seeds + 1)]
    jobs_q = [(str(path), c, s, seconds) for c in cases for s in range(1, seeds + 1)]
    with ProcessPoolExecutor(workers) as ex:
        smol = list(ex.map(_smolpy_quick, jobs_s))
        quick = list(ex.map(_quick, jobs_q))
    rows = []
    for i, case in enumerate(cases):
        a, b = smol[i * seeds : (i + 1) * seeds], quick[i * seeds : (i + 1) * seeds]
        rows.append(
            {
                "case": names[i] if names else str(case),
                "rates": case[:2],
                "heavy": {"smolpy": _summary(a, 0), "quick": _summary(b, 0)},
                "light": {"smolpy": _summary(a, 1), "quick": _summary(b, 1)},
                "waveshare": {"smolpy": _summary(a, 2), "quick": _summary(b, 2)},
            }
        )
    return rows


# --- the testbed ---


def _testbed(job: tuple) -> dict:
    kw, case, seed, duration_ms = job
    r1, r2, payload, pattern = case
    pis = [[(r1, 1_518, "poisson")], [(r2, frame(payload), _pattern(pattern))]]
    sc = pool.testbed(kw, pis, duration_ms, seed, 100, 10, 10, True)
    with switch_drop.audit() as found:
        result = backends.get("simpy").run(sc)
    m = result.metrics
    return {
        "seed": seed,
        "streams": _streams(found, (0, 1), {0: 1_518, 1: frame(payload)}),
        "pi": [switch_drop.polls(m, f"pi{i + 1}", 100) for i in range(2)],
        "waveshare": switch_drop.polls(m, "waveshare", 50),
    }


def validate(
    kw: dict,
    campaigns: Iterable[str] = CAMPAIGNS,
    seeds: Iterable[int] = SEEDS,
    duration_ms: float = DURATION_MS,
    data: Path = DATA,
    workers: int | None = None,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Every case of *campaigns* on the testbed's scenario at the rates the
    Pis sent, against the hardware."""
    jobs, meta = [], []
    for campaign in campaigns:
        hw = hardware(campaign, data)
        for name, (_, _, payload, pattern) in plan(campaign, data).items():
            r1, r2 = sent_rates(campaign, name, data)
            if not (r1 and r2) or name not in hw:
                continue
            for seed in seeds:
                jobs.append((kw, (r1, r2, payload, pattern), seed, duration_ms))
                meta.append((campaign, name, (r1, r2), payload, pattern, hw[name]))
    rows: dict[tuple[str, str], dict] = {}
    with ProcessPoolExecutor(workers) as ex:
        for (campaign, name, rates, payload, pattern, hw), run in zip(
            meta, ex.map(_testbed, jobs), strict=True
        ):
            row = rows.setdefault(
                (campaign, name),
                {
                    "campaign": campaign,
                    "case": name,
                    "rates": rates,
                    "payload": payload,
                    "pattern": pattern,
                    "hardware": hw,
                    "runs": [],
                },
            )
            row["runs"].append(run)
            if len(row["runs"]) == len(tuple(seeds)):
                s = [r["streams"] for r in row["runs"]]
                log(
                    f"{campaign} {name:13s} loss {_pct(s, 0)} / {_pct(s, 1)}"
                    f"  hw {_hw(hw['loss'][0])} / {_hw(hw['loss'][1])}"
                    f"  light run {statistics.fmean(x[1]['mean_run'] for x in s):.2f}"
                    f" hw {hw['mean_run'][1]}"
                )
    return list(rows.values())


def _pct(per_seed: list[list[dict]], i: int) -> str:
    return f"{100 * statistics.fmean(s[i]['loss'] for s in per_seed):.3f}"


def _hw(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.3f}"


def _run(x: float | None) -> str:
    return "–" if x is None else f"{x:.2f}"


def one_sender(seeds: Iterable[int] = SEEDS, duration_ms: float = 30_000) -> dict:
    """One Pi at 8 090/s with the Waveshare: ADR-0016 (P = 710) against
    Part 2 (P = 740, α = 24), its loss and mean run."""
    out: dict = {}
    for label, kw in (("adr_0016", ADR_0016), ("part_2", LS108GP)):
        runs = []
        for seed in seeds:
            sc = pool.testbed(
                kw, [[(8_090, 1_518, "poisson")]], duration_ms, seed, 100, 10, 10, True
            )
            with switch_drop.audit() as found:
                backends.get("simpy").run(sc)
            runs.append(_streams(found, (0,), {0: 1_518})[0])
        out[label] = {
            "loss": statistics.fmean(r["loss"] for r in runs),
            "mean_run": statistics.fmean(r["mean_run"] for r in runs),
        }
    return out


# --- the run and its summary ---


def run_light_port(
    duration_ms: float = DURATION_MS,
    seeds: tuple[int, ...] = SEEDS,
    label: str = "light-port",
    out: Path | None = None,
    data: Path = DATA,
    quick_model: Path = QUICK_MODEL,
    log: Callable[[str], None] = print,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-simpy-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {
        "format": "smolpy-light-port",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "switch": {k: str(v) for k, v in LS108GP.items()},
        "duration_ms": duration_ms,
        "seeds": list(seeds),
    }
    doc["one_sender"] = one_sender(seeds)
    log(f"one sender: {doc['one_sender']}")
    out.write_text(json.dumps(doc, indent=1))
    blind = CAMPAIGNS[0]
    cases = plan(blind, data)
    doc["quick_model"] = against_quick_model(
        quick_model,
        list(cases.values()),
        seeds=len(seeds),
        seconds=duration_ms / 1_000,
        names=list(cases),
    )
    out.write_text(json.dumps(doc, indent=1))
    doc["validation"] = validate(LS108GP, CAMPAIGNS, seeds, duration_ms, data, log=log)
    out.write_text(json.dumps(doc, indent=1))
    return out


def _mae(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """The mean absolute error and the mean error (bias) of (model, hardware)."""
    return (
        statistics.fmean(abs(a - b) for a, b in pairs),
        statistics.fmean(a - b for a, b in pairs),
    )


def light_index(row: dict) -> int:
    """Which Pi is the light one: the fewer cells per second."""
    r1, r2 = row["rates"]
    return 0 if r1 * 12 < r2 * math.ceil(frame(row["payload"]) / CELL) else 1


def summarize(path: Path) -> str:
    doc = json.loads(path.read_text())
    lines = []
    one = doc.get("one_sender")
    if one:
        a, b = one["adr_0016"], one["part_2"]
        lines += [
            "**One Pi at 8 090/s** (with the Waveshare):",
            f"ADR-0016 {100 * a['loss']:.3f} %, run {a['mean_run']:.2f};"
            f" Part 2 {100 * b['loss']:.3f} %, run {b['mean_run']:.2f}.",
            "",
        ]
    quick = doc.get("quick_model")
    if quick:
        lines += [
            "**Against the quick model** (same streams), loss % and mean run, SMOLPy / quick:",
            "",
            "| Case | heavy loss | light loss | light run | heavy run |",
            "|---|---|---|---|---|",
        ]
        z = []
        for row in quick:
            h, li = row["heavy"], row["light"]

            def cell(x: dict, key: str, scale: float = 1.0, fmt: str = ".3f") -> str:
                s, q = x["smolpy"], x["quick"]
                return f"{scale * s[key]:{fmt}} / {scale * q[key]:{fmt}}"

            lines.append(
                f"| {row['case']} | {cell(h, 'loss', 100)} | {cell(li, 'loss', 100)}"
                f" | {cell(li, 'mean_run', fmt='.2f')} | {cell(h, 'mean_run', fmt='.2f')} |"
            )
            for x in (h, li):
                s, q = x["smolpy"], x["quick"]
                se = math.hypot(s["loss_se"], q["loss_se"])
                if se > 0:
                    z.append((s["loss"] - q["loss"]) / se)
        if z:
            lines += [
                "",
                f"Loss differences in standard errors: mean {statistics.fmean(z):+.2f},"
                f" |z| ≤ 2 in {sum(abs(x) <= 2 for x in z)} of {len(z)}.",
                "",
            ]
    rows = doc.get("validation", [])
    for campaign in dict.fromkeys(r["campaign"] for r in rows):
        sel = [r for r in rows if r["campaign"] == campaign]
        lines += [
            f"**{campaign}**, loss % and mean run, model (mean of seeds) / hardware:",
            "",
            "| Case | Pi 1 loss | Pi 2 loss | Pi 1 run | Pi 2 run | Waveshare polls |",
            "|---|---|---|---|---|---|",
        ]
        light_loss, heavy_loss, light_run = [], [], []
        for r in sel:
            runs = [x["streams"] for x in r["runs"]]
            model_loss = [statistics.fmean(s[i]["loss"] for s in runs) for i in (0, 1)]
            model_run = [statistics.fmean(s[i]["mean_run"] for s in runs) for i in (0, 1)]
            hw = r["hardware"]
            ws = sum(x["waveshare"]["affected"] for x in r["runs"])
            lines.append(
                f"| {r['case']} | {100 * model_loss[0]:.3f} / {_hw(hw['loss'][0])}"
                f" | {100 * model_loss[1]:.3f} / {_hw(hw['loss'][1])}"
                f" | {model_run[0]:.2f} / {_run(hw['mean_run'][0])}"
                f" | {model_run[1]:.2f} / {_run(hw['mean_run'][1])} | {ws:g} |"
            )
            li = light_index(r)
            if hw["loss"][li] is not None and hw["loss"][1 - li] is not None:
                light_loss.append((100 * model_loss[li], 100 * hw["loss"][li]))
                heavy_loss.append((100 * model_loss[1 - li], 100 * hw["loss"][1 - li]))
            if hw["mean_run"][li] is not None:
                light_run.append((model_run[li], hw["mean_run"][li]))
        parts = []
        for what, pairs, unit in (
            ("light loss", light_loss, " pp"),
            ("heavy loss", heavy_loss, " pp"),
            ("light run", light_run, ""),
        ):
            if pairs:
                mae, bias = _mae(pairs)
                parts.append(f"{what} {mae:.3f}{unit} (bias {bias:+.3f})")
        lines += ["", f"MAE over {len(sel)} cases: " + "; ".join(parts) + ".", ""]
    return "\n".join(lines)
