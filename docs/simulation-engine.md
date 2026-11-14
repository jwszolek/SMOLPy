# Simulation Engine

- **MAC-learning switch** — each switch pre-seeds its forwarding table from the topology wiring, eliminating spurious flooding toward silent endpoints (e.g. a server that only receives). Dynamic learning still operates for traffic through intermediate switches. With `mac_table="topology"` a switch instead starts with the port towards every end station (shortest path through switches, ties to the lowest port), i.e. already converged, and never floods unicast.
- **NIC filtering** — end stations accept only frames addressed to their own MAC; flooded frames for other stations are dropped and do not count towards metrics.
- **Store-and-forward model** — transmission delay + propagation delay per hop.
- **Queuing** — each link direction has its own egress queue with 8 strict 802.1Q priority classes (FIFO within a class, non-preemptive); `qos="fifo"` on a switch ignores priorities. `queue_depth` reports buffered frames at the switch's outbound ports.
- **Traffic shaping** — constant, Poisson, and Pareto-burst patterns; IMIX frame-size distribution.
- **Live mode** — simulation runs in 200 chunks (~8 s total wall time); the dashboard reads shared metric arrays between chunks via Python's GIL.

The engine runs a [Scenario](scenario-model.md), the execution-independent description that `Network.simulate()` builds from the network. It never reads the `Network` builder. It is the reference implementation of the [Model Semantics](model-semantics.md). It is powered by [SimPy](https://simpy.readthedocs.io/) — every node type (`Adapter`, `Switch`, `MQTTBroker`, `ModbusSlave`, `ProfinetDevice`) runs as one or more SimPy processes, and links are modelled as directed channels with their own transmission and propagation delay.
