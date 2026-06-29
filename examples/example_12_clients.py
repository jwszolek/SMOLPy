"""
12-client file transfer — mild oversubscription (1.2 Gbps into 1 Gbps link).

Twelve clients push files with staggered starts (one new client every 5 s).
The server link is 1 Gbps.  While the first 10 clients are active the switch
handles traffic without queuing.  When client-11 joins at t = 50 s the
combined load crosses 1 Gbps and the switch queue starts growing.  Client-12
joins at t = 55 s, pushing the oversubscription to 20 %.

TRANSFER SCHEDULE
─────────────────
   client-{1..12} start at t = 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55 s
   simulation runs for 90 s

COMBINED LOAD vs SERVER LINK
──────────────────────────────
   Each client    :   100 Mbps
   12 clients     : 1 200 Mbps  (server link capacity: 1 000 Mbps)
   Oversubscription:   20 %

WHAT TO WATCH IN THE CHARTS
────────────────────────────
  throughput      — steps up every 5 s, locks at ~1 Gb/s once the link is full.
                    No further increase even as clients 11 and 12 join.
  queue_depth     — flat near zero for t < 50 s, then rises sharply when the
                    11th client creates the first oversubscription.  Keeps
                    growing after t = 55 s (12th client joins).
  bytes_received  — slope flattens once the server link saturates; the curve
                    becomes linear at the 1 Gbps cap rather than keeping pace
                    with the clients' combined 1.2 Gbps send rate.
  bytes_sent      — client-1 (starts first) and client-12 (starts last) as
                    representative traces.

TOPOLOGY
────────────────────────────────────────────────────
  [client-1 … 12]  ── 100 Mbps / 20 m ──┐
                                        [core-sw] ── 1 Gbps / 2 m ── [server]
"""

from smolpy import Network

# ── Topology ──────────────────────────────────────────────────────────────────
net = Network("12-client-oversubscribed")

clients = [net.adapter(f"client-{i}", ip=f"10.0.2.{i}") for i in range(1, 13)]
server  = net.adapter("server", ip="10.0.2.100")
sw      = net.switch("core-sw", ports=16, mode="store-and-forward")

for client in clients:
    net.link(client, sw, speed=100, length=20)

net.link(server, sw, speed=1_000, length=2)

# ── Traffic — staggered starts, one client every 5 s ─────────────────────────
_FPS = int(100_000_000 / (1_518 * 8))   # ≈ 8 231 frames/s (100 Mbps saturated)

for i, client in enumerate(clients):
    client.sends(to=server, rate=_FPS, size=1_518, pattern="constant",
                 delay_ms=i * 5_000)

# ── Observations ──────────────────────────────────────────────────────────────
for client in clients:
    net.observe("bytes_sent", on=client, every=500)   # one line per client

net.observe("bytes_received", on=server, every=500)
net.observe("throughput",     on=server, every=500)
net.observe("queue_depth",    on=sw,     every=200)

# ── Simulate ──────────────────────────────────────────────────────────────────
result = net.simulate(duration=90_000, live=True)
result.report()
