"""
Staggered multi-client file transfer — three clients push files to a server
with delayed starts.  The cumulative MB charts show exactly when each
transfer begins and how the network load compounds over time.

TRANSFER SCHEDULE
─────────────────
   t =  0 s   client-1 starts  (100 Mbps uplink, 1 518 B frames)
   t =  5 s   client-2 joins
   t = 25 s   client-3 joins   (5 s + 20 s = 25 s)
   simulation runs for 40 s

FILE SIZE (per client, at line rate on 100 Mbps)
─────────────────────────────────────────────────
   Max rate : 100 Mbps / (1 518 B × 8) ≈ 8 231 frames/s
   40 s × 8 231 fps × 1 518 B ≈ ~500 MB per client (if it ran the full sim)

WHAT TO WATCH IN THE CHARTS
────────────────────────────
  bytes_sent    — one line per client, starts flat then rises when the
                  client's delay expires.  Slope = actual TX rate.

  bytes_received — single line at server; slope jumps at t=5 s and t=25 s
                   as each new client joins, showing the load ramp-up.

  throughput     — instantaneous rate at server (Mb/s); should step up in
                   ~100 Mb/s increments at t=5 s and t=25 s.

  queue_depth    — frames buffered inside the switch.  With a 1 Gbps server
                   link and ≤ 300 Mbps combined input the queue stays near
                   zero — the switch is not the bottleneck here.

TOPOLOGY
────────────────────────────────────────────────────
  [client-1] ── 100 Mbps / 10 m ──┐
  [client-2] ── 100 Mbps / 20 m ──┤── [core-sw] ── 1 Gbps / 2 m ── [server]
  [client-3] ── 100 Mbps / 30 m ──┘
"""

from smolpy import Network

# ── Topology ──────────────────────────────────────────────────────────────────
net = Network("staggered-transfer")

client1 = net.adapter("client-1", ip="10.0.0.1")
client2 = net.adapter("client-2", ip="10.0.0.2")
client3 = net.adapter("client-3", ip="10.0.0.3")
server  = net.adapter("server",   ip="10.0.0.10")
sw      = net.switch("core-sw",   ports=8, mode="store-and-forward")

net.link(client1, sw, speed=100,    length=10)
net.link(client2, sw, speed=100,    length=20)
net.link(client3, sw, speed=100,    length=30)
net.link(server,  sw, speed=1_000,  length=2)   # 1 Gbps — no bottleneck on server side

# ── Traffic — staggered file transfers at line rate ───────────────────────────
_FPS = int(100_000_000 / (1_518 * 8))   # ≈ 8 231 frames/s (100 Mbps saturated)

client1.sends(to=server, rate=_FPS, size=1_518, pattern="constant", delay_ms=0)
client2.sends(to=server, rate=_FPS, size=1_518, pattern="constant", delay_ms=5_000)
client3.sends(to=server, rate=_FPS, size=1_518, pattern="constant", delay_ms=25_000)

# ── Observations ──────────────────────────────────────────────────────────────
# How many MB has each client actually put on the wire?
net.observe("bytes_sent",     on=client1, every=500)
net.observe("bytes_sent",     on=client2, every=500)
net.observe("bytes_sent",     on=client3, every=500)

# How many MB has the server received in total?
net.observe("bytes_received", on=server,  every=500)

# Instantaneous throughput at the server — shows the step-up as clients join
net.observe("throughput",     on=server,  every=500)

# Switch queue — should stay near zero with a 1 Gbps downstream link
net.observe("queue_depth",    on=sw,      every=200)

# ── Simulate ──────────────────────────────────────────────────────────────────
result = net.simulate(duration=40_000, live=True)
result.report()
