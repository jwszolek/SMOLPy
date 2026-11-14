from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from smolpy.core.node import Node

MetricName = Literal[
    "throughput",
    "latency",
    "frame_loss",
    "queue_depth",
    "utilization",
    "bytes_sent",
    "bytes_received",
    "broker_queue",
    "modbus_latency",
    "pn_latency",
    "pn_jitter",
    "pn_watchdog_expiries",
]


@dataclass
class Observation:
    metric: MetricName
    target: Node
    interval_ms: float
