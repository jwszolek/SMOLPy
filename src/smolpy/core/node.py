from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smolpy.core.link import Link
    from smolpy.model import NodeSpec


class Node(ABC):
    """Base class for all network nodes (Adapter, Switch, ...)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.links: list[Link] = []

    @abstractmethod
    def to_spec(self) -> NodeSpec:
        """This node as a Scenario IR element (ADR-0007)."""

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.name!r})"
