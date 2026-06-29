from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smolpy.dsl.node import Node


@dataclass
class Link:
    """Point-to-point cable between two nodes."""

    endpoint_a: Node
    endpoint_b: Node
    speed: float        # Mb/s
    length: float       # metres
    duplex: bool = True

    @property
    def speed_bps(self) -> float:
        return self.speed * 1_000_000

    @property
    def propagation_delay_us(self) -> float:
        """One-way propagation delay in microseconds."""
        return (self.length / 2e8) * 1_000_000
