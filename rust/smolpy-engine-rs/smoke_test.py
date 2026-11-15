"""Smoke test of an installed smolpy-engine-rs wheel (ADR-0008, step 7).

Runs a small network, a deterministic flow through a learning switch plus a
random one, on SimPy and on the Rust engine. The deterministic metrics must
be identical bit for bit and the random ones must come out at all. CI runs
it on every platform a wheel is built for that the runner can execute.

    python rust/smolpy-engine-rs/smoke_test.py
"""

from __future__ import annotations

import platform

import smolpy_engine_rs

from smolpy import Network, backends


def main() -> None:
    assert "rust" in backends.installed(), "the rust backend is not available"

    net = Network("smoke")
    sw = net.switch("sw", ports=3)
    a = net.adapter("a", ip="10.0.0.1")
    b = net.adapter("b", ip="10.0.0.2")
    c = net.adapter("c", ip="10.0.0.3")
    for node in (a, b, c):
        net.link(node, sw, speed=100, length=10)
    a.sends(b, rate=5_000, size=512, priority=3)
    c.sends(b, rate=2_000, size=1_200, delay_ms=0.5)
    for metric in ("throughput", "latency", "bytes_received"):
        net.observe(metric, on=b, every=5)
    net.observe("queue_depth", on=sw, every=1)
    net.observe("utilization", on=sw, every=5)
    scenario = net.to_scenario(duration=200)

    simpy = backends.run(scenario, "simpy")
    rust = backends.run(scenario, "rust")
    assert rust.metrics == simpy.metrics, "the engines disagree on a deterministic scenario"
    assert rust.stats is not None and rust.stats.events == simpy.stats.events  # type: ignore[union-attr]

    a.sends(c, rate=1_000, size="imix", pattern="poisson")
    net.observe("latency", on=c, every=5)
    random = backends.run(net.to_scenario(duration=200), "rust")
    assert random.metrics["latency:c"], "the random flow produced no samples"

    print(
        f"smolpy-engine-rs {smolpy_engine_rs.__version__} on {platform.platform()}"
        f" ({platform.machine()}): bit-exact with SimPy, {rust.stats.events} events"
    )


if __name__ == "__main__":
    main()
