from smolpy import Network

net = Network("office-net")
host_a = net.adapter("host-A", ip="10.0.0.1")
host_b = net.adapter("host-B", ip="10.0.0.2")
server  = net.adapter("server",  ip="10.0.0.10")
sw1     = net.switch("sw1", ports=8, mode="store-and-forward")

net.link(host_a, sw1, speed=1000,  length=5)
net.link(host_b, sw1, speed=1000,  length=10)
net.link(server, sw1, speed=10_000, length=2)

host_a.sends(to=server, rate=500, size=512,    pattern="constant")
host_b.sends(to=server, rate=200, size="imix", pattern="poisson")

net.observe("throughput",  on=server,  every=100)
net.observe("latency",     on=server,  every=100)
net.observe("queue_depth", on=sw1,     every=10)
net.observe("utilization", on=server,  every=100)

result = net.simulate(duration=10_000, live=True)
result.report()
