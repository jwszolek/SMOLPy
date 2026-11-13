"""One benchmark measurement, run in its own process (ADR-0006).

Prints a single JSON record on stdout. Always launched by the runner as a
fresh subprocess so that peak RSS belongs to exactly one scenario run.

    python -m benchmarks.worker --scenario B1 --frames 100000 --metrics on
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time

import simpy

_ENVS: list[simpy.Environment] = []
_AFTER_RUN: list = []  # callbacks run while the engine's state is still alive


class _TrackedEnvironment(simpy.Environment):
    """Registers itself so its event counter can be read after the run.

    SimPy draws one id from ``_eid`` for every scheduled event, so the number
    of processed events is ``next(_eid)`` minus whatever is still queued —
    exact, and with no per-event overhead.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _ENVS.append(self)

    def run(self, until=None):  # type: ignore[no-untyped-def]
        result = super().run(until)
        for callback in _AFTER_RUN:
            callback()
        return result


def _peak_rss_kb() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss // 1024 if sys.platform == "darwin" else rss  # macOS reports bytes


def _events_processed() -> int:
    return sum(next(env._eid) - len(env._queue) for env in _ENVS)


def _hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _canonical_metrics(metrics: dict[str, list[tuple[float, float]]]) -> dict:
    return {k: [[round(t, 6), float(f"{v:.9g}")] for t, v in s] for k, s in metrics.items()}


def _output_hash(events: int, metrics: dict[str, list[tuple[float, float]]]) -> str:
    """Same run, same engine: event count and metrics must both match."""
    return _hash({"events": events, "metrics": _canonical_metrics(metrics)})


def _metrics_hash(metrics: dict[str, list[tuple[float, float]]]) -> str:
    """Engine changes that remove events must still produce the same metrics."""
    return _hash(_canonical_metrics(metrics))


def measure(
    scenario: str, frames: int, metrics: bool, seed: int, profile: str | None, out: str | None
) -> dict:
    """Run one measurement. The engine creates its Environment through the
    ``simpy`` module, so the tracking subclass is installed only for the
    duration of this call."""
    original = simpy.Environment
    simpy.Environment = _TrackedEnvironment  # type: ignore[misc]
    _ENVS.clear()
    _AFTER_RUN.clear()
    try:
        return _measure(scenario, frames, metrics, seed, profile, out)
    finally:
        simpy.Environment = original  # type: ignore[misc]


def _measure(
    scenario: str, frames: int, metrics: bool, seed: int, profile: str | None, out: str | None
) -> dict:
    from benchmarks.scenarios import SCENARIOS

    rss_import_kb = _peak_rss_kb()

    t0 = time.perf_counter()
    built = SCENARIOS[scenario](frames, metrics)
    build_s = time.perf_counter() - t0
    rss_after_build_kb = _peak_rss_kb()

    run = lambda: built.network.simulate(duration=built.duration_ms, seed=seed)  # noqa: E731

    if profile == "cprofile":
        import cProfile
        import pstats

        profiler = cProfile.Profile()
        t0 = time.perf_counter()
        result = profiler.runcall(run)
        run_s = time.perf_counter() - t0
        with open(out, "w") as fh:  # type: ignore[arg-type]
            stats = pstats.Stats(profiler, stream=fh).strip_dirs()
            fh.write("=== sorted by own time (tottime) ===\n")
            stats.sort_stats("tottime").print_stats(30)
            fh.write("\n=== sorted by cumulative time ===\n")
            stats.sort_stats("cumulative").print_stats(30)
    elif profile == "tracemalloc":
        import tracemalloc

        def snapshot() -> None:
            # Taken inside env.run(), while every engine structure is still alive.
            snap = tracemalloc.take_snapshot().filter_traces(
                [tracemalloc.Filter(False, tracemalloc.__file__)]
            )
            current, peak = tracemalloc.get_traced_memory()
            with open(out, "w") as fh:  # type: ignore[arg-type]
                fh.write(
                    f"traced current {current / 2**20:.1f} MiB, peak {peak / 2**20:.1f} MiB\n\n"
                )
                fh.write("=== top allocations by line ===\n")
                for stat in snap.statistics("lineno")[:25]:
                    fh.write(f"{stat}\n")

        _AFTER_RUN.append(snapshot)
        tracemalloc.start()
        t0 = time.perf_counter()
        result = run()
        run_s = time.perf_counter() - t0
    else:
        t0 = time.perf_counter()
        result = run()
        run_s = time.perf_counter() - t0

    events = _events_processed()
    return {
        "scenario": scenario,
        "frames_target": frames,
        "metrics": "on" if metrics else "off",
        "seed": seed,
        "profile": profile,
        "status": "ok",
        "sim_duration_ms": built.duration_ms,
        "frames_expected": built.frames_expected,
        "nodes": len(built.network._nodes),
        "events": events,
        "build_s": build_s,
        "run_s": run_s,
        "events_per_s": events / run_s,
        "frames_per_s": built.frames_expected / run_s,
        "rss_import_kb": rss_import_kb,
        "rss_after_build_kb": rss_after_build_kb,
        "peak_rss_kb": _peak_rss_kb(),
        "output_hash": _output_hash(events, result.metrics),
        "metrics_hash": _metrics_hash(result.metrics),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", required=True)
    p.add_argument("--frames", type=int, required=True)
    p.add_argument("--metrics", choices=("on", "off"), default="off")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--profile", choices=("cprofile", "tracemalloc"), default=None)
    p.add_argument("--out", default=None, help="profile report path")
    a = p.parse_args()
    record = measure(a.scenario, a.frames, a.metrics == "on", a.seed, a.profile, a.out)
    print(json.dumps(record))


if __name__ == "__main__":
    main()
