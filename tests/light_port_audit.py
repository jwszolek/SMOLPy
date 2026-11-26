"""An audit of SimPy's protection of a light port (ADR-0017 Part 2).

Every ingress decision of a switch with a switch-wide drop state is checked
against an oracle of the rule, written apart from the engine: the pool's
use by its count, the reserve, the pool's room and the dynamic threshold
outside the state, and in it the marks set at the entry and at a misfit.
The audit keeps its own marks and compares them with the engine's at each
decision in the state, and counts the cases each test needs to have seen.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pytest


@dataclass
class Decision:
    """One ingress decision, as the oracle saw it."""

    time: int
    port: int
    units: int
    held: dict[int, int]  # each port's occupancy before it
    use: int  # the pool's use before it, by its count
    dropping: bool  # in the state when it arrived
    marked: bool  # its port marked before it (in the state)
    ok: bool


@dataclass
class Episode:
    entry: int
    held: dict[int, int]  # each port's occupancy at the entry
    refused: int  # the port whose refusal entered it
    marked: set[int]  # the marks at the entry
    misfits: list[int] = field(default_factory=list)  # ports marked in it


def pool_use(held: dict[int, int], reserve: int, total: bool) -> int:
    if total:
        return max(0, sum(held.values()) - reserve)
    return sum(max(0, h - reserve) for h in held.values())


def oracle_fits(
    held: dict[int, int],
    port: int,
    u: int,
    *,
    cap: int | None,
    pool: int | None,
    reserve: int,
    total: bool,
    alpha: float | None,
) -> bool:
    """Whether a frame of *u* units of *port* fits outside the state."""
    h = held[port]
    if cap is not None and h + u > cap:
        return False
    if pool is None:
        return True
    in_reserve = h + u <= reserve
    use = pool_use(held, reserve, total)
    after = dict(held)
    after[port] = h + u
    if not (in_reserve or pool_use(after, reserve, total) <= pool):
        return False
    if alpha is None or in_reserve:
        return True
    return h + u <= reserve + alpha * (pool - use)


class LightAudit:
    """Install the audit on SimPy's engine for the rest of the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from smolpy.sim import engine

        self.decisions: list[Decision] = []
        self.episodes: list[Episode] = []
        self.seen: Counter[str] = Counter()
        self.max_total: int = 0  # the most the switch held at a decision
        marks: dict[int, bool] = {}
        admit = engine._Account._admit_in_switch
        audit = self

        def checked(account, size: int, state) -> bool:
            sw = account.switch
            ports = sw.accounts
            port = next(p for p, a in ports.items() if a is account)
            held = {p: a.held for p, a in ports.items()}
            u = account.units(size)
            pool = sw.pool
            reserve = 0 if pool is None else pool.reserve
            total = pool is not None and pool.total
            fits = oracle_fits(
                held,
                port,
                u,
                cap=account.limit,
                pool=None if pool is None else pool.limit,
                reserve=reserve,
                total=total,
                alpha=sw.alpha,
            )
            use = 0 if pool is None else pool_use(held, reserve, total)
            if pool is not None:
                assert pool.use() == use
            audit.max_total = max(audit.max_total, sum(held.values()))
            dropping = state.dropping
            was_marked = marks.get(port, False)
            if dropping and sw.mark is not None:
                for p, a in ports.items():
                    assert a.marked == marks.get(p, False), (p, a.marked)
            ok = admit(account, size, state)
            now = account.env._now
            in_reserve = held[port] + u <= reserve
            if not dropping:
                assert ok == fits
                if not ok:
                    marked = (
                        set()
                        if sw.mark is None
                        else {p for p, h in held.items() if h > sw.mark} | {port}
                    )
                    marks.clear()
                    marks.update({p: p in marked for p in ports})
                    audit.episodes.append(Episode(now, held, port, marked))
                    if marked - {port}:
                        audit.seen["marked a port that was not refused"] += 1
                    if sw.alpha is not None and pool is not None:
                        no_alpha = oracle_fits(
                            held, port, u, cap=account.limit, pool=pool.limit,
                            reserve=reserve, total=total, alpha=None,
                        )  # fmt: skip
                        if no_alpha:
                            audit.seen["refused by the dynamic threshold"] += 1
            elif sw.mark is None:
                assert ok == in_reserve
            elif was_marked:
                assert not ok
                if in_reserve:
                    audit.seen["a marked port refused within its reserve"] += 1
            else:
                assert ok == fits
                if ok and not in_reserve:
                    audit.seen["an unmarked port admitted beyond its reserve"] += 1
                if ok:
                    audit.seen["an unmarked port admitted"] += 1
                else:
                    marks[port] = True
                    audit.episodes[-1].misfits.append(port)
                    audit.seen["an unmarked port marked at a misfit"] += 1
            if not dropping and ok and was_marked:
                audit.seen["a port marked in the last state admitted after it"] += 1
            audit.decisions.append(Decision(now, port, u, held, use, dropping, was_marked, ok))
            return ok

        monkeypatch.setattr(engine._Account, "_admit_in_switch", checked)
