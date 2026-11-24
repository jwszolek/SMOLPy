"""SMOLPy benchmark suite (ADR-0006).

python -m benchmarks run [--scenario B1 B2 B3] [--frames 100k 1M 10M] [--metrics on off]
    [--backend simpy|rust]
python -m benchmarks summarize benchmarks/results/<file>.json
python -m benchmarks batch --scenario B3 --frames 1M --runs 16 --jobs 8 [--backend rust]
python -m benchmarks profile --scenario B1 --frames 1M --tool cprofile|tracemalloc|py-spy
python -m benchmarks load [--loads 0.5 0.9 1.0 1.2] [--duration 20000] [--backend simpy ns3]
python -m benchmarks load-summary benchmarks/results/<file>.json
python -m benchmarks loss [--loads 0.93 0.996] [--duration 30000] [--backend simpy rust ns3]
python -m benchmarks loss-summary benchmarks/results/<file>.json
python -m benchmarks closed-loop [--loads 0.99] [--duration 30000] [--backend simpy rust ns3]
python -m benchmarks closed-loop-summary benchmarks/results/<file>.json
python -m benchmarks ingress [--loads 0.997] [--duration 30000] [--backend simpy rust ns3]
python -m benchmarks ingress-summary benchmarks/results/<file>.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from benchmarks import closed_loop, ingress, loss
from benchmarks.load import DURATION_MS, LOADS, run_load, summarize_load
from benchmarks.runner import parse_size, run_batch, run_suite, size_label, summarize

PROFILES_DIR = Path(__file__).parent / "profiles"


def _profile(scenario: str, frames: int, metrics: str, tool: str) -> None:
    PROFILES_DIR.mkdir(exist_ok=True)
    stem = PROFILES_DIR / f"{scenario}-{size_label(frames)}-metrics-{metrics}-{tool}"
    worker = [
        "-m", "benchmarks.worker", "--scenario", scenario,
        "--frames", str(frames), "--metrics", metrics,
    ]  # fmt: skip
    if tool == "py-spy":
        exe = shutil.which("py-spy")
        if exe is None:
            sys.exit(
                "py-spy is not installed — e.g. `uv tool install py-spy`, or use --tool cprofile"
            )
        out = stem.with_suffix(".svg")
        cmd = [exe, "record", "--rate", "250", "-o", str(out), "--", sys.executable, *worker]
        subprocess.run(cmd, check=True)
    else:
        out = stem.with_suffix(".txt")
        subprocess.run([sys.executable, *worker, "--profile", tool, "--out", str(out)], check=True)
    print(f"profile written to {out}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m benchmarks")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the benchmark suite")
    run.add_argument("--scenario", nargs="+", default=["B1", "B2", "B3"])
    run.add_argument("--frames", nargs="+", default=["100k", "1M", "10M"])
    run.add_argument("--metrics", nargs="+", default=["off", "on"], choices=("on", "off"))
    run.add_argument("--reps", type=int, default=None, help="default: 5 (3 at 10M frames)")
    run.add_argument("--timeout", type=float, default=3600, help="per run, seconds")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--label", default="run")
    run.add_argument("--out", type=Path, default=None)
    run.add_argument("--backend", default="simpy", help="simpy or rust")

    summ = sub.add_parser("summarize", help="markdown summary of a results file")
    summ.add_argument("file", type=Path)

    batch = sub.add_parser("batch", help="time a batch of independent runs in parallel")
    batch.add_argument("--scenario", default="B3")
    batch.add_argument("--frames", default="1M")
    batch.add_argument("--metrics", default="off", choices=("on", "off"))
    batch.add_argument("--runs", type=int, default=16)
    batch.add_argument("--jobs", type=int, default=8)
    batch.add_argument("--timeout", type=float, default=3600, help="per run, seconds")
    batch.add_argument("--seed", type=int, default=42)
    batch.add_argument("--backend", default="simpy", help="simpy or rust")

    prof = sub.add_parser("profile", help="profile one scenario")
    prof.add_argument("--scenario", default="B1")
    prof.add_argument("--frames", default="1M")
    prof.add_argument("--metrics", default="off", choices=("on", "off"))
    prof.add_argument("--tool", default="cprofile", choices=("cprofile", "tracemalloc", "py-spy"))

    load = sub.add_parser("load", help="Modbus and MQTT on ns-3's TCP against SimPy, under load")
    load.add_argument("--loads", nargs="+", type=float, default=list(LOADS))
    load.add_argument("--duration", type=float, default=DURATION_MS, help="simulated ms")
    load.add_argument("--backend", nargs="+", default=["simpy", "ns3"])
    load.add_argument("--label", default="load")
    load.add_argument("--out", type=Path, default=None)

    load_summ = sub.add_parser("load-summary", help="markdown table of a load results file")
    load_summ.add_argument("file", type=Path)

    lossy = sub.add_parser("loss", help="the testbed of ADR-0011, bounded buffers, every engine")
    lossy.add_argument("--loads", nargs="+", type=float, default=list(loss.LOADS))
    lossy.add_argument("--duration", type=float, default=loss.DURATION_MS, help="simulated ms")
    lossy.add_argument("--backend", nargs="+", default=list(loss.ENGINES))
    lossy.add_argument("--label", default="bounded-buffers")
    lossy.add_argument("--out", type=Path, default=None)

    loss_summ = sub.add_parser("loss-summary", help="markdown table of a loss results file")
    loss_summ.add_argument("file", type=Path)

    closed = sub.add_parser(
        "closed-loop", help="the testbed of ADR-0012, one Modbus request in flight, every engine"
    )
    closed.add_argument("--loads", nargs="+", type=float, default=list(closed_loop.LOADS))
    closed.add_argument(
        "--duration", type=float, default=closed_loop.DURATION_MS, help="simulated ms"
    )
    closed.add_argument("--backend", nargs="+", default=list(closed_loop.ENGINES))
    closed.add_argument("--label", default="one-request-in-flight")
    closed.add_argument("--out", type=Path, default=None)

    closed_summ = sub.add_parser(
        "closed-loop-summary", help="markdown tables of a closed-loop results file"
    )
    closed_summ.add_argument("file", type=Path)

    ing = sub.add_parser(
        "ingress", help="the testbed of ADR-0013, ingress accounts in frames, every engine"
    )
    ing.add_argument("--loads", nargs="+", type=float, default=list(ingress.LOADS))
    ing.add_argument("--duration", type=float, default=ingress.DURATION_MS, help="simulated ms")
    ing.add_argument("--backend", nargs="+", default=list(ingress.ENGINES))
    ing.add_argument("--model", nargs="+", default=list(ingress.MODELS), choices=ingress.MODELS)
    ing.add_argument("--label", default="ingress-buffers")
    ing.add_argument("--out", type=Path, default=None)

    ing_summ = sub.add_parser("ingress-summary", help="markdown table of an ingress results file")
    ing_summ.add_argument("file", type=Path)

    a = p.parse_args()
    if a.cmd == "run":
        sizes = [parse_size(s) for s in a.frames]
        out = run_suite(
            a.scenario, sizes, a.metrics, a.reps, a.timeout, a.seed, a.label, a.out, a.backend
        )
        print(f"\nresults: {out}\n")
        print(summarize(out))
    elif a.cmd == "summarize":
        print(summarize(a.file))
    elif a.cmd == "load":
        out = run_load(a.loads, a.duration, a.backend, a.label, a.out)
        print(f"\nresults: {out}\n")
        print(summarize_load(out, *a.backend[:2]) if len(a.backend) > 1 else "")
    elif a.cmd == "load-summary":
        print(summarize_load(a.file))
    elif a.cmd == "loss":
        out = loss.run_loss(a.loads, a.duration, a.backend, a.label, a.out)
        print(f"\nresults: {out}\n")
        print(loss.summarize_loss(out))
    elif a.cmd == "loss-summary":
        print(loss.summarize_loss(a.file))
    elif a.cmd == "closed-loop":
        out = closed_loop.run_closed_loop(a.loads, a.duration, a.backend, a.label, a.out)
        print(f"results written to {out}")
        print(closed_loop.summarize_closed_loop(out))
    elif a.cmd == "closed-loop-summary":
        print(closed_loop.summarize_closed_loop(a.file))
    elif a.cmd == "ingress":
        out = ingress.run_ingress(
            a.loads, a.duration, a.backend, a.label, a.out, models=tuple(a.model)
        )
        print(f"results written to {out}")
        print(ingress.summarize_ingress(out))
    elif a.cmd == "ingress-summary":
        print(ingress.summarize_ingress(a.file))
    elif a.cmd == "batch":
        frames = parse_size(a.frames)
        r = run_batch(a.scenario, frames, a.metrics, a.runs, a.jobs, a.timeout, a.seed, a.backend)
        print(json.dumps(r, indent=1))
    else:
        _profile(a.scenario, parse_size(a.frames), a.metrics, a.tool)


if __name__ == "__main__":
    main()
