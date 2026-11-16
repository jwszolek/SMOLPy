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


def switching() -> Scenario:
    """Ethernet only, within the Rust MVP: a learning core that floods until
    it has learnt every station, a converged FIFO access switch, priorities,
    a hairpin-free mesh of flows that start at different instants, and every
    instant on a 1 ns grid that coincides with samples."""
    net = Network("switching")
    core = net.switch("core", ports=4)  # learning: floods unknown destinations
    acc = net.switch("acc", ports=4, qos="fifo", mac_table="topology")
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    c = net.adapter("c", ip="10.0.0.3")
    d = net.adapter("d", ip="10.0.0.4")
    net.link(a, core, speed=100, length=10)
    net.link(b, core, speed=1_000, length=3)
    net.link(core, acc, speed=100, length=50)
    net.link(c, acc, speed=100, length=10)
    net.link(d, acc, speed=10, length=1)
    a.sends(c, rate=4_000, size=1_200, priority=0)  # crosses the core and the access switch
    b.sends(c, rate=2_000, size=200, priority=6, delay_ms=0.5)
    c.sends(a, rate=1_000, size=64, delay_ms=1)  # lets the core learn c late
    d.sends(b, rate=500, size=1_518, priority=3, delay_ms=0.25)
    b.sends(d, rate=250, size=128)
    for metric in ("throughput", "latency", "bytes_received"):
        net.observe(metric, on=c, every=1)
    net.observe("latency", on=a, every=2)
    net.observe("bytes_sent", on=b, every=1)
    net.observe("queue_depth", on=acc, every=0.5)
    net.observe("queue_depth", on=core, every=0.5)
    net.observe("utilization", on=acc, every=1)
    net.observe("utilization", on=d, every=2)
    return net.to_scenario(duration=40)


def polling() -> Scenario:
    """Modbus TCP only, within the Rust engine's scope: two masters poll three
    slaves, one behind a second switch. The learning core floods requests
    until it learns a slave, so slaves also receive requests for others,
    and drop them. Bulk traffic shares the uplink."""
    net = Network("polling")
    core = net.switch("core", ports=5)  # learning
    edge = net.switch("edge", ports=3)
    scada = net.adapter("scada", ip="10.0.0.1")
    plc = net.adapter("plc", ip="10.0.0.2")
    meter = net.modbus_slave("meter", ip="10.0.0.10", unit_id=1)
    drive = net.modbus_slave("drive", ip="10.0.0.11", unit_id=7)
    valve = net.modbus_slave("valve", ip="10.0.0.12", unit_id=3)
    net.link(scada, core, speed=100, length=10)
    net.link(plc, core, speed=100, length=10)
    net.link(meter, core, speed=10, length=5)
    net.link(core, edge, speed=100, length=80)
    net.link(drive, edge, speed=100, length=5)
    net.link(valve, edge, speed=100, length=5)
    scada.polls(meter, register=0, count=2, rate=250)
    scada.polls(drive, register=100, count=60, rate=125, delay_ms=0.3)
    plc.polls(valve, register=10, count=1, rate=500, delay_ms=1)
    plc.polls(drive, register=0, count=125, rate=100)
    plc.sends(scada, rate=3_000, size=1_200, delay_ms=0.5)  # shares the core with responses
    for master in (scada, plc):
        net.observe("modbus_latency", on=master, every=2)
        net.observe("bytes_received", on=master, every=2)
    net.observe("latency", on=scada, every=4)
    net.observe("queue_depth", on=core, every=1)
    net.observe("utilization", on=edge, every=4)
    return net.to_scenario(duration=40)


def messaging() -> Scenario:
    """MQTT only, within the Rust engine's scope: two brokers, one behind a
    FIFO edge switch, with QoS 0 and 1, one topic on both brokers, a topic
    nobody subscribes to and non-ASCII topic names (their length counts
    characters). Tagged bulk traffic shares the uplink."""
    net = Network("messaging")
    core = net.switch("core", ports=5, mac_table="topology")
    edge = net.switch("edge", ports=4, qos="fifo")  # learning
    plant = net.mqtt_broker("plant", ip="10.0.1.1")
    line = net.mqtt_broker("line", ip="10.0.2.1")
    hall = net.adapter("hall", ip="10.0.1.10")
    hmi = net.adapter("hmi", ip="10.0.1.20")
    scada = net.adapter("scada", ip="10.0.1.21")
    cell = net.adapter("cell", ip="10.0.2.10")
    historian = net.adapter("historian", ip="10.0.2.20")
    for node in (plant, hall, hmi, scada):
        net.link(node, core, speed=100, length=10)
    net.link(core, edge, speed=100, length=120)
    for node in (cell, historian, line):
        net.link(node, edge, speed=100, length=10)
    hall.publishes(plant, topic="hala/temp°C", rate=400, payload=32, qos=1)
    hall.publishes(plant, topic="hala/ciśnienie", rate=250, payload=200, delay_ms=0.4)
    cell.publishes(plant, topic="linia/stan", rate=200, payload=0, qos=1)
    cell.publishes(line, topic="linia/stan", rate=150, payload=900, delay_ms=1.1)
    cell.publishes(line, topic="debug", rate=100, payload=50, qos=1)  # routed nowhere
    plant.routes("hala/temp°C", to=[hmi, historian])
    plant.routes("hala/ciśnienie", to=[scada])
    plant.routes("linia/stan", to=[hmi])
    line.routes("linia/stan", to=[historian, hmi, scada])
    historian.sends(hmi, rate=2_000, size=1_400, priority=2, delay_ms=0.2)
    for sub in (hmi, scada, historian):
        net.observe("latency", on=sub, every=2)
    for pub in (hall, cell):
        net.observe("bytes_received", on=pub, every=4)  # their PUBACKs
    net.observe("throughput", on=historian, every=4)
    for broker in (plant, line):
        net.observe("broker_queue", on=broker, every=1)
    net.observe("utilization", on=plant, every=4)
    net.observe("queue_depth", on=edge, every=0.05)  # fan-out copies queue briefly
    return net.to_scenario(duration=40)


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


def groups() -> Scenario:
    """Ethernet only, within the Rust MVP: batch arrivals with fixed,
    geometric and heavy-tailed group sizes, IMIX, and priorities."""
    net = Network("groups")
    sw = net.switch("sw", ports=4)
    srv = net.adapter("srv", ip="10.0.0.1")
    cam = net.adapter("cam", ip="10.0.0.2")
    log = net.adapter("log", ip="10.0.0.3")
    hmi = net.adapter("hmi", ip="10.0.0.4")
    for node in (srv, cam, log, hmi):
        net.link(node, sw, speed=100, length=10)
    cam.sends(srv, rate=3_000, size=1_000, pattern="batch", batch=4)
    log.sends(srv, rate=2_000, size="imix", pattern="batch", batch="geom:3", priority=2)
    hmi.sends(srv, rate=1_000, size=300, pattern="batch", batch="pareto:2.5", priority=5)
    net.observe("latency", on=srv, every=20)
    net.observe("throughput", on=srv, every=20)
    net.observe("bytes_received", on=srv, every=20)
    net.observe("utilization", on=sw, every=20)
    return net.to_scenario(duration=800, seed=17)


DETERMINISTIC: dict[str, Callable[[], Scenario]] = {
    "line": line,
    "priorities": priorities,
    "switching": switching,
    "polling": polling,
    "messaging": messaging,
    "protocols": protocols,
}
STOCHASTIC: dict[str, Callable[[], Scenario]] = {
    "random-load": random_load,
    "batches": batches,
    "groups": groups,
}
