"""
20-client file transfer — heavy oversubscription (2 Gbps into 1 Gbps link).

Twenty clients push files with staggered starts (one new client every 3 s).
The server link is 1 Gbps.  From the moment the 11th client joins (t = 30 s)
the combined input is double the server link capacity and the switch queue
grows continuously.  This example demonstrates what network congestion looks
like at the packet level and how queue growth tracks oversubscription.

TRANSFER SCHEDULE
─────────────────
   client-{1..20} start at t = 0, 3, 6, 9, … , 57 s  (3 s apart)
   simulation runs for 90 s

COMBINED LOAD vs SERVER LINK
──────────────────────────────
   Each client    :   100 Mbps
   20 clients     : 2 000 Mbps  (server link capacity: 1 000 Mbps)
   Oversubscription:  100 %  —  2× the available bandwidth

WHAT TO WATCH IN THE CHARTS
────────────────────────────
  throughput      — climbs in 100 Mb/s steps until the link saturates at
                    t ≈ 30 s (10 clients active), then stays flat at ~1 Gb/s
                    regardless of how many more clients join.
  queue_depth     — near zero for t < 30 s, then ramps upward steeply after
                    the 11th client joins, and keeps climbing as each new client
                    adds more input than the bottleneck can drain.
  bytes_received  — slope locks at the 1 Gbps ceiling from t ≈ 30 s onward.
  bytes_sent      — client-1 (t = 0) and client-20 (t = 57 s) as reference
                    traces; note that both send at the same line rate — it is
                    the server, not the clients, that is the bottleneck.

TOPOLOGY
────────────────────────────────────────────────────
  [client-1 … 20]  ── 100 Mbps / 20 m ──┐
                                        [core-sw] ── 1 Gbps / 2 m ── [server]
"""

from smolpy import Network

# ── Topology ──────────────────────────────────────────────────────────────────
net = Network("20-client-congestion")

clients = [net.adapter(f"client-{i}", ip=f"10.0.3.{i}") for i in range(1, 21)]
server  = net.adapter("server", ip="10.0.3.100")
sw      = net.switch("core-sw", ports=24, mode="store-and-forward")

for client in clients:
    net.link(client, sw, speed=100, length=20)

net.link(server, sw, speed=1_000, length=2)

# ── Traffic — staggered starts, one client every 3 s ─────────────────────────
_FPS = int(100_000_000 / (1_518 * 8))   # ≈ 8 231 frames/s (100 Mbps saturated)

for i, client in enumerate(clients):
    client.sends(to=server, rate=_FPS, size=1_518, pattern="constant",
                 delay_ms=i * 3_000)

# ── Observations ──────────────────────────────────────────────────────────────
for client in clients:
    net.observe("bytes_sent", on=client, every=500)   # one line per client

net.observe("bytes_received", on=server, every=500)
net.observe("throughput",     on=server, every=500)
net.observe("queue_depth",    on=sw,     every=200)

# ── Simulate ──────────────────────────────────────────────────────────────────
result = net.simulate(duration=50_000, live=True)
result.report()
