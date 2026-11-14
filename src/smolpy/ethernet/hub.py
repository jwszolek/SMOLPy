from __future__ import annotations

from smolpy.core.node import Node
from smolpy.model import HubNode
from smolpy.model.validate import check_node


class Hub(Node):
    """Layer-1 hub — shared collision domain."""

    def __init__(self, name: str, ports: int) -> None:
        super().__init__(name)
        self.ports = ports
        check_node(self.to_spec())

    def to_spec(self) -> HubNode:
        return HubNode(self.name, self.ports)
