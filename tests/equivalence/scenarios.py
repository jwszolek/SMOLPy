"""Reference scenarios of the equivalence suite.

Together they use every node kind, traffic kind and metric. *DETERMINISTIC*
scenarios have no random draws (constant patterns, fixed sizes), so any two
correct backends agree window by window. *STOCHASTIC* ones use every random
pattern and IMIX sizes, so they are compared statistically. Durations are
short so the suite runs in seconds.

Stochastic scenarios observe only metrics whose per-run mean varies little
between runs: with 30 replications its standard error must stay well below
the 2 % limit, or two correct backends would fail the comparison by chance
(``test_stochastic_metrics_are_precise_enough``). Instantaneous samples such
as ``queue_depth`` are therefore checked in the deterministic scenarios.
"""

from __future__ import annotations

from collections.abc import Callable

from smolpy import Network
from smolpy.model import Scenario


def line() -> Scenario:
    """Two adapters on one switch, at a third of the link rate."""
    net = Network("line")
    sw = net.switch("sw", ports=2)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    net.link(a, sw, speed=100, length=30)
    net.link(b, sw, speed=100, length=5)
    a.sends(b, rate=2_500, size=1_500, delay_ms=1.5)
    for metric in ("throughput", "latency", "bytes_received"):
        net.observe(metric, on=b, every=5)
    net.observe("bytes_sent", on=a, every=5)
    net.observe("utilization", on=sw, every=5)
    return net.to_scenario(duration=60)


def priorities() -> Scenario:
    """PROFINET next to bulk traffic that saturates the uplink of a
    two-tier network with 802.1Q priorities; a watchdog that expires."""
    net = Network("priorities")
    core = net.switch("core", ports=3)
    edge = net.switch("edge", ports=4, mode="cut-through", mac_table="topology")
    plc = net.adapter("plc", ip="10.0.0.1")
    io = net.profinet_device("io", ip="10.0.0.2")
    slow_io = net.profinet_device("slow-io", ip="10.0.0.3")
    bulk = net.adapter("bulk", ip="10.0.0.4")
    srv = net.adapter("srv", ip="10.0.0.5")
    net.link(plc, core, speed=100, length=10)
    net.link(srv, core, speed=100, length=10)
    net.link(core, edge, speed=100, length=50)
    net.link(io, edge, speed=100, length=10)
    net.link(slow_io, edge, speed=10, length=10)
    net.link(bulk, edge, speed=100, length=10)
    plc.controls(io, cycle=2, inputs=40, outputs=40)
    plc.controls(slow_io, cycle=1, inputs=1_400, outputs=1_400, hold=1)
    bulk.sends(srv, rate=9_000, size=1_500, priority=0)
    bulk.sends(plc, rate=200, size=300, priority=5, delay_ms=0.25)
    for metric in ("pn_latency", "pn_jitter", "pn_watchdog_expiries"):
        net.observe(metric, on=plc, every=4)
    net.observe("pn_latency", on=io, every=4)
    net.observe("pn_watchdog_expiries", on=slow_io, every=4)
    net.observe("queue_depth", on=edge, every=2)
    net.observe("latency", on=srv, every=4)
    return net.to_scenario(duration=60)


def protocols() -> Scenario:
    """Modbus polling and MQTT publish/subscribe across a switch, with a
    FIFO switch."""
    net = Network("protocols")
    sw = net.switch("sw", ports=5, qos="fifo")
    scada = net.adapter("scada", ip="10.0.0.1")
    meter = net.modbus_slave("meter", ip="10.0.0.2", unit_id=3)
    sensor = net.adapter("sensor", ip="10.0.0.3")
    broker = net.mqtt_broker("broker", ip="10.0.0.4")
    hmi = net.adapter("hmi", ip="10.0.0.5")
    for node in (scada, meter, sensor, broker, hmi):
        net.link(node, sw, speed=100, length=20)
    scada.polls(meter, register=0, count=60, rate=400)
    broker.routes("plant/temp", to=[hmi, scada])
    sensor.publishes(broker, topic="plant/temp", rate=300, payload=64, qos=1)
    sensor.publishes(broker, topic="plant/other", rate=100, payload=16, delay_ms=0.7)
    net.observe("modbus_latency", on=scada, every=5)
    net.observe("latency", on=hmi, every=5)
    net.observe("bytes_received", on=scada, every=5)
    net.observe("broker_queue", on=broker, every=1)
    return net.to_scenario(duration=60)


def random_load() -> Scenario:
    """Poisson and bursty traffic with IMIX sizes into one server port, at
    about 40 % of its link."""
    net = Network("random-load")
    sw = net.switch("sw", ports=4)
    srv = net.adapter("srv", ip="10.0.0.1")
    net.link(srv, sw, speed=100, length=5)
    for i, (pattern, rate) in enumerate(
        (("poisson", 4_000), ("bursty", 1_500), ("poisson", 4_000)), start=2
    ):
        host = net.adapter(f"h{i}", ip=f"10.0.0.{i}")
        net.link(host, sw, speed=100, length=10)
        host.sends(srv, rate=rate, size="imix", pattern=pattern)  # type: ignore[arg-type]
    for metric in ("throughput", "latency", "bytes_received"):
        net.observe(metric, on=srv, every=20)
    return net.to_scenario(duration=400, seed=11)


def batches() -> Scenario:
    """Batch arrivals with fixed and heavy-tailed group sizes, next to
    PROFINET traffic."""
    net = Network("batches")
    sw = net.switch("sw", ports=4)
    srv = net.adapter("srv", ip="10.0.0.1")
    plc = net.adapter("plc", ip="10.0.0.2")
    io = net.profinet_device("io", ip="10.0.0.3")
    cam = net.adapter("cam", ip="10.0.0.4")
    for node in (srv, plc, io, cam):
        net.link(node, sw, speed=100, length=10)
    plc.controls(io, cycle=4, inputs=20, outputs=20)
    cam.sends(srv, rate=4_000, size=1_200, pattern="batch", batch=4)
    cam.sends(plc, rate=1_000, size="imix", pattern="batch", batch="pareto:2.5")
    net.observe("latency", on=srv, every=20)
    net.observe("throughput", on=srv, every=20)
    net.observe("pn_latency", on=plc, every=20)
    net.observe("utilization", on=sw, every=20)
    return net.to_scenario(duration=1_000, seed=5)


DETERMINISTIC: dict[str, Callable[[], Scenario]] = {
    "line": line,
    "priorities": priorities,
    "protocols": protocols,
}
STOCHASTIC: dict[str, Callable[[], Scenario]] = {
    "random-load": random_load,
    "batches": batches,
}
