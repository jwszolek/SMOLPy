from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from smolpy.dsl.node import Node

MetricName = Literal[
    "throughput",
    "latency",
    "frame_loss",
    "collision_rate",
    "queue_depth",
    "utilization",
    "bytes_sent",
    "bytes_received",
    "broker_queue",
]


@dataclass
class Observation:
    metric: MetricName
    target: Node
    interval_ms: float
