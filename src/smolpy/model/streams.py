"""Per-source random streams (ADR-0007, step 5; docs/model-semantics.md §6).

Every traffic statement has a stable *source id*, and every engine seeds the
generator of that statement from ``stream_seed(run seed, source id)``. Adding,
removing or reordering one statement does not change the draws of another.
"""

from __future__ import annotations

import hashlib
from collections import Counter

from smolpy.model.scenario import Flow, Poll, ProfinetAR, Publish, Route, Scenario, TrafficSpec


def _ends(t: TrafficSpec) -> tuple[str, str]:
    """The node that runs the statement and the node it is aimed at."""
    if isinstance(t, Flow):
        return t.source, t.destination
    if isinstance(t, Publish):
        return t.source, t.broker
    if isinstance(t, Poll):
        return t.source, t.slave
    if isinstance(t, ProfinetAR):
        return t.controller, t.device
    if isinstance(t, Route):
        return t.broker, t.topic
    raise TypeError(f"unknown traffic statement {t!r}")


def source_ids(scenario: Scenario) -> list[str]:
    """The source id of each traffic statement, in scenario order.

    ``"<kind>:<from>-><to>#<k>"``, where *k* counts the earlier statements of
    the same kind between the same two ends (a route's *to* is its topic).
    """
    seen: Counter[tuple[str, str, str]] = Counter()
    ids = []
    for t in scenario.traffic:
        a, b = _ends(t)
        key = (t.kind, a, b)
        ids.append(f"{t.kind}:{a}->{b}#{seen[key]}")
        seen[key] += 1
    return ids


def stream_seed(run_seed: int, source_id: str) -> int:
    """First 8 bytes, big-endian, of SHA-256 of ``"<run seed>:<source id>"``."""
    digest = hashlib.sha256(f"{run_seed}:{source_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")
