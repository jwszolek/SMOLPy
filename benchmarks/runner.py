"""Benchmark orchestration: one subprocess per measurement (ADR-0006)."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
from importlib import metadata
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
SIZES = {"100k": 100_000, "1M": 1_000_000, "10M": 10_000_000}


def parse_size(text: str) -> int:
    if text in SIZES:
        return SIZES[text]
    return int(text)


def size_label(frames: int) -> str:
    return next((k for k, v in SIZES.items() if v == frames), str(frames))


def default_reps(frames: int) -> int:
    return 3 if frames >= 10_000_000 else 5


def _read(path: str, key: str) -> str | None:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(key):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, cwd=Path(__file__).parent
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def environment() -> dict:
    mem = _read("/proc/meminfo", "MemTotal")
    return {
        "host": socket.gethostname(),
        "os": platform.platform(),
        "cpu": _read("/proc/cpuinfo", "model name") or platform.processor(),
        "cpu_count": os.cpu_count(),
        "memory": mem,
        "python": platform.python_version(),
        "simpy": metadata.version("simpy"),
        "smolpy": metadata.version("smolpy"),
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
    }


def run_one(scenario: str, frames: int, metrics: str, seed: int, timeout_s: float) -> dict:
    cmd = [
        sys.executable, "-m", "benchmarks.worker",
        "--scenario", scenario, "--frames", str(frames), "--metrics", metrics, "--seed", str(seed),
    ]  # fmt: skip
    base = {"scenario": scenario, "frames_target": frames, "metrics": metrics, "seed": seed}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, cwd=Path(__file__).parent.parent
        )
    except subprocess.TimeoutExpired:
        return {**base, "status": "timeout", "timeout_s": timeout_s}
    if proc.returncode != 0:
        return {**base, "status": "error", "stderr": proc.stderr[-2000:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_suite(
    scenarios: list[str],
    sizes: list[int],
    metrics: list[str],
    reps: int | None,
    timeout_s: float,
    seed: int,
    label: str,
    out: Path | None,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-simpy-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": "smolpy-bench",
        "version": 1,
        "label": label,
        "backend": "simpy",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "runs": [],
    }
    for frames in sizes:
        for scenario in scenarios:
            for m in metrics:
                n = reps or default_reps(frames)
                for rep in range(1, n + 1):
                    record = run_one(scenario, frames, m, seed, timeout_s)
                    record["rep"] = rep
                    doc["runs"].append(record)
                    out.write_text(json.dumps(doc, indent=1))  # keep partial progress
                    _print_progress(record, rep, n)
                    if record["status"] == "timeout":
                        break  # further repetitions would time out as well
    return out


def _print_progress(r: dict, rep: int, n: int) -> None:
    size = size_label(r["frames_target"])
    head = f"{r['scenario']} {size:>4} metrics={r['metrics']:<3} rep {rep}/{n}"
    if r["status"] != "ok":
        print(f"{head}  {r['status'].upper()}", flush=True)
        return
    print(
        f"{head}  run {r['run_s']:8.2f}s  {r['events_per_s']:>9,.0f} ev/s  "
        f"{r['frames_per_s']:>8,.0f} fr/s  peak {r['peak_rss_kb'] / 1024:7.1f} MB  "
        f"{r['output_hash']}",
        flush=True,
    )


def summarize(path: Path) -> str:
    """Markdown table: median and range per scenario / size / metrics variant."""
    doc = json.loads(path.read_text())
    groups: dict[tuple, list[dict]] = {}
    for r in doc["runs"]:
        groups.setdefault((r["scenario"], r["frames_target"], r["metrics"]), []).append(r)

    lines = [
        "| Scenario | Frames | Metrics | Runs | Wall time (median, min–max) | Events "
        "| Events/s | Frames/s | Peak RSS | Output |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]  # fmt: skip
    order = sorted(groups.items(), key=lambda g: (g[0][1], g[0][0], g[0][2]))
    for (scenario, frames, metrics), runs in order:
        ok = [r for r in runs if r["status"] == "ok"]
        label = f"| {scenario} | {size_label(frames)} | {metrics} |"
        if not ok:
            lines.append(f"{label} {len(runs)} | {runs[0]['status']} | | | | | |")
            continue
        times = [r["run_s"] for r in ok]
        hashes = {r["output_hash"] for r in ok}
        lines.append(
            f"{label} {len(ok)} | {statistics.median(times):.2f} s "
            f"({min(times):.2f}–{max(times):.2f}) "
            f"| {ok[0]['events']:,} "
            f"| {statistics.median(r['events_per_s'] for r in ok):,.0f} "
            f"| {statistics.median(r['frames_per_s'] for r in ok):,.0f} "
            f"| {statistics.median(r['peak_rss_kb'] for r in ok) / 1024:,.0f} MB "
            f"| {'identical' if len(hashes) == 1 else 'DIFFERS'} |"
        )
    return "\n".join(lines)
