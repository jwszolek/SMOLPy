"""Many independent runs of one or more scenarios (ADR-0009).

``run_many`` runs replications (the same scenario with different seeds) and
sweeps (several scenarios), in parallel worker processes. Each worker is a
``python -m smolpy._worker`` process that receives scenarios as IR JSON
(ADR-0007): no builder objects cross processes, and the user's script is
never imported again, so it needs no ``if __name__ == "__main__"`` guard.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import math
import os
import pickle
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from smolpy.core.network import Network, RunStats, SimulationResult
from smolpy.model import RunConfig, Scenario
from smolpy.stats import MeanCI, mean_ci

BACKENDS = ("simpy",)

Samples = dict[str, list[tuple[float, float]]]


@dataclass(frozen=True)
class Run:
    """One run of a batch: scenario *scenario* (its index in the batch),
    replication *index*, with *seed*. Exactly one of *result* and *error* is
    set; *error* holds the traceback of a failed run."""

    scenario: int
    index: int
    seed: int
    result: SimulationResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class BatchResult:
    """Every run of a batch, ordered by scenario, then replication."""

    scenarios: tuple[Scenario, ...]
    runs: tuple[Run, ...]

    @property
    def failures(self) -> list[Run]:
        return [r for r in self.runs if not r.ok]

    def results(self, scenario: int = 0) -> list[SimulationResult]:
        """The results of the successful runs of one scenario, in run order."""
        if not 0 <= scenario < len(self.scenarios):
            raise IndexError(
                f"scenario {scenario} out of range: the batch has {len(self.scenarios)}"
            )
        return [r.result for r in self.runs if r.scenario == scenario and r.result is not None]

    def raise_on_failure(self) -> None:
        """Raise if any run failed, naming each failed run and its seed."""
        if self.failures:
            lines = [
                f"scenario {r.scenario} run {r.index} (seed {r.seed}):\n{r.error}"
                for r in self.failures
            ]
            raise RuntimeError(f"{len(lines)} of {len(self.runs)} runs failed\n" + "\n".join(lines))

    def summary(
        self, scenario: int = 0, level: float = 0.95
    ) -> dict[str, list[tuple[float, MeanCI]]]:
        """Per metric key and sample time, the mean across the successful runs
        of *scenario*, with its standard deviation and confidence interval.

        Same shape as ``SimulationResult.metrics``. A window metric has no
        sample for a window without frames, so ``n`` is the number of runs
        that do have the sample, which can be fewer than the runs.
        """
        by_time: dict[str, dict[float, list[float]]] = {}
        for result in self.results(scenario):
            for key, samples in result.metrics.items():
                windows = by_time.setdefault(key, {})
                for t, v in samples:
                    windows.setdefault(t, []).append(v)
        return {
            key: [(t, mean_ci(vs, level)) for t, vs in sorted(windows.items())]
            for key, windows in sorted(by_time.items())
        }

    def scalar(
        self,
        metric: str,
        reduce: str | Callable[[list[float]], float] = "mean",
        scenario: int = 0,
    ) -> list[float]:
        """One number per successful run of *scenario*: its *metric* samples
        reduced by ``"mean"``, ``"min"``, ``"max"``, ``"last"``, ``"sum"`` or
        a function. A run without samples gives NaN (a reducing function is
        then not called). ``mean_ci(batch.scalar(...))`` gives the interval.
        """
        if isinstance(reduce, str):
            if reduce not in _REDUCERS:
                raise ValueError(
                    f"unknown reduce {reduce!r}, expected one of {', '.join(_REDUCERS)}"
                    " or a function"
                )
            fn = _REDUCERS[reduce]
        else:
            fn = reduce
        results = self.results(scenario)
        known = sorted({key for r in results for key in r.metrics})
        if results and metric not in known:
            raise KeyError(f"no metric {metric!r} in the runs; observed: {', '.join(known)}")
        out = []
        for r in results:
            values = [v for _, v in r.metrics.get(metric, [])]
            out.append(float(fn(values)) if values else math.nan)
        return out

    def export(self, path: str | os.PathLike[str], format: str | None = None) -> None:
        """Write every run to *path*, the format inferred from the extension
        unless *format* is given.

        - ``"csv"``: long format, one row per sample, with columns
          ``scenario, run, seed, time_ms, metric, value, error``. A failed
          run is one row with only its ``error`` (the last traceback line).
        - ``"json"``: ``{"scenarios": [names], "runs": [{scenario, run, seed,
          error, metrics}]}`` with the full traceback of failed runs.
        """
        p = Path(path)
        fmt = (format or p.suffix.lstrip(".")).lower()
        if fmt == "csv":
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["scenario", "run", "seed", "time_ms", "metric", "value", "error"])
                for r in self.runs:
                    if r.result is None:
                        last = (r.error or "").strip().splitlines()[-1:] or [""]
                        w.writerow([r.scenario, r.index, r.seed, "", "", "", last[0]])
                        continue
                    for key in sorted(r.result.metrics):
                        for t, v in r.result.metrics[key]:
                            w.writerow([r.scenario, r.index, r.seed, repr(t), key, repr(v), ""])
        elif fmt == "json":
            payload = {
                "scenarios": [sc.name for sc in self.scenarios],
                "runs": [
                    {
                        "scenario": r.scenario,
                        "run": r.index,
                        "seed": r.seed,
                        "error": r.error,
                        "metrics": None if r.result is None else r.result.metrics,
                    }
                    for r in self.runs
                ],
            }
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
        else:
            raise ValueError(f"unknown format {fmt!r}, expected 'csv' or 'json'")


def _last(values: list[float]) -> float:
    return values[-1]


_REDUCERS: dict[str, Callable[[list[float]], float]] = {
    "mean": lambda vs: math.fsum(vs) / len(vs),
    "min": min,
    "max": max,
    "last": _last,
    "sum": math.fsum,
}


def run_many(
    source: Network | Scenario | Sequence[Scenario],
    runs: int = 1,
    *,
    seeds: Sequence[int] | None = None,
    duration: float | None = None,
    backend: str = "simpy",
    workers: int | None = None,
) -> BatchResult:
    """Run every scenario of *source* *runs* times and collect the results.

    - *source*: a ``Network``, a ``Scenario``, or a list of scenarios (a sweep).
    - *duration* (ms) overrides the scenarios' run duration; it is required
      when a scenario has none, e.g. for a ``Network``.
    - *seeds*: one seed per run. By default ``base, base + 1, ...`` where
      *base* is the scenario's own seed, so a batch is reproducible and every
      scenario of a sweep sees the same seeds.
    - *workers*: worker processes, by default one per CPU; ``1`` runs
      everything in this process.

    An invalid scenario raises before anything runs. A run that fails while
    running is recorded in the result with its seed and traceback, and the
    other runs go on; see :meth:`BatchResult.raise_on_failure`.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}, expected one of {', '.join(BACKENDS)}")
    _positive("runs", runs)
    if workers is not None:
        _positive("workers", workers)
    if seeds is not None:
        seeds = list(seeds)
        if len(seeds) != runs:
            raise ValueError(f"{len(seeds)} seeds given for {runs} runs")
        if not all(_is_int(s) for s in seeds):
            raise ValueError(f"seeds must be integers, got {seeds!r}")

    scenarios = tuple(_with_run(sc, duration) for sc in _scenarios(source, duration))

    from smolpy.sim.engine import check_runnable

    for sc in scenarios:
        check_runnable(sc)

    tasks = [
        (i, k, seed)
        for i, sc in enumerate(scenarios)
        for k, seed in enumerate(seeds or [_base_seed(sc) + k for k in range(runs)])
    ]
    n_workers = min(workers or os.cpu_count() or 1, len(tasks))

    texts = [sc.to_json() for sc in scenarios]
    if n_workers == 1:
        from smolpy._worker import run_task

        outcomes = {(i, k): run_task(texts[i], seed) for i, k, seed in tasks}
    else:
        outcomes = _run_in_workers(texts, tasks, n_workers)

    batch_runs = []
    for i, k, seed in tasks:
        outcome = outcomes[i, k]
        if isinstance(outcome, str):
            batch_runs.append(Run(i, k, seed, error=outcome))
        else:
            metrics, stats = outcome
            sc = _seeded(scenarios[i], seed)
            batch_runs.append(Run(i, k, seed, result=SimulationResult(metrics, sc, stats)))
    return BatchResult(scenarios, tuple(batch_runs))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive(name: str, value: object) -> None:
    if not _is_int(value) or value < 1:  # type: ignore[operator]
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _base_seed(scenario: Scenario) -> int:
    assert scenario.run is not None
    return scenario.run.seed


def _scenarios(
    source: Network | Scenario | Sequence[Scenario], duration: float | None
) -> list[Scenario]:
    if isinstance(source, Network):
        return [source.to_scenario(duration=duration)]
    if isinstance(source, Scenario):
        return [source]
    if isinstance(source, Sequence) and not isinstance(source, str):
        items = list(source)
        if not items:
            raise ValueError("run_many needs at least one scenario")
        for item in items:
            if not isinstance(item, Scenario):
                raise TypeError(f"expected Scenario, got {type(item).__name__}")
        return items
    raise TypeError(
        f"expected Network, Scenario or a list of Scenarios, got {type(source).__name__}"
    )


def _with_run(scenario: Scenario, duration: float | None) -> Scenario:
    if duration is not None:
        seed = scenario.run.seed if scenario.run else RunConfig(duration).seed
        return dataclasses.replace(scenario, run=RunConfig(duration, seed))
    if scenario.run is None:
        raise ValueError(f"scenario {scenario.name!r} has no run configuration; pass duration=")
    return scenario


def _seeded(scenario: Scenario, seed: int) -> Scenario:
    assert scenario.run is not None
    return dataclasses.replace(scenario, run=dataclasses.replace(scenario.run, seed=seed))


Outcome = tuple[Samples, RunStats] | str  # metrics and statistics, or a traceback


def _run_in_workers(
    texts: list[str], tasks: list[tuple[int, int, int]], n_workers: int
) -> dict[tuple[int, int], Outcome]:
    """Run *tasks* on *n_workers* worker processes, each fed by one thread."""
    pending: queue.SimpleQueue[tuple[int, int, int]] = queue.SimpleQueue()
    for task in tasks:
        pending.put(task)
    outcomes: dict[tuple[int, int], Outcome] = {}
    errors: list[BaseException] = []

    def feed() -> None:
        try:
            worker = _Worker()
        except BaseException as exc:  # e.g. the interpreter cannot be started
            errors.append(exc)
            return
        try:
            while True:
                try:
                    i, k, seed = pending.get_nowait()
                except queue.Empty:
                    return
                outcomes[i, k] = worker.run(i, texts[i], seed)
        finally:
            worker.close()

    threads = [threading.Thread(target=feed, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if len(outcomes) < len(tasks):
        raise RuntimeError("worker processes could not be started") from (
            errors[0] if errors else None
        )
    return outcomes


class _Worker:
    """One ``python -m smolpy._worker`` process. Sends each scenario once and
    starts a new process if the current one dies."""

    def __init__(self) -> None:
        self._start()

    def _start(self) -> None:
        env = dict(os.environ)
        # Find this smolpy even when it was put on sys.path by hand.
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [root, env.get("PYTHONPATH")]))
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "smolpy._worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )
        self._sent: set[int] = set()

    def _pipes(self) -> tuple[IO[bytes], IO[bytes]]:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        return self._proc.stdin, self._proc.stdout

    def run(self, scenario: int, text: str, seed: int) -> Outcome:
        tasks, results = self._pipes()
        try:
            known = scenario in self._sent
            pickle.dump((scenario, None if known else text, seed), tasks)
            tasks.flush()
            self._sent.add(scenario)
            outcome: Outcome = pickle.load(results)
            return outcome
        except (EOFError, OSError, pickle.UnpicklingError):
            code = self._discard()
            self._start()
            return f"worker process died during the run (exit code {code})\n"

    def _discard(self) -> int:
        """Close the pipes of a dead process; return its exit code."""
        tasks, results = self._pipes()
        try:
            tasks.close()
        except OSError:  # unflushed bytes for a process that is gone
            pass
        results.close()
        return self._proc.wait()

    def close(self) -> None:
        tasks, _ = self._pipes()
        try:
            pickle.dump(None, tasks)
            tasks.flush()
        except OSError:
            pass
        self._discard()
