"""Worker process of ``run_many`` (ADR-0009): ``python -m smolpy._worker``.

Reads pickled ``(scenario id, scenario JSON or None, seed, backend)`` tasks from
stdin and writes one pickled outcome per task to stdout: ``(metrics, stats)``
or the traceback of a failed run. The JSON is sent with the first task of a
scenario only; later tasks refer to it by id. ``None`` ends the worker.

Being started as its own module, the worker never imports the user's script,
so scripts need no ``__main__`` guard.
"""

from __future__ import annotations

import os
import pickle
import sys
import traceback
from functools import lru_cache
from typing import Any, BinaryIO

from smolpy.model import Scenario

_parse = lru_cache(maxsize=4)(Scenario.from_json)


def run_task(text: str, seed: int, backend: str = "simpy") -> Any:
    """``(metrics, stats)`` of one run, or the traceback if it failed. The
    scenario was checked against *backend* by ``run_many`` before any run."""
    import dataclasses

    from smolpy import backends

    try:
        scenario = _parse(text)
        assert scenario.run is not None
        run = dataclasses.replace(scenario.run, seed=seed)
        result = backends.get(backend).run(dataclasses.replace(scenario, run=run))
    except Exception:
        return traceback.format_exc()
    return result.metrics, result.stats


def serve(tasks: BinaryIO, out: BinaryIO) -> None:
    texts: dict[int, str] = {}
    while True:
        try:
            task = pickle.load(tasks)
        except EOFError:
            return
        if task is None:
            return
        scenario, text, seed, backend = task
        if text is not None:
            texts[scenario] = text
        outcome = run_task(texts[scenario], seed, backend)
        pickle.dump(outcome, out, protocol=pickle.HIGHEST_PROTOCOL)
        out.flush()


def main() -> None:
    # Keep stdout for results only: anything printed during a run goes to stderr.
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    serve(sys.stdin.buffer, out)


if __name__ == "__main__":
    main()
