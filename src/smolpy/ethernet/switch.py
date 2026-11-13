from __future__ import annotations

from typing import Literal

from smolpy.core.node import Node

SwitchMode = Literal["store-and-forward", "cut-through"]
SwitchQoS = Literal["strict", "fifo"]


class Switch(Node):
    """Layer-2 switch with MAC table and per-port queues.

    qos="strict" serves 802.1Q priority classes strictly in order on every
    egress port; qos="fifo" ignores frame priority.
    """

    def __init__(
        self,
        name: str,
        ports: int,
        mode: SwitchMode = "store-and-forward",
        qos: SwitchQoS = "strict",
    ) -> None:
        super().__init__(name)
        if qos not in ("strict", "fifo"):
            raise ValueError(f"invalid qos {qos!r} — expected 'strict' or 'fifo'")
        self.ports = ports
        self.mode = mode
        self.qos = qos
        self.mac_table: dict[str, int] = {}  # mac -> port index
