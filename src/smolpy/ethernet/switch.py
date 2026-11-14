from __future__ import annotations

from typing import Literal

from smolpy.core.node import Node

SwitchMode = Literal["store-and-forward", "cut-through"]
SwitchQoS = Literal["strict", "fifo"]
MacTableMode = Literal["learning", "topology"]


class Switch(Node):
    """Layer-2 switch with MAC table and per-port queues.

    qos="strict" serves 802.1Q priority classes strictly in order on every
    egress port; qos="fifo" ignores frame priority.

    mac_table="learning" starts with only directly attached stations known
    and learns the rest from source addresses, flooding unknown destinations
    meanwhile. mac_table="topology" starts with the port towards every end
    station already known (shortest path), i.e. the converged state, and
    keeps learning on top of it.
    """

    def __init__(
        self,
        name: str,
        ports: int,
        mode: SwitchMode = "store-and-forward",
        qos: SwitchQoS = "strict",
        mac_table: MacTableMode = "learning",
    ) -> None:
        super().__init__(name)
        if qos not in ("strict", "fifo"):
            raise ValueError(f"invalid qos {qos!r} — expected 'strict' or 'fifo'")
        if mac_table not in ("learning", "topology"):
            raise ValueError(f"invalid mac_table {mac_table!r} — expected 'learning' or 'topology'")
        self.ports = ports
        self.mode = mode
        self.qos = qos
        self.mac_table_mode = mac_table
