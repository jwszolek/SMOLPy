from __future__ import annotations
from smolpy.dsl.node import Node


class Hub(Node):
    """Layer-1 hub — shared collision domain."""

    def __init__(self, name: str, ports: int) -> None:
        super().__init__(name)
        self.ports = ports
