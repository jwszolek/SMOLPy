# Simulation Engine

- **MAC-learning switch** — each switch pre-seeds its forwarding table from the topology wiring, eliminating spurious flooding toward silent endpoints (e.g. a server that only receives). Dynamic learning still operates for traffic through intermediate switches.
- **Store-and-forward model** — transmission delay + propagation delay per hop.
- **Queuing** — each link direction is an independent SimPy Store; `queue_depth` reports buffered frames at the switch's outbound ports.
- **Traffic shaping** — constant, Poisson, and Pareto-burst patterns; IMIX frame-size distribution.
- **Live mode** — simulation runs in 200 chunks (~8 s total wall time); the dashboard reads shared metric arrays between chunks via Python's GIL.

The engine is powered by [SimPy](https://simpy.readthedocs.io/) — every node type (`Adapter`, `Switch`, `Hub`, `MQTTBroker`, `ModbusSlave`) runs as one or more SimPy processes, and links are modelled as directed channels with their own transmission and propagation delay.
