from __future__ import annotations

from typing import Literal

from smolpy.dsl.node import Node

SwitchMode = Literal["store-and-forward", "cut-through"]


class Switch(Node):
    """Layer-2 switch with MAC table and per-port queues."""

    def __init__(self, name: str, ports: int, mode: SwitchMode = "store-and-forward") -> None:
        super().__init__(name)
        self.ports = ports
        self.mode = mode
        self.mac_table: dict[str, int] = {}  # mac -> port index
