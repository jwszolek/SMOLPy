"""
10-client file transfer — borderline saturation of the server uplink.

Ten clients push files simultaneously with staggered starts (one new client
every 6 seconds).  Each client runs at 100 Mbps, so the combined load when
all ten are active is exactly 1 Gbps — matching the server link capacity to
the byte.  The switch queue stays near zero because input equals output, but
there is no room to spare.

TRANSFER SCHEDULE
─────────────────
   client-{1..10} start at t = 0, 6, 12, 18, 24, 30, 36, 42, 48, 54 s
   simulation runs for 90 s

COMBINED LOAD vs SERVER LINK
──────────────────────────────
   Each client   : 100 Mbps
   10 clients    : 1 000 Mbps  =  1 Gbps  (server link capacity)
   Oversubscription: 0 %

WHAT TO WATCH IN THE CHARTS
────────────────────────────
  throughput      — climbs in ~100 Mb/s steps every 6 s, plateaus at ~1 Gb/s
                    once all clients are active.
  bytes_received  — slope increases each time a new client joins.
  queue_depth     — stays near zero even at full saturation; the server link
                    can just keep up with the combined input.
  bytes_sent      — representative traces for client-1 (first) and
                    client-10 (last).

TOPOLOGY
────────────────────────────────────────────────────
  [client-1 … 10]  ── 100 Mbps / 20 m ──┐
                                       [core-sw] ── 1 Gbps / 2 m ── [server]
"""

from smolpy import Network

# ── Topology ──────────────────────────────────────────────────────────────────
net = Network("10-client-saturation")

clients = [net.adapter(f"client-{i}", ip=f"10.0.1.{i}") for i in range(1, 11)]
server  = net.adapter("server", ip="10.0.1.100")
sw      = net.switch("core-sw", ports=16, mode="store-and-forward")

for i, client in enumerate(clients, start=1):
    net.link(client, sw, speed=100, length=20)

net.link(server, sw, speed=1_000, length=2)

# ── Traffic — staggered starts, one client every 6 s ─────────────────────────
_FPS = int(100_000_000 / (1_518 * 8))   # ≈ 8 231 frames/s (100 Mbps saturated)

for i, client in enumerate(clients):
    client.sends(to=server, rate=_FPS, size=1_518, pattern="constant",
                 delay_ms=i * 6_000)

# ── Observations ──────────────────────────────────────────────────────────────
for client in clients:
    net.observe("bytes_sent", on=client, every=500)   # one line per client

net.observe("bytes_received", on=server, every=500)
net.observe("throughput",     on=server, every=500)
net.observe("queue_depth",    on=sw,     every=200)

# ── Simulate ──────────────────────────────────────────────────────────────────
result = net.simulate(duration=50_000, live=True)
result.report()
