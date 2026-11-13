"""SMOLPy benchmark suite (ADR-0006).

python -m benchmarks run [--scenario B1 B2 B3] [--frames 100k 1M 10M] [--metrics on off]
python -m benchmarks summarize benchmarks/results/<file>.json
python -m benchmarks batch --scenario B3 --frames 1M --runs 16 --jobs 8
python -m benchmarks profile --scenario B1 --frames 1M --tool cprofile|tracemalloc|py-spy
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

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

    prof = sub.add_parser("profile", help="profile one scenario")
    prof.add_argument("--scenario", default="B1")
    prof.add_argument("--frames", default="1M")
    prof.add_argument("--metrics", default="off", choices=("on", "off"))
    prof.add_argument("--tool", default="cprofile", choices=("cprofile", "tracemalloc", "py-spy"))

    a = p.parse_args()
    if a.cmd == "run":
        sizes = [parse_size(s) for s in a.frames]
        out = run_suite(a.scenario, sizes, a.metrics, a.reps, a.timeout, a.seed, a.label, a.out)
        print(f"\nresults: {out}\n")
        print(summarize(out))
    elif a.cmd == "summarize":
        print(summarize(a.file))
    elif a.cmd == "batch":
        frames = parse_size(a.frames)
        r = run_batch(a.scenario, frames, a.metrics, a.runs, a.jobs, a.timeout, a.seed)
        print(json.dumps(r, indent=1))
    else:
        _profile(a.scenario, parse_size(a.frames), a.metrics, a.tool)


if __name__ == "__main__":
    main()
