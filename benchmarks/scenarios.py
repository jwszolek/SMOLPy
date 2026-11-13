"""Benchmark scenarios (ADR-0006).

Every scenario is sized by the number of frames its traffic generators inject,
not by simulated time: the builder computes the simulated duration needed to
reach the target from the configured rates. All generators are deterministic
(constant rates, periodic protocols), so the injected frame count and the
engine's event count do not depend on the seed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from smolpy import Network


@dataclass(frozen=True)
class Built:
    network: Network
    duration_ms: float
    frames_per_s: float  # injected frames per simulated second

    @property
    def frames_expected(self) -> int:
        return round(self.frames_per_s * self.duration_ms / 1_000)


def _duration_ms(target_frames: int, frames_per_s: float) -> float:
    return target_frames / frames_per_s * 1_000


def build_b1(target_frames: int, metrics: bool) -> Built:
    """B1 — single path: one constant flow, sender -> switch -> receiver."""
    rate = 50_000.0  # 512 B frames, ~205 Mb/s on 1 Gb/s links
    net = Network("bench-b1")
    sw = net.switch("sw", ports=4)
    tx = net.adapter("tx", ip="10.0.0.1")
    rx = net.adapter("rx", ip="10.0.0.2")
    net.link(tx, sw, speed=1_000, length=5)
    net.link(rx, sw, speed=1_000, length=5)
    tx.sends(to=rx, rate=rate, size=512, pattern="constant")
    if metrics:
        net.observe("throughput", on=rx, every=100)
        net.observe("latency", on=rx, every=100)
        net.observe("bytes_sent", on=tx, every=100)
        net.observe("queue_depth", on=sw, every=100)
    return Built(net, _duration_ms(target_frames, rate), rate)


def build_b2(target_frames: int, metrics: bool) -> Built:
    """B2 — industrial cell: PROFINET, Modbus, MQTT and bulk traffic on one switch."""
    n = 10
    pn_cycle_ms, modbus_hz, mqtt_hz, bulk_fps = 2, 10.0, 10.0, 2_000.0
    net = Network("bench-b2")
    sw = net.switch("cell", ports=48)
    plc = net.adapter("plc", ip="10.0.0.1")
    broker = net.mqtt_broker("broker", ip="10.0.0.2")
    historian = net.adapter("historian", ip="10.0.0.3")
    eng = net.adapter("eng", ip="10.0.0.4")
    for node in (plc, broker, historian, eng):
        net.link(node, sw, speed=100, length=5)

    sensors = []
    for i in range(1, n + 1):
        dev = net.profinet_device(f"io-{i}", ip=f"10.0.1.{i}")
        slave = net.modbus_slave(f"mb-{i}", ip=f"10.0.2.{i}", unit_id=i)
        sensor = net.adapter(f"sensor-{i}", ip=f"10.0.3.{i}")
        for node in (dev, slave, sensor):
            net.link(node, sw, speed=100, length=15)
        plc.controls(dev, cycle=pn_cycle_ms, inputs=32, outputs=16)
        plc.polls(slave, register=40001, count=10, rate=modbus_hz)
        sensor.publishes(to=broker, topic="plant/temp", rate=mqtt_hz, payload=20, qos=0)
        sensors.append(sensor)
    broker.routes("plant/temp", to=[historian])
    eng.sends(to=historian, rate=bulk_fps, size=1_518, pattern="constant")

    fps = (
        n * 2 * (1_000 / pn_cycle_ms)  # PROFINET: inputs + outputs
        + n * modbus_hz * 2  # Modbus: request + response
        + n * mqtt_hz * 2  # MQTT QoS 0: publish + forward to one subscriber
        + bulk_fps
    )
    if metrics:
        for metric in ("pn_latency", "pn_jitter", "pn_watchdog_expiries", "modbus_latency"):
            net.observe(metric, on=plc, every=100)
        net.observe("broker_queue", on=broker, every=100)
        net.observe("throughput", on=historian, every=100)
        net.observe("latency", on=historian, every=100)
        net.observe("queue_depth", on=sw, every=100)
    return Built(net, _duration_ms(target_frames, fps), fps)


def build_b3(target_frames: int, metrics: bool) -> Built:
    """B3 — large plant: 50 access switches x 20 nodes behind one core switch, paired traffic."""
    n_access, per_access, rate = 50, 20, 20.0
    interval_ms = 1_000 / rate
    net = Network("bench-b3")
    core = net.switch("core", ports=n_access + 2)
    access = []
    nodes: list[list] = []
    for a in range(n_access):
        sw = net.switch(f"acc-{a}", ports=per_access + 2)
        net.link(sw, core, speed=1_000, length=50)
        access.append(sw)
        row = []
        for i in range(per_access):
            node = net.adapter(f"n-{a}-{i}", ip=f"10.{a}.0.{i + 1}")
            net.link(node, sw, speed=100, length=20)
            row.append(node)
        nodes.append(row)
    # Access switches are paired (a <-> a xor 1) and every node exchanges traffic
    # with its counterpart in the paired access switch, so all traffic crosses
    # the core (4 hops). Traffic flows in both directions so every switch learns
    # every destination MAC; one-way traffic would leave the destination unknown
    # to the sender's access switch and flood every frame to all local ports.
    # Start phases are spread over one interval so the 1 000 flows do not fire
    # in lock-step.
    total = n_access * per_access
    for a in range(n_access):
        for i in range(per_access):
            idx = a * per_access + i
            nodes[a][i].sends(
                to=nodes[a ^ 1][i],
                rate=rate,
                size=512,
                pattern="constant",
                delay_ms=interval_ms * idx / total,
            )
    if metrics:
        net.observe("queue_depth", on=core, every=100)
        for a in range(n_access):
            net.observe("queue_depth", on=access[a], every=100)
            net.observe("latency", on=nodes[a][0], every=100)
            net.observe("throughput", on=nodes[a][0], every=100)
    fps = total * rate
    return Built(net, _duration_ms(target_frames, fps), fps)


SCENARIOS: dict[str, Callable[[int, bool], Built]] = {
    "B1": build_b1,
    "B2": build_b2,
    "B3": build_b3,
}
