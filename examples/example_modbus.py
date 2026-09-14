"""
Modbus TCP scenario: a PLC polling 3 field sensors, sharing the switch
with regular Ethernet traffic.

No gateway/converter needed
----------------------------
Unlike Modbus RTU/ASCII (serial, requires a gateway to reach an IP network),
Modbus TCP already rides directly on Ethernet — the wire frame is just
Ethernet/IPv4/TCP + a 6-byte MBAP header + the Modbus PDU. So a ModbusSlave
is a first-class network node, wired into the same switch fabric as any
other Adapter, exactly like MQTTBroker.

Mounting sensors on Modbus
---------------------------
Each field device is a separate ModbusSlave node, addressed by its own
unit_id (1-247) — this is the Modbus equivalent of a subscriber list, except
addressing happens per-request rather than per-topic. One master (the PLC,
a regular Adapter using polls()) can poll many slaves; the simulation
engine routes each request/response pair by matching frame.modbus_unit_id
against each slave's own unit_id.

Master/slave vs MQTT pub/sub
------------------------------
Where MQTT is fire-and-forget fan-out through a broker, Modbus here is
strict master-initiated request/response: the PLC polls each sensor at a
fixed rate, and only the addressed slave replies. This lets us measure
poll latency (RTT) directly, which pub/sub protocols like MQTT can't
express without an extra ack layer.

Sharing the wire with bulk Ethernet traffic
---------------------------------------------
The PLC's uplink to the switch also carries a large periodic upload to a
historian server, demonstrating that Modbus and regular Ethernet traffic
compete for the same link — the poll latency (modbus_latency) responds to
that contention just like any other queued traffic.
"""
from smolpy import Network

net = Network("modbus-plant")
sw = net.switch("sw", ports=8)

plc = net.adapter("plc", ip="10.0.0.1")

sensors = {
    "temperature": net.modbus_slave("temp-sensor", ip="10.0.0.10", unit_id=1),
    "pressure": net.modbus_slave("pressure-sensor", ip="10.0.0.11", unit_id=2),
    "flow": net.modbus_slave("flow-sensor", ip="10.0.0.12", unit_id=3),
}

historian = net.adapter("historian", ip="10.0.0.20")

net.link(plc, sw, speed=100, length=5)
for sensor in sensors.values():
    net.link(sensor, sw, speed=100, length=10)
net.link(historian, sw, speed=1_000, length=2)

# PLC polls each sensor's holding registers at a different, realistic rate.
plc.polls(sensors["temperature"], register=40001, count=2, rate=1.0)
plc.polls(sensors["pressure"], register=40010, count=2, rate=1.0)
plc.polls(sensors["flow"], register=40020, count=4, rate=0.5)

# Regular Ethernet traffic sharing the PLC's uplink: periodic batch upload
# of logged data to the historian, competing with the Modbus polls.
plc.sends(to=historian, rate=20, size=1_518, pattern="bursty")

net.observe("modbus_latency", on=plc, every=500)
net.observe("throughput", on=historian, every=500)
net.observe("queue_depth", on=sw, every=500)

result = net.simulate(duration=30_000, live=True)
result.report()
