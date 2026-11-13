"""
PROFINET RT scenario: a PLC exchanging cyclic I/O with three IO devices while
an engineering station downloads a project to the same PLC.

Cyclic producer-consumer exchange
---------------------------------
plc.controls() sets up one Application Relationship (AR) per device. Every
2 ms the PLC sends output data to each device and each device sends input
data to the PLC, independently -- not as request and reply. Each consumer runs
a watchdog: no frame for hold x cycle = 3 x 2 ms = 6 ms counts an expiry.

Why prioritisation matters
--------------------------
The engineering station sits on a 1 Gb/s link and sends the download in bursts
of 100 full-size frames. The PLC's own link is 100 Mb/s, so every burst queues
for ~12 ms in front of the PLC's switch port -- the same port that carries the
devices' input frames.

  QOS = "strict"  RT frames (802.1Q priority 6) overtake the burst:
                  pn_jitter stays around 100 us, no watchdog expiries.
  QOS = "fifo"    RT frames wait behind each burst: pn_jitter reaches
                  several ms and the watchdogs expire repeatedly.

Change QOS below and run again to compare.
"""

from smolpy import Network

QOS = "strict"  # or "fifo"

net = Network("profinet-cell")
sw = net.switch("cell-sw", ports=8, qos=QOS)

plc = net.adapter("plc", ip="10.0.0.1")
eng = net.adapter("eng-station", ip="10.0.0.50")
devices = [net.profinet_device(f"io-{i}", ip=f"10.0.4.{i}") for i in range(1, 4)]

net.link(plc, sw, speed=100, length=5)
net.link(eng, sw, speed=1_000, length=20)
for dev in devices:
    net.link(dev, sw, speed=100, length=15)
    plc.controls(dev, cycle=2, inputs=32, outputs=16, hold=3)

# Project download: average ~49 Mb/s, sent in bursts of 100 frames, starting at 2 s
eng.sends(to=plc, rate=4_000, size=1_518, pattern="batch", batch=100, delay_ms=2_000)

net.observe("pn_latency", on=plc, every=100)
net.observe("pn_jitter", on=plc, every=100)
net.observe("pn_watchdog_expiries", on=plc, every=100)
net.observe("queue_depth", on=sw, every=100)

result = net.simulate(duration=10_000, live=True)
result.report()
