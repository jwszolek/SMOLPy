# smolpy-engine-rs

The native simulation engine of [SMOLPy](https://github.com/jwszolek/SMOLPy), a network description language and discrete-event simulator for industrial networks. It is written in Rust and exposed to Python with PyO3 ([ADR-0008](https://jwszolek.github.io/SMOLPy/adr/0008-rust-des-engine-mvp/)).

```bash
pip install smolpy[rust]        # or: pip install smolpy smolpy-engine-rs
```

```python
result = net.simulate(duration=60_000, backend="rust")
```

- **The same model as SimPy.** SimPy, SMOLPy's reference engine, and this engine implement the same written contract, [Model Semantics](https://jwszolek.github.io/SMOLPy/model-semantics/). On deterministic scenarios they give the same results bit for bit, down to the event count. On random traffic, which uses each engine's own generator, they agree statistically.
- **About 40x faster.**
    - B1 at 10 M frames runs in 4.9 s, against 200.8 s on SimPy.
    - A 10 201-node plant (B4) at 100 M frames runs in 5.2 min, against 106 min.
- **Scope: the whole model, headless.**
    - Adapters, switches (both QoS modes, learning and converged MAC tables), links, and flows between any two end stations with every pattern and IMIX sizes.
    - Switch buffers: egress and ingress limits, in bytes, frames or cells, with tail drop or a resume threshold, a shared pool with reserves and drains, and the metrics `drops`, `drops_total`, `ingress_drops_total`, `egress_drops_total`, and the drop episodes and drop time of each scope.
    - The metrics `throughput`, `latency`, `bytes_sent`, `bytes_received`, `frames_sent`, `frames_received`, `queue_depth` and `utilization`.
    - Modbus TCP polling: slaves, open-loop polls and polls with one request in flight and a response timeout, `modbus_latency`, `modbus_unanswered`, `modbus_timeouts` and `modbus_skipped`, on a master or over the polls of a slave.
    - MQTT: brokers, publications with QoS 0 and 1, routes and `broker_queue`.
    - PROFINET RT: devices, controllers' ARs, the watchdog, `pn_latency`, `pn_jitter` and `pn_watchdog_expiries`.
    - The live and text dashboards need SimPy.
- **Versions.** The package is released together with `smolpy`, with the same version. It checks that it reads the Scenario IR version that `smolpy` writes.

One abi3 wheel per platform serves every Python from 3.10: Linux x86_64 and aarch64, macOS arm64 and x86_64, and Windows x86_64.
