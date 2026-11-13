from __future__ import annotations

from smolpy.core.adapter import _derive_mac
from smolpy.core.node import Node


class ProfinetDevice(Node):
    """PROFINET IO Device — exchanges cyclic RT I/O data with an IO Controller.

    Provides input data and consumes output data every update time once a
    controller has set up an AR with it via Adapter.controls().
    """

    def __init__(self, name: str, ip: str, mac: str | None = None) -> None:
        super().__init__(name)
        self.ip = ip
        self.mac = mac or _derive_mac(ip)
