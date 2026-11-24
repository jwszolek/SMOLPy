from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from smolpy.core.node import Node
from smolpy.model import SwitchNode
from smolpy.model.validate import check_node

SwitchMode = Literal["store-and-forward", "cut-through"]
SwitchQoS = Literal["strict", "fifo"]
MacTableMode = Literal["learning", "topology"]


@dataclass(frozen=True)
class Frames:
    """A switch buffer limit in frames, whatever their size (ADR-0013):
    ``buffer=Frames(60)``. A plain ``int`` is a limit in bytes."""

    n: int

    def __str__(self) -> str:
        return f"{self.n}frames"


Limit = int | Frames


def _split(limit: Limit | None) -> tuple[int | None, int | None]:
    """A limit as (bytes, frames), at most one of them set."""
    if isinstance(limit, Frames):
        return None, limit.n
    return limit, None


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
    on any port (ADR-0013). Each is in bytes, an int, or in frames,
    Frames(n); a frame that does not fit is dropped. None, the default, is
    unbounded.
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
    ) -> None:
        super().__init__(name)
        self.ports = ports
        self.mode = mode
        self.qos = qos
        self.mac_table_mode = mac_table
        self.buffer = buffer
        self.ingress_buffer = ingress_buffer
        check_node(self.to_spec())

    def to_spec(self) -> SwitchNode:
        buffer_bytes, buffer_frames = _split(self.buffer)
        ingress_bytes, ingress_frames = _split(self.ingress_buffer)
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
        )
