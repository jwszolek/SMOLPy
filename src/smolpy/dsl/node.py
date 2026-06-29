from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smolpy.dsl.link import Link


class Node(ABC):
    """Base class for all network nodes (Adapter, Switch, Hub)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.links: list[Link] = []

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.name!r})"
