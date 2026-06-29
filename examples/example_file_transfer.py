"""
File-transfer simulation — client sends a large file to a server over a 100 Mbps LAN.

MONITORING PERSPECTIVE
──────────────────────
For a file transfer the natural monitoring point is the SERVER (receiver):

  • throughput  — how fast data actually arrives (Mb/s).
                  Goal: stay close to the link rate (~94 Mb/s after Ethernet overhead).

  • latency     — time from frame creation to delivery (µs).
                  Spikes mean frames are queuing inside the switch.

  • frame_loss  — frames that never arrive.
                  Should be 0 % on a wired LAN; any loss is a red flag.

The SWITCH gives the network-level view:

  • queue_depth — frames buffered at the switch.
                  A persistently growing queue means the output port is
                  slower than the combined input rate (congestion).

  • utilization — what fraction of the switch's input capacity is in use.
                  100 % = the uplinks are saturated.

TOPOLOGY
────────────────────────────────────────────
  [client]  ──  100 Mbps / 10 m  ──▶ [core-sw]
  [bg-host] ──  100 Mbps / 50 m  ──▶ [core-sw]  ← competing background traffic
                                          │
                                   1 Gbps / 2 m
                                          │
                                      [server]

The client→switch link (100 Mbps) is the bottleneck.
The server→switch link (1 Gbps) has headroom, so frames are never
queued on the way out — all congestion shows up at the switch input.

FILE TRANSFER PARAMETERS
────────────────────────
  File size  : 100 MB
  Frame size : 1 518 B  (max Ethernet / jumbo off)
  Link speed : 100 Mbps
  Max rate   : 100 000 000 / (1 518 × 8) ≈ 8 231 frames/s
  Transfer   : 100 MB / 100 Mbps = ~8 000 ms
  Simulation : 12 000 ms  (covers transfer + 4 s tail so you can see the queue drain)
"""

from smolpy import Network

# ── Topology ──────────────────────────────────────────────────────────────────
net    = Network("file-transfer")
client = net.adapter("client",  ip="10.0.0.1")
server = net.adapter("server",  ip="10.0.0.2")
bg     = net.adapter("bg-host", ip="10.0.0.3")
sw     = net.switch("core-sw",  ports=8, mode="store-and-forward")

net.link(client, sw, speed=100,    length=10)   # 100 Mbps — bottleneck uplink
net.link(server, sw, speed=1_000,  length=2)    # 1 Gbps   — server is never the limit
net.link(bg,     sw, speed=100,    length=50)   # 100 Mbps — background host

# ── Traffic ───────────────────────────────────────────────────────────────────
# Client saturates its 100 Mbps uplink (file transfer at line rate)
_MAX_FPS = int(100_000_000 / (1_518 * 8))       # ≈ 8 231 frames/s
client.sends(to=server, rate=_MAX_FPS, size=1_518, pattern="constant")

# Background host: light web / IoT traffic (~10 % of its link)
bg.sends(to=server, rate=820, size="imix", pattern="poisson")

# ── Observations ──────────────────────────────────────────────────────────────
# --- Server perspective (did the file arrive correctly and quickly?) -----------
net.observe("throughput", on=server, every=200)  # Mb/s  — primary KPI
net.observe("latency",    on=server, every=200)  # µs    — congestion indicator

# --- Switch perspective (is the network coping with the load?) ----------------
net.observe("queue_depth", on=sw, every=50)      # frames — congestion visible here
net.observe("utilization", on=sw, every=200)     # %      — combined input load

# ── Simulate ──────────────────────────────────────────────────────────────────
# 12 s covers the full transfer + tail so you can watch the queue drain to zero.
result = net.simulate(duration=12_000, live=True)
result.report()
