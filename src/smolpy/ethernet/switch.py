from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from smolpy.core.node import Node
from smolpy.model import SwitchNode
from smolpy.model.validate import ScenarioError, check_node

SwitchMode = Literal["store-and-forward", "cut-through"]
SwitchQoS = Literal["strict", "fifo"]
MacTableMode = Literal["learning", "topology"]
DropScope = Literal["port", "switch"]
DrainCount = Literal["freed", "net"]
PoolCount = Literal["excess", "total"]


@dataclass(frozen=True)
class Frames:
    """A switch buffer limit in frames, whatever their size (ADR-0013):
    ``buffer=Frames(60)``. A plain ``int`` is a limit in bytes."""

    n: int

    def __str__(self) -> str:
        return f"{self.n}frames"


@dataclass(frozen=True)
class Cells:
    """A switch buffer limit in cells of the switch's ``cell`` bytes, a frame
    taking ceil(bytes / cell) of them (ADR-0014): ``ingress_buffer=Cells(740)``
    with ``cell=128``."""

    n: int

    def __str__(self) -> str:
        return f"{self.n}cells"


Limit = int | Frames | Cells


def _split(limit: Limit | None) -> tuple[int | None, int | None, int | None]:
    """A limit as (bytes, frames, cells), at most one of them set."""
    if isinstance(limit, Frames):
        return None, limit.n, None
    if isinstance(limit, Cells):
        return None, None, limit.n
    return limit, None, None


def _resume(name: str, label: str, limit: Limit | None, resume: Limit | None) -> int | None:
    """A resume level as a number in its limit's unit, which it must share."""
    if resume is None:
        return None
    if limit is not None and type(resume) is not type(limit):
        unit = {Frames: "Frames(n)", Cells: "Cells(n)"}.get(type(limit), "an int, in bytes")
        raise ScenarioError(
            f"Switch {name!r}: {label} is given in the unit of its scope's limit:"
            f" {unit}, got {resume!r}"
        )
    return resume.n if isinstance(resume, Frames | Cells) else resume


class Switch(Node):
    """Layer-2 switch with MAC table and per-port queues.

    qos="strict" serves 802.1Q priority classes strictly in order on every
    egress port; qos="fifo" ignores frame priority.

    mac_table="learning" starts with only directly attached stations known
    and learns the rest from source addresses, flooding unknown destinations
    meanwhile. mac_table="topology" starts with the port towards every end
    station already known (shortest path), i.e. the converged state, and
    keeps learning on top of it.

    buffer bounds the frames queued on each port (ADR-0011), and
    ingress_buffer the frames that entered on each port and are still queued
    on any port (ADR-0013). Each is in bytes, an int, in frames, Frames(n),
    or in cells of cell bytes, Cells(n) (ADR-0014); a frame that does not fit
    is dropped. None, the default, is unbounded.

    resume and ingress_resume, in the unit of their limit, make a full
    account drop every frame until a release brings it to that level: a
    resume threshold (ADR-0014). None, the default, is tail drop.

    pool is a buffer shared by the ingress ports, for what each holds beyond
    its ingress_reserve; ingress_drain ends a port's drop state once the
    switch has freed that much (ADR-0015). They share the ingress scope's
    unit with ingress_buffer.

    ingress_drop="switch" gives the switch one drop state, entered at any
    ingress refusal and ended by ingress_drain; in it a frame is admitted
    only if it fits in its port's ingress_reserve (ADR-0016). None, the
    default, is "port": a drop state per ingress port.

    ingress_drain_count="net" ends that state once the switch's ingress
    occupancy is ingress_drain below its value at the entry: what it has
    freed since, less what it has admitted in the state (ADR-0017). None,
    the default, is "freed": once it has freed ingress_drain.

    With ingress_drain_count="net", three more protect a light port (ADR-0017
    Part 2). ingress_pool_count="total" counts the pool's use as the switch's
    whole ingress occupancy less one reserve; None is "excess", each port's
    excess over its reserve. ingress_alpha, a dynamic threshold, admits a
    frame beyond its port's reserve only while the port holds at most the
    reserve plus alpha times the pool's free room. ingress_mark, in the
    ingress unit, marks at the entry of the state the refused port and each
    port holding more; in the state a marked port is refused and an unmarked
    one admitted if its frame fits, or else marked.
    """

    def __init__(
        self,
        name: str,
        ports: int,
        mode: SwitchMode = "store-and-forward",
        qos: SwitchQoS = "strict",
        mac_table: MacTableMode = "learning",
        buffer: Limit | None = None,
        ingress_buffer: Limit | None = None,
        cell: int | None = None,
        resume: Limit | None = None,
        ingress_resume: Limit | None = None,
        pool: Limit | None = None,
        ingress_reserve: Limit | None = None,
        ingress_drain: Limit | None = None,
        ingress_drop: DropScope | None = None,
        ingress_drain_count: DrainCount | None = None,
        ingress_pool_count: PoolCount | None = None,
        ingress_alpha: float | None = None,
        ingress_mark: Limit | None = None,
    ) -> None:
        super().__init__(name)
        self.ports = ports
        self.mode = mode
        self.qos = qos
        self.mac_table_mode = mac_table
        self.buffer = buffer
        self.ingress_buffer = ingress_buffer
        self.cell = cell
        self.resume = resume
        self.ingress_resume = ingress_resume
        self.pool = pool
        self.ingress_reserve = ingress_reserve
        self.ingress_drain = ingress_drain
        self.ingress_drop = ingress_drop
        self.ingress_drain_count = ingress_drain_count
        self.ingress_pool_count = ingress_pool_count
        self.ingress_alpha = ingress_alpha
        self.ingress_mark = ingress_mark
        check_node(self.to_spec())

    def to_spec(self) -> SwitchNode:
        buffer_bytes, buffer_frames, buffer_cells = _split(self.buffer)
        ingress_bytes, ingress_frames, ingress_cells = _split(self.ingress_buffer)
        pool_bytes, pool_frames, pool_cells = _split(self.pool)
        scope = self.ingress_buffer if self.ingress_buffer is not None else self.pool
        return SwitchNode(
            self.name,
            self.ports,
            self.mode,
            self.qos,
            self.mac_table_mode,
            buffer_bytes,
            buffer_frames,
            ingress_bytes,
            ingress_frames,
            self.cell,
            buffer_cells,
            ingress_cells,
            _resume(self.name, "resume", self.buffer, self.resume),
            _resume(self.name, "ingress_resume", self.ingress_buffer, self.ingress_resume),
            pool_bytes,
            pool_frames,
            pool_cells,
            _resume(self.name, "ingress_reserve", scope, self.ingress_reserve),
            _resume(self.name, "ingress_drain", scope, self.ingress_drain),
            self.ingress_drop,
            self.ingress_drain_count,
            self.ingress_pool_count,
            _alpha(self.ingress_alpha),
            _resume(self.name, "ingress_mark", scope, self.ingress_mark),
        )


def _alpha(alpha: float | None) -> float | None:
    """A dynamic threshold as a float, an int given being whole; anything
    else is left for validation."""
    if isinstance(alpha, int) and not isinstance(alpha, bool):
        return float(alpha)
    return alpha
