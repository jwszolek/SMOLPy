from __future__ import annotations

from smolpy.core.adapter import _derive_mac
from smolpy.core.node import Node


class ModbusSlave(Node):
    """Modbus TCP slave — responds to register reads from a polling master.

    Passive: never initiates traffic. Replies only to requests addressed
    to its own unit_id.
    """

    def __init__(self, name: str, ip: str, unit_id: int, mac: str | None = None) -> None:
        super().__init__(name)
        self.ip = ip
        self.mac = mac or _derive_mac(ip)
        self.unit_id = unit_id
