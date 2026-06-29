from __future__ import annotations

import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generator

import simpy

if TYPE_CHECKING:
    from smolpy.dsl.adapter import Adapter
    from smolpy.dsl.hub import Hub
    from smolpy.dsl.network import Network, SimulationResult
    from smolpy.dsl.switch import Switch

_MAX_RETRIES = 16
_SLOT_TIME_US = 51.2  # 10 Mbps Ethernet slot time (µs)

Samples = list[tuple[float, float]]  # (time_ms, value)


# ---------------------------------------------------------------------------
# Frame — internal simulation packet
# ---------------------------------------------------------------------------

@dataclass
class _Frame:
    src_mac: str
    dst_mac: str
    size_bytes: int
    created_at_us: float
    retries: int = field(default=0)
    in_port: int = field(default=-1)  # port index at the receiving switch/hub
    mqtt_topic: str | None = field(default=None)
    mqtt_qos:   int        = field(default=0)


def _resolve_size(size: int | str, rng: random.Random) -> int:
    if isinstance(size, int):
        return size
    # IMIX: 40% 64 B, 57% 594 B, 3% 1518 B
    r = rng.random()
    if r < 0.40:
        return 64
    if r < 0.97:
        return 594
    return 1518


# ---------------------------------------------------------------------------
# Per-adapter runtime counters
# ---------------------------------------------------------------------------

class _AdapterCounters:
    def __init__(self) -> None:
        self.bytes_received: int = 0
        self.frames_received: int = 0
        self.latency_us: list[float] = []

    def record(self, frame: _Frame, now: float) -> None:
        self.bytes_received += frame.size_bytes
        self.frames_received += 1
        self.latency_us.append(now - frame.created_at_us)


# ---------------------------------------------------------------------------
# Link channel — one directed lane of a link
# ---------------------------------------------------------------------------

class _LinkChannel:
    """Simulates one direction of a cable: enqueue → tx delay → prop delay → deliver."""

    def __init__(
        self,
        env: simpy.Environment,
        speed_bps: float,
        prop_delay_us: float,
        in_port: int,
        dst_store: simpy.Store,
    ) -> None:
        self.env = env
        self.speed_bps = speed_bps
        self.prop_delay_us = prop_delay_us
        self.in_port = in_port
        self.dst_store = dst_store
        self.queue: simpy.Store = simpy.Store(env)
        self._medium = simpy.Resource(env, capacity=1)
        self.bits_sent: int = 0
        env.process(self._run())

    def enqueue(self, frame: _Frame) -> None:
        self.queue.put(frame)

    def _run(self) -> Generator:
        while True:
            frame: _Frame = yield self.queue.get()
            with self._medium.request() as req:
                yield req
                tx_us = (frame.size_bytes * 8) / self.speed_bps * 1_000_000
                yield self.env.timeout(tx_us)
                self.bits_sent += frame.size_bytes * 8
            yield self.env.timeout(self.prop_delay_us)
            # Copy frame so flooding to multiple channels doesn't create in_port conflicts
            delivered = _Frame(
                src_mac=frame.src_mac,
                dst_mac=frame.dst_mac,
                size_bytes=frame.size_bytes,
                created_at_us=frame.created_at_us,
                retries=frame.retries,
                in_port=self.in_port,
                mqtt_topic=frame.mqtt_topic,
                mqtt_qos=frame.mqtt_qos,
            )
            self.dst_store.put(delivered)


# ---------------------------------------------------------------------------
# SimPy processes
# ---------------------------------------------------------------------------

def _bytes_rx_sampler(
    env: simpy.Environment,
    counters: _AdapterCounters,
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        samples.append((env.now / 1_000, counters.bytes_received / 1_000_000))


def _bytes_tx_sampler(
    env: simpy.Environment,
    channel: _LinkChannel,
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        samples.append((env.now / 1_000, channel.bits_sent / 8 / 1_000_000))


def _traffic_gen(
    env: simpy.Environment,
    src_mac: str,
    dst_mac: str,
    rate_fps: float,
    size_spec: int | str,
    pattern: str,
    channel: _LinkChannel,
    rng: random.Random,
    delay_us: float = 0.0,
) -> Generator:
    if delay_us > 0.0:
        yield env.timeout(delay_us)
    interval_us = 1_000_000.0 / rate_fps
    while True:
        if pattern == "bursty":
            # ON: Pareto-distributed burst at 4× rate; OFF: proportional silence
            burst_len = max(1, int(rng.paretovariate(1.5)))
            for _ in range(burst_len):
                yield env.timeout(interval_us / 4)
                channel.enqueue(_Frame(
                    src_mac=src_mac, dst_mac=dst_mac,
                    size_bytes=_resolve_size(size_spec, rng),
                    created_at_us=env.now,
                ))
            yield env.timeout(interval_us * burst_len * rng.uniform(0.5, 2.0))
        else:
            if pattern == "constant":
                yield env.timeout(interval_us)
            else:  # poisson
                yield env.timeout(rng.expovariate(1.0 / interval_us))
            channel.enqueue(_Frame(
                src_mac=src_mac, dst_mac=dst_mac,
                size_bytes=_resolve_size(size_spec, rng),
                created_at_us=env.now,
            ))


def _mqtt_traffic_gen(
    env: simpy.Environment,
    src_mac: str,
    dst_mac: str,
    topic: str,
    rate_hz: float,
    frame_size: int,
    qos: int,
    channel: _LinkChannel,
    delay_us: float = 0.0,
) -> Generator:
    """Periodic MQTT PUBLISH traffic (constant-rate, sensor-style)."""
    if delay_us > 0.0:
        yield env.timeout(delay_us)
    interval_us = 1_000_000.0 / rate_hz
    while True:
        yield env.timeout(interval_us)
        channel.enqueue(_Frame(
            src_mac=src_mac,
            dst_mac=dst_mac,
            size_bytes=frame_size,
            created_at_us=env.now,
            mqtt_topic=topic,
            mqtt_qos=qos,
        ))


def _mqtt_broker_forwarder(
    env: simpy.Environment,
    inbound: simpy.Store,
    broker_mac: str,
    routes: dict[str, list],      # topic → [Adapter, ...]
    out_channel: _LinkChannel,    # broker's outbound channel to nearest switch
) -> Generator:
    """MQTT broker: receives PUBLISH, fans out to subscribers, ACKs QoS 1."""
    # Pre-build topic → [subscriber_mac, ...]
    topic_subs: dict[str, list[str]] = {
        topic: [a.mac for a in adapters]
        for topic, adapters in routes.items()
    }
    while True:
        frame: _Frame = yield inbound.get()
        if frame.mqtt_topic is None:
            continue  # not an MQTT frame
        for sub_mac in topic_subs.get(frame.mqtt_topic, []):
            out_channel.enqueue(_Frame(
                src_mac=broker_mac,
                dst_mac=sub_mac,
                size_bytes=frame.size_bytes,
                created_at_us=frame.created_at_us,   # preserve original timestamp for latency
                mqtt_topic=frame.mqtt_topic,
            ))
        # QoS 1 → PUBACK (4-byte MQTT header + Ethernet/IP/TCP = 58 bytes total)
        if frame.mqtt_qos >= 1:
            out_channel.enqueue(_Frame(
                src_mac=broker_mac,
                dst_mac=frame.src_mac,
                size_bytes=58,
                created_at_us=env.now,
            ))


def _broker_queue_sampler(
    env: simpy.Environment,
    inbound: simpy.Store,
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        samples.append((env.now / 1_000, float(len(inbound.items))))


def _switch_forwarder(
    env: simpy.Environment,
    inbound: simpy.Store,
    out_channels: dict[int, _LinkChannel],
    initial_mac: dict[str, int] | None = None,
) -> Generator:
    """MAC-learning store-and-forward switch.

    initial_mac pre-seeds the table for adapters whose MACs are known from
    topology wiring, preventing spurious flooding toward silent endpoints.
    """
    mac_table: dict[str, int] = dict(initial_mac) if initial_mac else {}
    while True:
        frame: _Frame = yield inbound.get()
        mac_table[frame.src_mac] = frame.in_port
        dst_port = mac_table.get(frame.dst_mac)
        if dst_port is None:
            # Unknown destination: flood to all ports except the one it arrived on
            for port, ch in out_channels.items():
                if port != frame.in_port:
                    ch.enqueue(frame)
        elif dst_port != frame.in_port:
            out_channels[dst_port].enqueue(frame)
        # dst_port == in_port → hairpin, drop silently


def _hub_broadcaster(
    env: simpy.Environment,
    inbound: simpy.Store,
    out_channels: dict[int, _LinkChannel],
    collisions: list[int],
) -> Generator:
    """Layer-1 hub: broadcast to all ports except source.

    Collision approximation: if a frame is already queued when a new one
    arrives, count it as a collision. Full CSMA/CD backoff is deferred.
    """
    while True:
        frame: _Frame = yield inbound.get()
        if inbound.items:
            collisions[0] += 1
        for port, ch in out_channels.items():
            if port != frame.in_port:
                ch.enqueue(frame)


def _adapter_receiver(
    env: simpy.Environment,
    inbound: simpy.Store,
    counters: _AdapterCounters,
) -> Generator:
    while True:
        frame: _Frame = yield inbound.get()
        counters.record(frame, env.now)


def _throughput_sampler(
    env: simpy.Environment,
    counters: _AdapterCounters,
    interval_us: float,
    samples: Samples,
) -> Generator:
    last_bytes = 0
    while True:
        yield env.timeout(interval_us)
        delta = counters.bytes_received - last_bytes
        last_bytes = counters.bytes_received
        mbps = (delta * 8) / (interval_us / 1e6) / 1e6
        samples.append((env.now / 1_000, mbps))


def _latency_sampler(
    env: simpy.Environment,
    counters: _AdapterCounters,
    interval_us: float,
    samples: Samples,
) -> Generator:
    last_idx = 0
    while True:
        yield env.timeout(interval_us)
        window = counters.latency_us[last_idx:]
        last_idx = len(counters.latency_us)
        if window:
            avg = sum(window) / len(window)
            samples.append((env.now / 1_000, avg))


def _queue_depth_sampler(
    env: simpy.Environment,
    out_channels: dict[int, _LinkChannel],
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        depth = sum(len(ch.queue.items) for ch in out_channels.values())
        samples.append((env.now / 1_000, float(depth)))


def _collision_rate_sampler(
    env: simpy.Environment,
    collisions: list[int],
    interval_us: float,
    samples: Samples,
) -> Generator:
    last = 0
    while True:
        yield env.timeout(interval_us)
        delta = collisions[0] - last
        last = collisions[0]
        rate = delta / (interval_us / 1e6)
        samples.append((env.now / 1_000, rate))


def _utilization_sampler(
    env: simpy.Environment,
    channels: list[_LinkChannel],
    interval_us: float,
    samples: Samples,
) -> Generator:
    last_bits = [0] * len(channels)
    while True:
        yield env.timeout(interval_us)
        current_bits = [ch.bits_sent for ch in channels]
        delta = sum(current_bits) - sum(last_bits)
        last_bits = current_bits
        capacity = sum(ch.speed_bps * (interval_us / 1e6) for ch in channels)
        util = (delta / capacity * 100) if capacity > 0 else 0.0
        samples.append((env.now / 1_000, util))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_simulation(
    network: Network,
    duration_ms: float,
    *,
    _all_samples: dict[str, Samples] | None = None,
    _sim_state: dict | None = None,
    _n_chunks: int = 1,
) -> SimulationResult:
    from smolpy.dsl.adapter import Adapter
    from smolpy.dsl.hub import Hub
    from smolpy.dsl.mqtt_broker import MQTTBroker
    from smolpy.dsl.network import SimulationResult
    from smolpy.dsl.switch import Switch

    env = simpy.Environment()
    duration_us = duration_ms * 1_000
    rng = random.Random(42)

    # Every node gets an inbound store where delivered frames arrive
    inbound: dict[str, simpy.Store] = {
        name: simpy.Store(env) for name in network._nodes
    }

    adapter_counters: dict[str, _AdapterCounters] = {
        name: _AdapterCounters()
        for name, node in network._nodes.items()
        if isinstance(node, Adapter)
    }

    hub_collisions: dict[str, list[int]] = {
        name: [0]
        for name, node in network._nodes.items()
        if isinstance(node, Hub)
    }

    # Build directed link channels from topology
    next_port: dict[str, int] = defaultdict(int)
    out_channels: dict[str, dict[int, _LinkChannel]] = defaultdict(dict)
    in_channels: dict[str, list[_LinkChannel]] = defaultdict(list)
    adapter_out: dict[str, _LinkChannel] = {}  # single outbound channel per leaf adapter
    static_macs: dict[str, dict[str, int]] = defaultdict(dict)  # switch_name → {mac: port}

    for link in network._links:
        a, b = link.endpoint_a, link.endpoint_b
        port_a = next_port[a.name]; next_port[a.name] += 1
        port_b = next_port[b.name]; next_port[b.name] += 1

        ch_ab = _LinkChannel(env, link.speed_bps, link.propagation_delay_us, port_b, inbound[b.name])
        ch_ba = _LinkChannel(env, link.speed_bps, link.propagation_delay_us, port_a, inbound[a.name])

        out_channels[a.name][port_a] = ch_ab
        out_channels[b.name][port_b] = ch_ba
        in_channels[b.name].append(ch_ab)
        in_channels[a.name].append(ch_ba)

        if isinstance(a, (Adapter, MQTTBroker)):
            adapter_out[a.name] = ch_ab
            if isinstance(b, Switch):
                static_macs[b.name][a.mac] = port_b
        if isinstance(b, (Adapter, MQTTBroker)):
            adapter_out[b.name] = ch_ba
            if isinstance(a, Switch):
                static_macs[a.name][b.mac] = port_a

    # Start one process per node
    for name, node in network._nodes.items():
        if isinstance(node, Adapter):
            env.process(_adapter_receiver(env, inbound[name], adapter_counters[name]))
            ch = adapter_out.get(name)
            if ch:
                for spec in node.traffic_specs:
                    env.process(_traffic_gen(
                        env, node.mac, spec.destination.mac,
                        spec.rate, spec.size, spec.pattern, ch, rng,
                        delay_us=spec.delay_ms * 1_000,
                    ))
                for spec in node.mqtt_specs:
                    env.process(_mqtt_traffic_gen(
                        env, node.mac, spec.broker.mac,
                        spec.topic, spec.rate_hz, spec.frame_size, spec.qos, ch,
                        delay_us=spec.delay_ms * 1_000,
                    ))
        elif isinstance(node, MQTTBroker):
            ch = adapter_out.get(name)
            if ch:
                env.process(_mqtt_broker_forwarder(
                    env, inbound[name], node.mac, node._routes, ch,
                ))
        elif isinstance(node, Switch):
            env.process(_switch_forwarder(env, inbound[name], out_channels[name], static_macs.get(name)))
        elif isinstance(node, Hub):
            env.process(_hub_broadcaster(env, inbound[name], out_channels[name], hub_collisions[name]))

    # Start metric samplers
    # Reuse an externally supplied dict (live mode) or create a fresh one.
    all_samples: dict[str, Samples] = _all_samples if _all_samples is not None else {}

    for obs in network._observations:
        target = obs.target
        interval_us = obs.interval_ms * 1_000
        key = f"{obs.metric}:{target.name}"
        if key not in all_samples:
            all_samples[key] = []
        samples = all_samples[key]

        if obs.metric == "throughput" and isinstance(target, Adapter):
            env.process(_throughput_sampler(env, adapter_counters[target.name], interval_us, samples))
        elif obs.metric == "latency" and isinstance(target, Adapter):
            env.process(_latency_sampler(env, adapter_counters[target.name], interval_us, samples))
        elif obs.metric == "queue_depth" and isinstance(target, Switch):
            env.process(_queue_depth_sampler(env, out_channels[target.name], interval_us, samples))
        elif obs.metric == "collision_rate" and isinstance(target, Hub):
            env.process(_collision_rate_sampler(env, hub_collisions[target.name], interval_us, samples))
        elif obs.metric == "utilization":
            chs = in_channels.get(target.name, [])
            if chs:
                env.process(_utilization_sampler(env, chs, interval_us, samples))
        elif obs.metric == "bytes_received" and isinstance(target, Adapter):
            env.process(_bytes_rx_sampler(env, adapter_counters[target.name], interval_us, samples))
        elif obs.metric == "bytes_sent" and isinstance(target, Adapter):
            ch = adapter_out.get(target.name)
            if ch:
                env.process(_bytes_tx_sampler(env, ch, interval_us, samples))
        elif obs.metric == "broker_queue" and isinstance(target, MQTTBroker):
            env.process(_broker_queue_sampler(env, inbound[target.name], interval_us, samples))

    if _n_chunks > 1:
        chunk_us = duration_us / _n_chunks
        sleep_s  = 8.0 / _n_chunks  # target ~8 s total wall time
        for i in range(_n_chunks):
            if _sim_state is not None and _sim_state.get("stop", False):
                break
            while _sim_state is not None and _sim_state.get("paused", False):
                time.sleep(0.05)
            env.run(until=(i + 1) * chunk_us)
            if _sim_state is not None:
                _sim_state["progress"] = (i + 1) / _n_chunks
            time.sleep(sleep_s)
    else:
        env.run(until=duration_us)

    if _sim_state is not None:
        _sim_state["done"] = True

    return SimulationResult(metrics=all_samples, network=network)
