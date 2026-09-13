"""
MQTT Publish-Subscribe scenario: 10 temperature sensors → broker → server.

Publish-subscribe model
-----------------------
Each sensor adapter calls publishes() to send periodic MQTT PUBLISH frames
to the broker on topic "plant/temperature".  The broker deduplicates topic
routing via routes(), fanning out one copy of every inbound PUBLISH to each
registered subscriber (here: the server adapter).  Sensors join staggered
by 2 s intervals so each sensor's contribution is individually visible in
the metric charts.

QoS 1 acknowledgement
---------------------
With qos=1, the broker also enqueues a PUBACK frame (58 bytes) back toward
the publisher.  In this simplified model the PUBACK travels the broker's
single outbound channel (toward the server), adding a small amount of extra
traffic on that link.

broker_queue metric
-------------------
Samples the depth of the broker's inbound store — unprocessed PUBLISH frames
waiting to be fanned out.  At 1 msg/s per sensor with 10 sensors active the
arrival rate reaches 10 msg/s; as long as the 1 Gbps broker uplink is not
saturated the queue stays at zero.  A rising queue would indicate the broker
is becoming a bottleneck.

Frame sizes
-----------
Each PUBLISH creates a wire frame of:
  Ethernet+IPv4+TCP  =  54 B
  MQTT overhead      =   2 B (fixed header) + 2 B (topic-length field)
                       + 16 B ("plant/temperature") + 2 B (packet-ID, QoS 1)
                       =  22 B
  Payload            =  20 B
  ─────────────────────
  Total              =  96 B    (~100 B, vs bulk transfers at 1 518 B)

With all 10 sensors active the aggregate publish rate is
  10 × 96 B × 8 bits ≈ 7.7 kb/s on each sensor's 100 Mbps link,
and ≈ 9.6 kb/s (publishes) + overhead (PUBACKs) on the 1 Gbps backbone —
negligible compared to the link capacity.
"""
from smolpy import Network, MQTTBroker

net    = Network("mqtt-sensors")
sw     = net.switch("sw", ports=14)
broker = net.mqtt_broker("mqtt-broker", ip="10.0.2.1")
server = net.adapter("server", ip="10.0.3.1")

sensors = [
    net.adapter(f"sensor-{i + 1}", ip=f"10.0.1.{i + 1}")
    for i in range(10)
]

for sensor in sensors:
    net.link(sensor, sw, speed=100, length=10)

net.link(sw, broker, speed=1_000, length=2)
net.link(broker, server, speed=1_000, length=2)

broker.routes("plant/temperature", to=[server])

for i, sensor in enumerate(sensors):
    sensor.publishes(
        to=broker,
        topic="plant/temperature",
        rate=1.0,
        payload=20,
        qos=1,
        delay_ms=i * 2_000,
    )

net.observe("broker_queue", on=broker, every=500)
net.observe("latency",      on=server, every=500)
net.observe("throughput",   on=server, every=500)

result = net.simulate(duration=40_000, live=True)
result.report()
