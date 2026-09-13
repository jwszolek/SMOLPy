"""
Two-tier switching — access bottleneck on the edge uplink.

Nine "direct" clients connect straight to the core switch.  Five "edge"
clients sit behind a second (access) switch whose uplink to the core is only
100 Mbps — the same speed as a single client link.

Clients join one at a time (1 s apart within each group) so every bytes_sent
line is individually visible and the queue explosion is pinpointed to the
exact moment the 2nd edge client joins.

CLIENT JOIN SCHEDULE
────────────────────
  client-1 … 9  :  t =  0,  1,  2, …,  8 s   (100 Mbps each, direct to core-sw)
  edge-1   … 5  :  t = 12, 13, 14, 15, 16 s   (100 Mbps each, via edge-sw uplink)
  simulation     :  40 s

WHAT HAPPENS AT EACH KEY MOMENT
────────────────────────────────
   t =  0– 8 s   direct load ramps from 100 → 900 Mbps; queues near zero
   t =  9–11 s   direct load stable at 900 Mbps; server link at 90 %
   t = 12 s       edge-1 joins → 100 Mbps on uplink (just fits); server = 1 Gbps
   t = 13 s       edge-2 joins → 200 Mbps into 100 Mbps uplink → edge-sw queue STARTS
   t = 14–16 s   edge-3/4/5 join → 300/400/500 Mbps → edge-sw queue grows fast
   t > 16 s       all 14 clients active; edge-sw perpetually overloaded

TOPOLOGY
────────────────────────────────────────────────────────────────
  [client-1 … 9]  ── 100 Mbps / 20 m ──┐
                                      [core-sw] ── 1 Gbps / 2 m ── [server]
  [edge-sw] ─── 100 Mbps / 5 m ─────┘
    │
  [edge-1 … 5] ── 100 Mbps / 10 m ──┘

WHAT TO WATCH
─────────────
  queue_depth:edge-sw   — near zero at t=12 s (1 edge client = 100 Mbps, fits);
                          then explodes at t=13 s when client 2 pushes it over
  queue_depth:core-sw   — stays near zero throughout; edge-sw uplink caps the
                          inbound at exactly 100 Mbps regardless of edge clients
  throughput:server     — ramps to ~1 Gbps as clients join, then holds flat
  bytes_sent:client-*   — 9 distinct lines ramping up 1 s apart
  bytes_sent:edge-*     — 5 lines; only edge-1 achieves ~11.8 MB/s; others are
                          starved by the shared 100 Mbps uplink (~4 MB/s each)
"""

from smolpy import Network

# ── Topology ──────────────────────────────────────────────────────────────────
net = Network("two-tier-bottleneck")

direct_clients = [net.adapter(f"client-{i}", ip=f"10.0.1.{i}") for i in range(1, 10)]
edge_clients   = [net.adapter(f"edge-{i}",   ip=f"10.0.2.{i}") for i in range(1, 6)]
server         = net.adapter("server",   ip="10.0.0.1")

core_sw = net.switch("core-sw", ports=16, mode="store-and-forward")
edge_sw = net.switch("edge-sw", ports=8,  mode="store-and-forward")

for client in direct_clients:
    net.link(client,  core_sw, speed=100,   length=20)

for client in edge_clients:
    net.link(client,  edge_sw, speed=100,   length=10)

net.link(edge_sw, core_sw, speed=100,   length=5)    # ← bottleneck uplink
net.link(server,  core_sw, speed=1_000, length=2)

# ── Traffic ───────────────────────────────────────────────────────────────────
_FPS = int(100_000_000 / (1_518 * 8))   # ≈ 8 231 fps = 100 Mbps saturated

# Direct clients: one joins every 1 s starting at t = 0
for i, client in enumerate(direct_clients):
    client.sends(to=server, rate=_FPS, size=1_518, pattern="constant",
                 delay_ms=i * 1_000)

# Edge clients: one joins every 1 s starting at t = 12 s
for i, client in enumerate(edge_clients):
    client.sends(to=server, rate=_FPS, size=1_518, pattern="constant",
                 delay_ms=12_000 + i * 1_000)

# ── Observations ──────────────────────────────────────────────────────────────
# Switch queues — the contrast between them is the main story
net.observe("queue_depth",    on=core_sw, every=200)
net.observe("queue_depth",    on=edge_sw, every=200)

# Server throughput — shows the two-phase ramp
net.observe("throughput",     on=server,  every=500)
net.observe("bytes_received", on=server,  every=500)

# All clients — one line each so every node lights up in the topology
for client in direct_clients:
    net.observe("bytes_sent", on=client, every=500)
for client in edge_clients:
    net.observe("bytes_sent", on=client, every=500)

# ── Simulate ──────────────────────────────────────────────────────────────────
result = net.simulate(duration=40_000, live=True)
result.report()
