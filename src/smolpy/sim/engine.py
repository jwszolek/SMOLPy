from __future__ import annotations

import copy
import itertools
import random
import time
from collections import defaultdict
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import simpy

if TYPE_CHECKING:
    from smolpy.core.batch import BatchSpec
    from smolpy.core.network import Network, SimulationResult

_MAX_RETRIES = 16
_SLOT_TIME_US = 51.2  # 10 Mbps Ethernet slot time (µs)

Samples = list[tuple[float, float]]  # (time_ms, value)


# ---------------------------------------------------------------------------
# Frame — internal simulation packet
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Frame:
    src_mac: str
    dst_mac: str
    size_bytes: int
    created_at_us: float
    retries: int = field(default=0)
    in_port: int = field(default=-1)  # port index at the receiving switch/hub
    mqtt_topic: str | None = field(default=None)
    mqtt_qos: int = field(default=0)
    modbus_unit_id: int | None = field(default=None)
    modbus_register_count: int = field(default=0)
    modbus_is_response: bool = field(default=False)
    priority: int = field(default=0)  # 802.1Q PCP 0-7
    pn_ar_id: int | None = field(default=None)  # PROFINET RT AR this frame belongs to
    pn_cycle_counter: int = field(default=0)


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


# Per-frame values (latency, RTT, jitter) are not kept for the whole run.
# Each observing sampler registers its own window buffer in a list of sinks
# and clears it after every sample; with no observer the value is not even
# computed. Memory therefore depends on the sampling window, not on the
# run length (ADR-0006 §5).
Sinks = list[list[float]]


def _emit(sinks: Sinks, value: float) -> None:
    for window in sinks:
        window.append(value)


class _AdapterCounters:
    def __init__(self) -> None:
        self.bytes_received: int = 0
        self.frames_received: int = 0
        self.latency_sinks: Sinks = []
        self.modbus_rtt_sinks: Sinks = []

    def record(self, frame: _Frame, now: float) -> None:
        self.bytes_received += frame.size_bytes
        self.frames_received += 1
        if self.latency_sinks:
            _emit(self.latency_sinks, now - frame.created_at_us)
        if frame.modbus_is_response and self.modbus_rtt_sinks:
            _emit(self.modbus_rtt_sinks, now - frame.created_at_us)


# ---------------------------------------------------------------------------
# Link channel — one directed lane of a link
# ---------------------------------------------------------------------------


class _LinkChannel:
    """Simulates one direction of a cable: enqueue → tx delay → prop delay → deliver.

    The egress queue serves 802.1Q priority classes strictly (higher first,
    FIFO within a class, non-preemptive). With strict_priority=False every
    frame is treated as priority 0, i.e. plain FIFO.
    """

    def __init__(
        self,
        env: simpy.Environment,
        speed_bps: float,
        prop_delay_us: float,
        in_port: int,
        dst_store: simpy.Store,
        strict_priority: bool = True,
    ) -> None:
        self.env = env
        self.speed_bps = speed_bps
        self.prop_delay_us = prop_delay_us
        self.in_port = in_port
        self.dst_store = dst_store
        self.strict_priority = strict_priority
        self.queue: simpy.PriorityStore = simpy.PriorityStore(env)
        # PriorityItem compares on the key only, so the sequence number is what
        # keeps equal-priority frames in FIFO order.
        self._seq = itertools.count()
        self.bits_sent: int = 0
        env.process(self._run())

    def enqueue(self, frame: _Frame) -> None:
        cls = -frame.priority if self.strict_priority else 0
        self.queue.put(simpy.PriorityItem((cls, next(self._seq)), frame))

    def _run(self) -> Generator:
        # Serialization (tx) is the only thing that occupies the wire: the next
        # frame's transmission starts as soon as this one's bits have been put
        # on the wire. Propagation delay is a fixed, one-time latency incurred
        # by that frame in flight, not a hold on the link, so it elapses
        # in parallel instead of blocking this loop — otherwise it would be
        # spent serially on top of transmission time and understate capacity
        # (a 2 m/10 ns link previously cost ~2.4 % of throughput at 10 Gb/s).
        #
        # The propagation delay is a single timeout carrying the frame, with
        # delivery as its callback. A process per frame would cost a Process
        # object, its Initialize event and a generator resume on every hop
        # (ADR-0006 §5).
        while True:
            item = yield self.queue.get()
            frame: _Frame = item.item
            tx_us = (frame.size_bytes * 8) / self.speed_bps * 1_000_000
            yield self.env.timeout(tx_us)
            self.bits_sent += frame.size_bytes * 8
            in_flight = self.env.timeout(self.prop_delay_us, frame)
            in_flight.callbacks.append(self._deliver)

    def _deliver(self, in_flight: simpy.Event) -> None:
        frame: _Frame = in_flight.value
        # The frame is updated in place: a frame object is only ever in one
        # place at a time, because senders build a new frame per transmission
        # and flooding (_replicate) gives every port its own copy.
        frame.in_port = self.in_port
        self.dst_store.put(frame)


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
    batch: BatchSpec | None = None,  # only for pattern == "batch"
    priority: int = 0,
) -> Generator:
    if delay_us > 0.0:
        yield env.timeout(delay_us)
    interval_us = 1_000_000.0 / rate_fps
    if pattern == "batch":
        assert batch is not None, "pattern='batch' needs a BatchSpec"
        # Compound Poisson: groups arrive at rate rate/E[X]; each group's X
        # frames are enqueued back to back at the same instant.
        group_interval_us = interval_us * batch.mean()
        while True:
            yield env.timeout(rng.expovariate(1.0 / group_interval_us))
            for _ in range(batch.draw(rng)):
                channel.enqueue(
                    _Frame(
                        src_mac=src_mac,
                        dst_mac=dst_mac,
                        size_bytes=_resolve_size(size_spec, rng),
                        created_at_us=env.now,
                        priority=priority,
                    )
                )
    while True:
        if pattern == "bursty":
            # ON: Pareto-distributed burst at 4× rate; OFF: proportional silence
            burst_len = max(1, int(rng.paretovariate(1.5)))
            for _ in range(burst_len):
                yield env.timeout(interval_us / 4)
                channel.enqueue(
                    _Frame(
                        src_mac=src_mac,
                        dst_mac=dst_mac,
                        size_bytes=_resolve_size(size_spec, rng),
                        created_at_us=env.now,
                        priority=priority,
                    )
                )
            yield env.timeout(interval_us * burst_len * rng.uniform(0.5, 2.0))
        else:
            if pattern == "constant":
                yield env.timeout(interval_us)
            else:  # poisson
                yield env.timeout(rng.expovariate(1.0 / interval_us))
            channel.enqueue(
                _Frame(
                    src_mac=src_mac,
                    dst_mac=dst_mac,
                    size_bytes=_resolve_size(size_spec, rng),
                    created_at_us=env.now,
                    priority=priority,
                )
            )


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
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=frame_size,
                created_at_us=env.now,
                mqtt_topic=topic,
                mqtt_qos=qos,
            )
        )


def _modbus_poll_gen(
    env: simpy.Environment,
    src_mac: str,
    dst_mac: str,
    unit_id: int,
    register_count: int,
    frame_size: int,
    rate_hz: float,
    channel: _LinkChannel,
    delay_us: float = 0.0,
) -> Generator:
    """Periodic Modbus TCP poll request (Read Holding Registers, FC 03)."""
    if delay_us > 0.0:
        yield env.timeout(delay_us)
    interval_us = 1_000_000.0 / rate_hz
    while True:
        yield env.timeout(interval_us)
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=frame_size,
                created_at_us=env.now,
                modbus_unit_id=unit_id,
                modbus_register_count=register_count,
            )
        )


def _modbus_slave_responder(
    env: simpy.Environment,
    inbound: simpy.Store,
    slave_mac: str,
    unit_id: int,
    out_channel: _LinkChannel,  # slave's outbound channel toward the master
) -> Generator:
    """Modbus slave: replies to Read Holding Registers requests addressed to unit_id."""
    while True:
        frame: _Frame = yield inbound.get()
        if frame.dst_mac != slave_mac:
            continue  # flooded frame for another station: the NIC drops it
        if frame.modbus_unit_id != unit_id:
            continue  # not a Modbus frame, or addressed to a different slave
        response_size = 62 + 2 * frame.modbus_register_count
        out_channel.enqueue(
            _Frame(
                src_mac=slave_mac,
                dst_mac=frame.src_mac,
                size_bytes=response_size,
                created_at_us=frame.created_at_us,  # preserve original timestamp for RTT
                modbus_unit_id=unit_id,
                modbus_register_count=frame.modbus_register_count,
                modbus_is_response=True,
            )
        )


def _mqtt_broker_forwarder(
    env: simpy.Environment,
    inbound: simpy.Store,
    broker_mac: str,
    routes: dict[str, list],  # topic → [Adapter, ...]
    out_channel: _LinkChannel,  # broker's outbound channel to nearest switch
) -> Generator:
    """MQTT broker: receives PUBLISH, fans out to subscribers, ACKs QoS 1."""
    # Pre-build topic → [subscriber_mac, ...]
    topic_subs: dict[str, list[str]] = {
        topic: [a.mac for a in adapters] for topic, adapters in routes.items()
    }
    while True:
        frame: _Frame = yield inbound.get()
        if frame.dst_mac != broker_mac:
            continue  # flooded frame for another station: the NIC drops it
        if frame.mqtt_topic is None:
            continue  # not an MQTT frame
        for sub_mac in topic_subs.get(frame.mqtt_topic, []):
            out_channel.enqueue(
                _Frame(
                    src_mac=broker_mac,
                    dst_mac=sub_mac,
                    size_bytes=frame.size_bytes,
                    created_at_us=frame.created_at_us,  # preserve original timestamp for latency
                    mqtt_topic=frame.mqtt_topic,
                )
            )
        # QoS 1 → PUBACK (4-byte MQTT header + Ethernet/IP/TCP = 58 bytes total)
        if frame.mqtt_qos >= 1:
            out_channel.enqueue(
                _Frame(
                    src_mac=broker_mac,
                    dst_mac=frame.src_mac,
                    size_bytes=58,
                    created_at_us=env.now,
                )
            )


def _broker_queue_sampler(
    env: simpy.Environment,
    inbound: simpy.Store,
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        samples.append((env.now / 1_000, float(len(inbound.items))))


class _PnAR:
    """Consumer-side state of one PROFINET RT data direction."""

    def __init__(self, cycle_us: float, hold: int) -> None:
        self.cycle_us = cycle_us
        self.hold = hold
        self.last_arrival: float | None = None
        self.arrival: simpy.Event | None = None


class _PnConsumer:
    """Consumer of PROFINET RT cyclic data for one node, across all its ARs."""

    def __init__(self, env: simpy.Environment) -> None:
        self.env = env
        self.ars: dict[int, _PnAR] = {}
        self.latency_sinks: Sinks = []
        self.jitter_sinks: Sinks = []
        self.watchdog_expiries: int = 0

    def expect(self, ar_id: int, cycle_us: float, hold: int) -> None:
        self.ars[ar_id] = _PnAR(cycle_us, hold)

    def record(self, frame: _Frame, now: float) -> None:
        ar = self.ars.get(frame.pn_ar_id) if frame.pn_ar_id is not None else None
        if ar is None:
            return
        if self.latency_sinks:
            _emit(self.latency_sinks, now - frame.created_at_us)
        if ar.last_arrival is None:
            # Supervision starts with the first frame, as after AR establishment.
            self.env.process(self._watchdog(ar))
        elif self.jitter_sinks:
            _emit(self.jitter_sinks, abs(now - ar.last_arrival - ar.cycle_us))
        ar.last_arrival = now
        if ar.arrival is not None and not ar.arrival.triggered:
            ar.arrival.succeed()

    def _watchdog(self, ar: _PnAR) -> Generator:
        timeout_us = ar.hold * ar.cycle_us
        while True:
            ar.arrival = self.env.event()
            fired = yield ar.arrival | self.env.timeout(timeout_us)
            if ar.arrival in fired:
                continue
            # Real devices abort the AR here; we count it and keep exchanging
            # (ADR-0005), re-arming once data flows again.
            self.watchdog_expiries += 1
            yield ar.arrival


def _pn_provider(
    env: simpy.Environment,
    src_mac: str,
    dst_mac: str,
    ar_id: int,
    frame_size: int,
    cycle_us: float,
    channel: _LinkChannel,
    priority: int,
    delay_us: float = 0.0,
) -> Generator:
    """PROFINET RT provider: one cyclic I/O frame every update time."""
    if delay_us > 0.0:
        yield env.timeout(delay_us)
    counter = 0
    while True:
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=frame_size,
                created_at_us=env.now,
                priority=priority,
                pn_ar_id=ar_id,
                pn_cycle_counter=counter,
            )
        )
        counter = (counter + 1) & 0xFFFF
        yield env.timeout(cycle_us)


def _pn_device_receiver(
    env: simpy.Environment,
    inbound: simpy.Store,
    mac: str,
    consumer: _PnConsumer,
) -> Generator:
    while True:
        frame: _Frame = yield inbound.get()
        if frame.dst_mac != mac:
            continue  # flooded frame for another station: the NIC drops it
        consumer.record(frame, env.now)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _window_sampler(
    env: simpy.Environment,
    sinks: Sinks,
    aggregate: Callable[[list[float]], float],
    interval_us: float,
    samples: Samples,
) -> Generator:
    """Aggregate the values emitted into *sinks* during each sampling window."""
    window: list[float] = []
    sinks.append(window)  # registered now, before any frame can arrive
    return _window_loop(env, window, aggregate, interval_us, samples)


def _window_loop(
    env: simpy.Environment,
    window: list[float],
    aggregate: Callable[[list[float]], float],
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        if window:
            samples.append((env.now / 1_000, aggregate(window)))
            window.clear()


def _pn_expiries_sampler(
    env: simpy.Environment,
    consumer: _PnConsumer,
    interval_us: float,
    samples: Samples,
) -> Generator:
    while True:
        yield env.timeout(interval_us)
        samples.append((env.now / 1_000, float(consumer.watchdog_expiries)))


def _replicate(frame: _Frame, out_channels: dict[int, _LinkChannel]) -> None:
    """Send a copy of *frame* to every port except the one it arrived on."""
    for port, ch in out_channels.items():
        if port != frame.in_port:
            ch.enqueue(copy.copy(frame))


def _topology_mac_table(
    network: Network, switch: str, neighbours: dict[str, list[tuple[int, str]]]
) -> dict[str, int]:
    """Port towards every reachable end station, by breadth-first search.

    Switches and hubs are traversed, end stations are not. The first port to
    reach a station wins: shortest path, ties to the lowest port index.
    """
    from smolpy.ethernet.hub import Hub
    from smolpy.ethernet.switch import Switch

    table: dict[str, int] = {}
    seen = {switch}
    frontier = sorted(neighbours[switch])
    for _, peer in frontier:
        seen.add(peer)
    while frontier:
        nxt: list[tuple[int, str]] = []
        for port, name in frontier:
            node = network._nodes[name]
            if isinstance(node, (Switch, Hub)):
                for _, peer in neighbours[name]:
                    if peer not in seen:
                        seen.add(peer)
                        nxt.append((port, peer))
            else:
                table.setdefault(node.mac, port)  # type: ignore[attr-defined]
        frontier = nxt
    return table


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
            _replicate(frame, out_channels)
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
        _replicate(frame, out_channels)


def _adapter_receiver(
    env: simpy.Environment,
    inbound: simpy.Store,
    mac: str,
    counters: _AdapterCounters,
    pn: _PnConsumer,
) -> Generator:
    while True:
        frame: _Frame = yield inbound.get()
        # A NIC accepts only frames addressed to its own MAC. Frames that reach
        # it because a switch flooded them (unknown destination) or a hub
        # repeated them are dropped and do not count towards any metric.
        if frame.dst_mac != mac:
            continue
        counters.record(frame, env.now)
        if frame.pn_ar_id is not None:
            pn.record(frame, env.now)


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
    seed: int = 42,
    _all_samples: dict[str, Samples] | None = None,
    _sim_state: dict | None = None,
    _n_chunks: int = 1,
) -> SimulationResult:
    from smolpy.core.adapter import PN_RT_PRIORITY, Adapter
    from smolpy.core.network import SimulationResult
    from smolpy.ethernet.hub import Hub
    from smolpy.ethernet.switch import Switch
    from smolpy.modbus.slave import ModbusSlave
    from smolpy.mqtt.broker import MQTTBroker
    from smolpy.profinet.device import ProfinetDevice

    endpoint_types = (Adapter, MQTTBroker, ModbusSlave, ProfinetDevice)

    env = simpy.Environment()
    duration_us = duration_ms * 1_000
    rng = random.Random(seed)

    # Every node gets an inbound store where delivered frames arrive
    inbound: dict[str, simpy.Store] = {name: simpy.Store(env) for name in network._nodes}

    adapter_counters: dict[str, _AdapterCounters] = {
        name: _AdapterCounters()
        for name, node in network._nodes.items()
        if isinstance(node, Adapter)
    }

    hub_collisions: dict[str, list[int]] = {
        name: [0] for name, node in network._nodes.items() if isinstance(node, Hub)
    }

    pn_consumers: dict[str, _PnConsumer] = {
        name: _PnConsumer(env)
        for name, node in network._nodes.items()
        if isinstance(node, (Adapter, ProfinetDevice))
    }

    def _strict(node: object) -> bool:
        return not (isinstance(node, Switch) and node.qos == "fifo")

    # Build directed link channels from topology
    next_port: dict[str, int] = defaultdict(int)
    out_channels: dict[str, dict[int, _LinkChannel]] = defaultdict(dict)
    in_channels: dict[str, list[_LinkChannel]] = defaultdict(list)
    adapter_out: dict[str, _LinkChannel] = {}  # single outbound channel per leaf adapter
    static_macs: dict[str, dict[str, int]] = defaultdict(dict)  # switch_name → {mac: port}
    neighbours: dict[str, list[tuple[int, str]]] = defaultdict(list)  # name → [(port, peer)]

    for link in network._links:
        a, b = link.endpoint_a, link.endpoint_b
        port_a = next_port[a.name]
        next_port[a.name] += 1
        port_b = next_port[b.name]
        next_port[b.name] += 1

        # ch_ab is a's egress queue, ch_ba is b's — each follows its owner's QoS.
        ch_ab = _LinkChannel(
            env, link.speed_bps, link.propagation_delay_us, port_b, inbound[b.name], _strict(a)
        )
        ch_ba = _LinkChannel(
            env, link.speed_bps, link.propagation_delay_us, port_a, inbound[a.name], _strict(b)
        )

        out_channels[a.name][port_a] = ch_ab
        out_channels[b.name][port_b] = ch_ba
        in_channels[b.name].append(ch_ab)
        in_channels[a.name].append(ch_ba)
        neighbours[a.name].append((port_a, b.name))
        neighbours[b.name].append((port_b, a.name))

        if isinstance(a, endpoint_types):
            adapter_out[a.name] = ch_ab
            if isinstance(b, Switch):
                static_macs[b.name][a.mac] = port_b
        if isinstance(b, endpoint_types):
            adapter_out[b.name] = ch_ba
            if isinstance(a, Switch):
                static_macs[a.name][b.mac] = port_a

    for name, node in network._nodes.items():
        if isinstance(node, Switch) and node.mac_table_mode == "topology":
            for mac, port in _topology_mac_table(network, name, neighbours).items():
                static_macs[name].setdefault(mac, port)

    # Start one process per node
    ar_ids = itertools.count()
    for name, node in network._nodes.items():
        if isinstance(node, Adapter):
            env.process(
                _adapter_receiver(
                    env, inbound[name], node.mac, adapter_counters[name], pn_consumers[name]
                )
            )
            ch = adapter_out.get(name)
            for pn in node.profinet_specs:
                ar_id = next(ar_ids)
                device_ch = adapter_out.get(pn.device.name)
                if ch is None or device_ch is None:
                    continue  # one side not wired — no cyclic exchange possible
                cycle_us = pn.cycle_ms * 1_000
                delay_us = pn.delay_ms * 1_000
                pn_consumers[name].expect(ar_id, cycle_us, pn.hold)  # inputs
                pn_consumers[pn.device.name].expect(ar_id, cycle_us, pn.hold)  # outputs
                env.process(
                    _pn_provider(
                        env,
                        node.mac,
                        pn.device.mac,
                        ar_id,
                        pn.output_frame_size,
                        cycle_us,
                        ch,
                        PN_RT_PRIORITY,
                        delay_us,
                    )
                )
                env.process(
                    _pn_provider(
                        env,
                        pn.device.mac,
                        node.mac,
                        ar_id,
                        pn.input_frame_size,
                        cycle_us,
                        device_ch,
                        PN_RT_PRIORITY,
                        delay_us,
                    )
                )
            if ch:
                for spec in node.traffic_specs:
                    env.process(
                        _traffic_gen(
                            env,
                            node.mac,
                            spec.destination.mac,
                            spec.rate,
                            spec.size,
                            spec.pattern,
                            ch,
                            rng,
                            delay_us=spec.delay_ms * 1_000,
                            batch=spec.batch,
                            priority=spec.priority,
                        )
                    )
                for spec in node.mqtt_specs:
                    env.process(
                        _mqtt_traffic_gen(
                            env,
                            node.mac,
                            spec.broker.mac,
                            spec.topic,
                            spec.rate_hz,
                            spec.frame_size,
                            spec.qos,
                            ch,
                            delay_us=spec.delay_ms * 1_000,
                        )
                    )
                for spec in node.modbus_specs:
                    env.process(
                        _modbus_poll_gen(
                            env,
                            node.mac,
                            spec.slave.mac,
                            spec.slave.unit_id,
                            spec.count,
                            spec.request_frame_size,
                            spec.rate_hz,
                            ch,
                            delay_us=spec.delay_ms * 1_000,
                        )
                    )
        elif isinstance(node, MQTTBroker):
            ch = adapter_out.get(name)
            if ch:
                env.process(
                    _mqtt_broker_forwarder(
                        env,
                        inbound[name],
                        node.mac,
                        node._routes,
                        ch,
                    )
                )
        elif isinstance(node, ModbusSlave):
            ch = adapter_out.get(name)
            if ch:
                env.process(_modbus_slave_responder(env, inbound[name], node.mac, node.unit_id, ch))
        elif isinstance(node, ProfinetDevice):
            env.process(_pn_device_receiver(env, inbound[name], node.mac, pn_consumers[name]))
        elif isinstance(node, Switch):
            env.process(
                _switch_forwarder(env, inbound[name], out_channels[name], static_macs.get(name))
            )
        elif isinstance(node, Hub):
            env.process(
                _hub_broadcaster(env, inbound[name], out_channels[name], hub_collisions[name])
            )

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
            env.process(
                _throughput_sampler(env, adapter_counters[target.name], interval_us, samples)
            )
        elif obs.metric == "latency" and isinstance(target, Adapter):
            sinks = adapter_counters[target.name].latency_sinks
            env.process(_window_sampler(env, sinks, _mean, interval_us, samples))
        elif obs.metric == "queue_depth" and isinstance(target, Switch):
            env.process(_queue_depth_sampler(env, out_channels[target.name], interval_us, samples))
        elif obs.metric == "collision_rate" and isinstance(target, Hub):
            env.process(
                _collision_rate_sampler(env, hub_collisions[target.name], interval_us, samples)
            )
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
        elif obs.metric == "modbus_latency" and isinstance(target, Adapter):
            sinks = adapter_counters[target.name].modbus_rtt_sinks
            env.process(_window_sampler(env, sinks, _mean, interval_us, samples))
        elif obs.metric == "pn_latency" and target.name in pn_consumers:
            sinks = pn_consumers[target.name].latency_sinks
            env.process(_window_sampler(env, sinks, _mean, interval_us, samples))
        elif obs.metric == "pn_jitter" and target.name in pn_consumers:
            sinks = pn_consumers[target.name].jitter_sinks
            env.process(_window_sampler(env, sinks, max, interval_us, samples))
        elif obs.metric == "pn_watchdog_expiries" and target.name in pn_consumers:
            env.process(_pn_expiries_sampler(env, pn_consumers[target.name], interval_us, samples))

    if _n_chunks > 1:
        chunk_us = duration_us / _n_chunks
        sleep_s = 8.0 / _n_chunks  # target ~8 s total wall time
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
