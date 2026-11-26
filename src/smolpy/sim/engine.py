from __future__ import annotations

import copy
import heapq
import itertools
import random
import time
from collections import defaultdict
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING

import simpy

from smolpy import model
from smolpy.model import timebase as tb
from smolpy.model.frames import (
    IFG_BYTES,
    MODBUS_REQUEST_BYTES,
    MQTT_PUBACK_BYTES,
    PN_RT_PRIORITY,
    PREAMBLE_SFD_BYTES,
    WIRE_OVERHEAD_BYTES,
    flow_frame_size,
    modbus_response_frame_size,
    mqtt_publish_frame_size,
    pn_frame_size,
)
from smolpy.model.streams import source_ids, stream_seed

if TYPE_CHECKING:
    from smolpy.core.batch import BatchSpec
    from smolpy.core.network import SimulationResult

# Simulated time is an integer number of nanoseconds (Model Semantics §1):
# every timeout and timestamp below is an int, computed by smolpy.model.timebase.

Samples = list[tuple[float, float]]  # (time_ms, value)


class _Environment(simpy.Environment):
    """A SimPy environment whose clock is an integer number of nanoseconds:
    every timeout is an int, so ``now`` is one too."""

    _now: int

    @property
    def now(self) -> int:
        return self._now


# ---------------------------------------------------------------------------
# Frame — internal simulation packet
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Frame:
    src_mac: str
    dst_mac: str
    size_bytes: int
    created_at_ns: int
    retries: int = field(default=0)
    in_port: int = field(default=-1)  # port index at the receiving switch
    mqtt_topic: str | None = field(default=None)
    mqtt_qos: int = field(default=0)
    modbus_unit_id: int | None = field(default=None)
    modbus_register_count: int = field(default=0)
    modbus_is_response: bool = field(default=False)
    modbus_tid: int = field(default=-1)  # MBAP transaction id, on polls with outstanding=1
    priority: int = field(default=0)  # 802.1Q PCP 0-7
    pn_ar_id: int | None = field(default=None)  # PROFINET RT AR this frame belongs to
    pn_cycle_counter: int = field(default=0)
    # the ingress account of the switch that holds it, while it is queued there
    # (ADR-0013); shared by a flooded frame's copies
    stored: _Stored | None = field(default=None)


def _resolve_size(size: int | str, rng: random.Random) -> int:
    if isinstance(size, int):
        return size
    # IMIX (frames.IMIX_SIZES): 40% 64 B, 57% 594 B, 3% 1518 B
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
Sinks = list[list[int]]  # values in ns


def _emit(sinks: Sinks, value: int) -> None:
    for window in sinks:
        window.append(value)


class _PollCounts:
    """The Modbus counts of the polls of one slave, from every master
    (ADR-0013): the Modbus metrics observed on a slave."""

    __slots__ = ("requests", "responses", "timeouts", "skipped", "rtt_sinks")

    def __init__(self) -> None:
        self.requests = 0
        self.responses = 0
        self.timeouts = 0
        self.skipped = 0
        self.rtt_sinks: Sinks = []


class _AdapterCounters:
    def __init__(self, polled: dict[str, _PollCounts] | None = None) -> None:
        # the counts of every slave, by its MAC, shared by every master
        self.polled: dict[str, _PollCounts] = {} if polled is None else polled
        self.bytes_received: int = 0
        self.frames_received: int = 0
        self.modbus_requests: int = 0  # sent by this adapter's polls
        self.modbus_responses: int = 0  # accepted
        self.modbus_timeouts: int = 0  # requests timed out (ADR-0012)
        self.modbus_skipped: int = 0  # poll instants skipped (ADR-0012)
        # polls with one request in flight, by the slave's MAC
        self.transactions: dict[str, _Transaction] = {}
        self.latency_sinks: Sinks = []
        self.modbus_rtt_sinks: Sinks = []

    def record(self, frame: _Frame, now: int) -> None:
        self.bytes_received += frame.size_bytes
        self.frames_received += 1
        if self.latency_sinks:
            _emit(self.latency_sinks, now - frame.created_at_ns)
        if frame.modbus_is_response:
            t = self.transactions.get(frame.src_mac)
            if t is not None and not t.accept(frame, now):
                return  # a late response: the master has given up on it
            self.modbus_responses += 1
            if self.modbus_rtt_sinks:
                _emit(self.modbus_rtt_sinks, now - frame.created_at_ns)
            slave = self.polled.get(frame.src_mac)
            if slave is not None:
                slave.responses += 1
                if slave.rtt_sinks:
                    _emit(slave.rtt_sinks, now - frame.created_at_ns)


class _Transaction:
    """The transaction slot of a poll with one request in flight (ADR-0012):
    the request outstanding, if any, and when the previous one completed."""

    def __init__(
        self, counters: _AdapterCounters, slave: _PollCounts, timeout_ns: int | None
    ) -> None:
        self.counters = counters
        self.slave = slave
        self.timeout_ns = timeout_ns
        self.tid = -1  # the last request's transaction id
        self.sent_at = 0
        self.outstanding = False
        self.completed_at = -1

    def free(self, now: int) -> bool:
        """Whether a request may be sent now: none is outstanding, and the
        previous one did not complete at this very instant."""
        return not self.outstanding and self.completed_at != now

    def send(self, now: int) -> int:
        self.tid += 1
        self.sent_at = now
        self.outstanding = True
        return self.tid

    def accept(self, frame: _Frame, now: int) -> bool:
        """Whether a response ends the outstanding request: its own, before
        the timeout. Otherwise it is discarded."""
        if not self.outstanding or frame.modbus_tid != self.tid:
            return False
        if self.timeout_ns is not None and now - self.sent_at >= self.timeout_ns:
            return False
        self.outstanding = False
        self.completed_at = now
        return True

    def expire(self, tid: int, now: int) -> None:
        """The timeout of request *tid*: it times out if still outstanding."""
        if self.outstanding and self.tid == tid:
            self.outstanding = False
            self.completed_at = now
            self.counters.modbus_timeouts += 1
            self.slave.timeouts += 1


# ---------------------------------------------------------------------------
# Link channel — one directed lane of a link
# ---------------------------------------------------------------------------


class _Account:
    """A switch buffer account (ADR-0011, ADR-0013, ADR-0014): what it holds
    of the frames queued, in frame bytes, frames or cells, against its limit.

    With a *resume* level, a frame that does not fit puts the account in the
    drop state if it holds more than that level; it then refuses every frame
    until a release brings it to the level. It counts its drop episodes, the
    runs of refusals with no admission between them, and its time in the
    drop state."""

    __slots__ = (
        "limit",
        "unit",
        "cell",
        "resume",
        "env",
        "held",
        "dropping",
        "refused",
        "episodes",
        "since",
        "drop_ns",
        "switch",
        "drain",
        "drain_start",
        "marked",
    )

    def __init__(
        self,
        limit: int | None,
        unit: str,
        env: _Environment,
        cell: int | None = None,
        resume: int | None = None,
        switch: _Ingress | None = None,
        drain: int | None = None,
    ) -> None:
        self.limit = limit  # None: no cap of its own, only the switch's pool
        self.unit = unit
        self.cell = cell or 1
        self.resume = resume
        self.env = env
        self.held = 0
        self.dropping = False  # the drop state
        self.refused = False  # whether the last frame it decided was refused
        self.episodes = 0
        self.since = 0  # when it entered the drop state
        self.drop_ns = 0  # its time in the drop state before `since`
        # an ingress account: its switch, with the pool and what it has freed,
        # and the drain that ends its drop state (ADR-0015)
        self.switch = switch
        self.drain = drain
        self.drain_start = 0
        self.marked = False  # refused in the switch's drop state (ADR-0017)

    def units(self, size: int) -> int:
        """What a frame of *size* bytes takes of the account."""
        if self.unit == "frames":
            return 1
        if self.unit == "cells":
            return -(-size // self.cell)
        return size

    def fits(self, size: int) -> bool:
        """Whether a frame fits outside a drop state: its cap, then its
        reserve or the pool (ADR-0015), and with a dynamic threshold, beyond
        its reserve, at most R + α·(P − pool use) held after it (ADR-0017)."""
        u = self.units(size)
        if self.limit is not None and self.held + u > self.limit:
            return False
        sw = self.switch
        pool = None if sw is None else sw.pool
        if pool is None or not pool.fits(self.held, u):
            return pool is None
        alpha = sw.alpha  # type: ignore[union-attr]
        after = self.held + u
        return (
            alpha is None
            or after <= pool.reserve
            or after <= pool.reserve + alpha * (pool.limit - pool.use())
        )

    def admit(self, size: int) -> bool:
        """Decide a frame: False if it is dropped, by tail drop or the drop
        state; a refusal that follows an admission starts an episode."""
        state = None if self.switch is None else self.switch.state
        if state is not None:
            return self._admit_in_switch(size, state)
        if self.fits(size) and not self.dropping:
            self.refused = False
            return True
        if not self.refused:
            self.refused = True
            self.episodes += 1
        if not self.dropping:
            if self.drain is not None:
                self.enter()
            elif self.resume is not None and self.held > self.resume:
                self.dropping = True
                self.since = self.env._now
        return False

    def _admit_in_switch(self, size: int, state: _Account) -> bool:
        """Decide a frame with a drop state for the whole switch (ADR-0016):
        in the state, by admits_in_state; outside it, by fits, the cap, the
        reserve and the pool, a refusal entering it."""
        if state.dropping:
            return self.admits_in_state(size)
        if self.fits(size):
            return True
        state.episodes += 1
        state.enter()
        sw = self.switch
        sw.entry_held = sw.held  # type: ignore[union-attr]
        mark = sw.mark  # type: ignore[union-attr]
        if mark is not None:
            # the marks at the entry: each port above the level, and this one
            for account in sw.accounts.values():  # type: ignore[union-attr]
                account.marked = account.held > mark
            self.marked = True
        return False

    def admits_in_state(self, size: int) -> bool:
        """Whether a frame is admitted in the switch's drop state: only if it
        fits in its port's reserve (ADR-0016); with a mark, if its port is
        unmarked and it fits, a misfit marking the port (ADR-0017)."""
        sw = self.switch
        if sw.mark is None:  # type: ignore[union-attr]
            return self.held + self.units(size) <= sw.reserve  # type: ignore[union-attr]
        if self.marked:
            return False
        if self.fits(size):
            return True
        self.marked = True
        return False

    def enter(self) -> None:
        """Enter a drop state ended by a drain (ADR-0015)."""
        self.dropping = True
        self.since = self.env._now
        self.drain_start = self.switch.freed  # type: ignore[union-attr]
        self.switch.draining.append(self)  # type: ignore[union-attr]

    def charge(self, size: int) -> None:
        u = self.units(size)
        sw = self.switch
        if sw is not None:
            sw.held += u
            if sw.pool is not None:
                sw.pool.charge(self.held, u)
        self.held += u

    def release(self, size: int) -> None:
        u = self.units(size)
        sw = self.switch
        if sw is not None and sw.pool is not None:
            sw.pool.charge(self.held, -u)
        self.held -= u
        if sw is not None:
            sw.held -= u
            sw.free(u)
        if self.dropping and self.resume is not None and self.held <= self.resume:
            self.leave()

    def leave(self) -> None:
        self.dropping = False
        self.drop_ns += self.env._now - self.since

    def drop_time_ns(self) -> int:
        """Its time in the drop state so far."""
        return self.drop_ns + (self.env._now - self.since if self.dropping else 0)


class _Stored:
    """A frame admitted to an ingress account: it leaves the account when
    the last of its queued copies is taken (ADR-0013)."""

    __slots__ = ("account", "copies")

    def __init__(self, account: _Account) -> None:
        self.account = account
        self.copies = 0


class _BoundedQueue(simpy.PriorityStore):
    """An egress queue of a switch with buffer accounts (ADR-0011, ADR-0013).

    A frame leaves its accounts when the transmitter takes it, which is when
    SimPy pops it from the store: at once when the transmitter asks for the
    next frame, or, if it was idle, when the Put of a queued frame is
    processed. The frame in transmission is therefore not counted. The last
    copy of a frame taken releases its ingress account."""

    def __init__(self, env: _Environment, egress: _Account | None) -> None:
        super().__init__(env)
        self.egress = egress

    def _do_get(self, event: simpy.resources.store.StoreGet) -> None:
        if self.items:
            item = heapq.heappop(self.items)
            frame: _Frame = item.item
            if self.egress is not None:
                self.egress.release(frame.size_bytes)
            stored = frame.stored
            if stored is not None:
                frame.stored = None
                stored.copies -= 1
                if stored.copies == 0:
                    stored.account.release(frame.size_bytes)
            event.succeed(item)


class _LinkChannel:
    """Simulates one direction of a cable: enqueue → tx delay → prop delay → deliver.

    The egress queue serves 802.1Q priority classes strictly (higher first,
    FIFO within a class, non-preemptive). With strict_priority=False every
    frame is treated as priority 0, i.e. plain FIFO.

    On a switch with buffer accounts (*accounted*), the queue counts what it
    holds; with an *egress* account, a frame that does not fit is dropped on
    arrival, whatever its priority (tail drop, ADR-0011, ADR-0013).
    """

    def __init__(
        self,
        env: _Environment,
        speed_mbps: float,
        prop_delay_ns: int,
        in_port: int,
        dst_store: simpy.Store,
        strict_priority: bool = True,
        egress: _Account | None = None,
        accounted: bool = False,
    ) -> None:
        self.env = env
        self.speed_mbps = speed_mbps
        self.prop_delay_ns = prop_delay_ns
        self._tx_ns: dict[int, int] = {}  # preamble, SFD and frame, by frame size
        self._ifg_ns = tb.transmission_ns(IFG_BYTES, speed_mbps)
        self._free_at = 0  # end of the inter-frame gap after the last frame
        self.in_port = in_port
        self.dst_store = dst_store
        self.strict_priority = strict_priority
        self.egress = egress
        self.frames_dropped: int = 0  # by the egress account
        # Only a switch with accounts counts what its queues hold: every
        # other queue, every end station's among them, runs exactly as before.
        self.queue: simpy.PriorityStore = (
            _BoundedQueue(env, egress) if accounted else simpy.PriorityStore(env)
        )
        # PriorityItem compares on the key only, so the sequence number is what
        # keeps equal-priority frames in FIFO order.
        self._seq = itertools.count()
        self.bits_sent: int = 0  # frame bits whose transmission has completed
        self.frames_sent: int = 0  # those frames; each also took 20 B of wire overhead
        env.process(self._run())

    def enqueue(self, frame: _Frame) -> bool:
        """Queue *frame*; False if the egress account drops it."""
        egress = self.egress
        if egress is not None:
            if not egress.admit(frame.size_bytes):
                self.frames_dropped += 1  # tail drop: nothing is queued or scheduled
                return False
            egress.charge(frame.size_bytes)
        cls = -frame.priority if self.strict_priority else 0
        self.queue.put(simpy.PriorityItem((cls, next(self._seq)), frame))
        return True

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
            size = frame.size_bytes
            tx_ns = self._tx_ns.get(size)
            if tx_ns is None:
                tx_ns = self._tx_ns[size] = tb.transmission_ns(
                    PREAMBLE_SFD_BYTES + size, self.speed_mbps
                )
            # The inter-frame gap after the previous frame is waited out in the
            # same timeout: one event per frame, as without it. The clock is
            # read from _now, not through the `now` property: this loop runs
            # once per frame and hop.
            env = self.env
            gap = self._free_at - env._now
            yield env.timeout(tx_ns + gap if gap > 0 else tx_ns)
            self._free_at = env._now + self._ifg_ns
            self.bits_sent += size * 8
            self.frames_sent += 1
            in_flight = self.env.timeout(self.prop_delay_ns, frame)
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


def _ticks(env: _Environment, period: tb.Exact) -> Iterator[simpy.Timeout]:
    """Timeouts to the sample instants ``round(k * period)``, k = 1, 2, ...;
    after each one ``env.now`` is the instant."""
    for t in tb.instants(0, period):
        yield env.timeout(t - env.now)


def _bytes_rx_sampler(
    env: _Environment,
    counters: _AdapterCounters,
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    for tick in _ticks(env, period):
        yield tick
        samples.append((tb.ns_to_ms(env.now), counters.bytes_received / 1_000_000))


def _bytes_tx_sampler(
    env: _Environment,
    channel: _LinkChannel,
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    for tick in _ticks(env, period):
        yield tick
        samples.append((tb.ns_to_ms(env.now), channel.bits_sent // 8 / 1_000_000))


def _traffic_gen(
    env: _Environment,
    src_mac: str,
    dst_mac: str,
    rate_fps: float,
    size_spec: int | str,
    pattern: str,
    channel: _LinkChannel,
    rng: random.Random,
    delay_ns: int = 0,
    batch: BatchSpec | None = None,  # only for pattern == "batch"
    priority: int = 0,
) -> Generator:
    def send() -> None:
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=flow_frame_size(_resolve_size(size_spec, rng), priority),
                created_at_ns=env.now,
                priority=priority,
            )
        )

    if delay_ns > 0:
        yield env.timeout(delay_ns)
    period = tb.interval_ns(rate_fps)  # exact
    mean_ns = float(period)  # the mean of random gaps; each draw is rounded
    if pattern == "constant":
        for t in tb.instants(delay_ns, period):
            yield env.timeout(t - env.now)
            send()
    elif pattern == "poisson":
        while True:
            yield env.timeout(tb.draw_ns(rng.expovariate(1.0 / mean_ns)))
            send()
    elif pattern == "batch":
        assert batch is not None, "pattern='batch' needs a BatchSpec"
        # Compound Poisson: groups arrive at rate rate/E[X]; each group's X
        # frames are enqueued back to back at the same instant.
        group_ns = mean_ns * batch.mean()
        while True:
            yield env.timeout(tb.draw_ns(rng.expovariate(1.0 / group_ns)))
            for _ in range(batch.draw(rng)):
                send()
    else:  # bursty
        # ON: Pareto-distributed burst at 4x rate; OFF: proportional silence
        # with mean 0.75 T per frame, so a cycle averages T per frame.
        quarter = Fraction(period) / 4
        while True:
            burst_len = max(1, int(rng.paretovariate(1.5)))
            in_burst = tb.instants(env.now, quarter)
            for _ in range(burst_len):
                yield env.timeout(next(in_burst) - env.now)
                send()
            yield env.timeout(tb.draw_ns(mean_ns * burst_len * rng.uniform(0.3, 1.2)))


def _mqtt_traffic_gen(
    env: _Environment,
    src_mac: str,
    dst_mac: str,
    topic: str,
    rate_hz: float,
    frame_size: int,
    qos: int,
    channel: _LinkChannel,
    delay_ns: int = 0,
) -> Generator:
    """Periodic MQTT PUBLISH traffic (constant-rate, sensor-style)."""
    if delay_ns > 0:
        yield env.timeout(delay_ns)
    for t in tb.instants(delay_ns, tb.interval_ns(rate_hz)):
        yield env.timeout(t - env.now)
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=frame_size,
                created_at_ns=env.now,
                mqtt_topic=topic,
                mqtt_qos=qos,
            )
        )


def _modbus_poll_gen(
    env: _Environment,
    src_mac: str,
    dst_mac: str,
    unit_id: int,
    register_count: int,
    frame_size: int,
    rate_hz: float,
    channel: _LinkChannel,
    counters: _AdapterCounters,
    slave: _PollCounts,
    delay_ns: int = 0,
    transaction: _Transaction | None = None,  # with outstanding=1
) -> Generator:
    """Periodic Modbus TCP poll request (Read Holding Registers, FC 03).

    With a *transaction*, a poll instant is skipped while a request is
    outstanding (ADR-0012), and each request starts its timeout, if any."""
    if delay_ns > 0:
        yield env.timeout(delay_ns)
    for t in tb.instants(delay_ns, tb.interval_ns(rate_hz)):
        yield env.timeout(t - env.now)
        tid = -1
        if transaction is not None:
            if not transaction.free(env.now):
                counters.modbus_skipped += 1
                slave.skipped += 1
                continue
            tid = transaction.send(env.now)
        counters.modbus_requests += 1
        slave.requests += 1
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=frame_size,
                created_at_ns=env.now,
                modbus_unit_id=unit_id,
                modbus_register_count=register_count,
                modbus_tid=tid,
            )
        )
        if transaction is not None and transaction.timeout_ns is not None:
            _expire_after(env, transaction, tid)


def _expire_after(env: _Environment, transaction: _Transaction, tid: int) -> None:
    """One timeout event, at the request's timeout."""
    assert transaction.timeout_ns is not None
    event = env.timeout(transaction.timeout_ns)
    event.callbacks.append(lambda _: transaction.expire(tid, env.now))


def _modbus_slave_responder(
    env: _Environment,
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
        response_size = modbus_response_frame_size(frame.modbus_register_count)
        out_channel.enqueue(
            _Frame(
                src_mac=slave_mac,
                dst_mac=frame.src_mac,
                size_bytes=response_size,
                created_at_ns=frame.created_at_ns,  # preserve original timestamp for RTT
                modbus_unit_id=unit_id,
                modbus_register_count=frame.modbus_register_count,
                modbus_is_response=True,
                modbus_tid=frame.modbus_tid,  # MBAP echoes the transaction id
            )
        )


def _mqtt_broker_forwarder(
    env: _Environment,
    inbound: simpy.Store,
    broker_mac: str,
    topic_subs: dict[str, list[str]],  # topic → [subscriber_mac, ...]
    out_channel: _LinkChannel,  # broker's outbound channel to nearest switch
) -> Generator:
    """MQTT broker: receives PUBLISH, fans out to subscribers, ACKs QoS 1."""
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
                    created_at_ns=frame.created_at_ns,  # preserve original timestamp for latency
                    mqtt_topic=frame.mqtt_topic,
                )
            )
        # QoS 1 → PUBACK (4-byte MQTT header + Ethernet/IP/TCP)
        if frame.mqtt_qos >= 1:
            out_channel.enqueue(
                _Frame(
                    src_mac=broker_mac,
                    dst_mac=frame.src_mac,
                    size_bytes=MQTT_PUBACK_BYTES,
                    created_at_ns=env.now,
                )
            )


def _broker_queue_sampler(
    env: _Environment,
    inbound: simpy.Store,
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    for tick in _ticks(env, period):
        yield tick
        samples.append((tb.ns_to_ms(env.now), float(len(inbound.items))))


class _PnAR:
    """Consumer-side state of one PROFINET RT data direction."""

    def __init__(self, cycle_ns: int, hold: int) -> None:
        self.cycle_ns = cycle_ns
        self.hold = hold
        self.last_arrival: int | None = None
        self.arrival: simpy.Event | None = None


class _PnConsumer:
    """Consumer of PROFINET RT cyclic data for one node, across all its ARs."""

    def __init__(self, env: _Environment) -> None:
        self.env = env
        self.ars: dict[int, _PnAR] = {}
        self.latency_sinks: Sinks = []
        self.jitter_sinks: Sinks = []
        self.watchdog_expiries: int = 0

    def expect(self, ar_id: int, cycle_ns: int, hold: int) -> None:
        self.ars[ar_id] = _PnAR(cycle_ns, hold)

    def record(self, frame: _Frame, now: int) -> None:
        ar = self.ars.get(frame.pn_ar_id) if frame.pn_ar_id is not None else None
        if ar is None:
            return
        if self.latency_sinks:
            _emit(self.latency_sinks, now - frame.created_at_ns)
        if ar.last_arrival is None:
            # Supervision starts with the first frame, as after AR establishment.
            self.env.process(self._watchdog(ar))
        elif self.jitter_sinks:
            _emit(self.jitter_sinks, abs(now - ar.last_arrival - ar.cycle_ns))
        ar.last_arrival = now
        if ar.arrival is not None and not ar.arrival.triggered:
            ar.arrival.succeed()

    def _watchdog(self, ar: _PnAR) -> Generator:
        timeout_ns = ar.hold * ar.cycle_ns
        while True:
            ar.arrival = self.env.event()
            fired = yield ar.arrival | self.env.timeout(timeout_ns)
            if ar.arrival in fired:
                continue
            # Real devices abort the AR here; we count it and keep exchanging
            # (ADR-0005), re-arming once data flows again.
            self.watchdog_expiries += 1
            yield ar.arrival


def _pn_provider(
    env: _Environment,
    src_mac: str,
    dst_mac: str,
    ar_id: int,
    frame_size: int,
    cycle_ns: int,
    channel: _LinkChannel,
    priority: int,
    delay_ns: int = 0,
) -> Generator:
    """PROFINET RT provider: one cyclic I/O frame every update time."""
    if delay_ns > 0:
        yield env.timeout(delay_ns)
    counter = 0
    while True:
        channel.enqueue(
            _Frame(
                src_mac=src_mac,
                dst_mac=dst_mac,
                size_bytes=frame_size,
                created_at_ns=env.now,
                priority=priority,
                pn_ar_id=ar_id,
                pn_cycle_counter=counter,
            )
        )
        counter = (counter + 1) & 0xFFFF
        yield env.timeout(cycle_ns)  # an integer cycle: exact, no drift


def _pn_device_receiver(
    env: _Environment,
    inbound: simpy.Store,
    mac: str,
    consumer: _PnConsumer,
) -> Generator:
    while True:
        frame: _Frame = yield inbound.get()
        if frame.dst_mac != mac:
            continue  # flooded frame for another station: the NIC drops it
        consumer.record(frame, env.now)


def _mean_us(values: list[int]) -> float:
    """Mean of integer ns values, in µs: one division of exact integers."""
    return sum(values) / (len(values) * tb.NS_PER_US)


def _max_us(values: list[int]) -> float:
    return max(values) / tb.NS_PER_US


def _window_sampler(
    env: _Environment,
    sinks: Sinks,
    aggregate: Callable[[list[int]], float],
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    """Aggregate the values emitted into *sinks* during each sampling window."""
    window: list[int] = []
    sinks.append(window)  # registered now, before any frame can arrive
    return _window_loop(env, window, aggregate, period, samples)


def _window_loop(
    env: _Environment,
    window: list[int],
    aggregate: Callable[[list[int]], float],
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    for tick in _ticks(env, period):
        yield tick
        if window:
            samples.append((tb.ns_to_ms(env.now), aggregate(window)))
            window.clear()


def _pn_expiries_sampler(
    env: _Environment,
    consumer: _PnConsumer,
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    for tick in _ticks(env, period):
        yield tick
        samples.append((tb.ns_to_ms(env.now), float(consumer.watchdog_expiries)))


def _replicate(frame: _Frame, out_channels: dict[int, _LinkChannel]) -> None:
    """Send a copy of *frame* to every port except the one it arrived on."""
    for port, ch in out_channels.items():
        if port != frame.in_port:
            ch.enqueue(copy.copy(frame))


def _topology_mac_table(
    nodes: dict[str, model.NodeSpec], switch: str, neighbours: dict[str, list[tuple[int, str]]]
) -> dict[str, int]:
    """Port towards every reachable end station, by breadth-first search.

    Switches are traversed, end stations are not. The first port to
    reach a station wins: shortest path, ties to the lowest port index.
    """
    table: dict[str, int] = {}
    seen = {switch}
    frontier = sorted(neighbours[switch])
    for _, peer in frontier:
        seen.add(peer)
    while frontier:
        nxt: list[tuple[int, str]] = []
        for port, name in frontier:
            node = nodes[name]
            if isinstance(node, model.SwitchNode):
                for _, peer in neighbours[name]:
                    if peer not in seen:
                        seen.add(peer)
                        nxt.append((port, peer))
            else:
                table.setdefault(node.mac, port)
        frontier = nxt
    return table


class _Pool:
    """A switch's shared buffer pool (ADR-0015): what its ingress ports hold
    beyond their reserves, Σ max(0, held − reserve), against its limit. With
    *total*, its use is the ports' whole occupancy less one reserve,
    max(0, Σ held − reserve) (ADR-0017)."""

    __slots__ = ("limit", "reserve", "used", "total", "held")

    def __init__(self, limit: int, reserve: int, total: bool = False) -> None:
        self.limit = limit
        self.reserve = reserve
        self.used = 0  # Σ max(0, held − reserve)
        self.total = total
        self.held = 0  # Σ held

    def _beyond(self, held: int) -> int:
        return held - self.reserve if held > self.reserve else 0

    def use(self) -> int:
        """What the pool holds, by its count."""
        return self._beyond(self.held) if self.total else self.used

    def fits(self, held: int, u: int) -> bool:
        """Whether a port holding *held* can take *u* more: in its reserve, or
        in the pool for what it adds to the pool's use."""
        if self.total:
            return held + u <= self.reserve or self._beyond(self.held + u) <= self.limit
        return self.used + self._beyond(held + u) - self._beyond(held) <= self.limit

    def charge(self, held: int, u: int) -> None:
        """A port holding *held* takes *u* more, or gives *-u* back."""
        self.used += self._beyond(held + u) - self._beyond(held)
        self.held += u


class _Ingress:
    """A switch's ingress accounts, by port, the frames they dropped, and
    with a pool, the pool, what the switch has freed so far and the ports
    whose drop state a drain ends (ADR-0013, ADR-0015). With a drop state
    for the whole switch, *state* holds it: its drop state, entries and time,
    with no limit of its own (ADR-0016). With *net*, its drain is the fall of
    the switch's ingress occupancy, *held*, since the entry (ADR-0017)."""

    def __init__(self, accounts: dict[int, _Account], pool: _Pool | None = None) -> None:
        self.accounts = accounts
        self.dropped = 0
        self.pool = pool
        self.reserve = 0 if pool is None else pool.reserve
        self.freed = 0  # units freed by releases, in the ingress scope's unit
        self.draining: list[_Account] = []  # in their drop state, with a drain
        self.state: _Account | None = None
        self.held = 0  # Σ of the ports' occupancies
        self.net = False  # the state's drain counted net (ADR-0017)
        self.entry_held = 0  # the occupancy at the state's entry
        # the protection of a light port (ADR-0017 Part 2): the dynamic
        # threshold, and the level above which a port is marked at the entry
        self.alpha: float | None = None
        self.mark: int | None = None

    def free(self, u: int) -> None:
        """A release of *u*: every drained drop state ends at the release at
        which its drain has been freed since it began, or with *net*, at
        which the occupancy is its drain below its value at the entry."""
        self.freed += u
        if self.draining:
            for account in list(self.draining):
                if self.net:
                    drained = self.entry_held - self.held
                else:
                    drained = self.freed - account.drain_start
                if drained >= account.drain:  # type: ignore[operator]
                    self.draining.remove(account)
                    account.leave()


def _switch_forwarder(
    env: _Environment,
    inbound: simpy.Store,
    out_channels: dict[int, _LinkChannel],
    initial_mac: dict[str, int] | None = None,
    ingress: _Ingress | None = None,
) -> Generator:
    """MAC-learning store-and-forward switch.

    initial_mac pre-seeds the table for adapters whose MACs are known from
    topology wiring, preventing spurious flooding toward silent endpoints.

    With *ingress* accounts, a frame is dropped if its ingress port's
    account has no room; otherwise each copy is admitted by its egress port,
    and the frame is charged once if a copy was queued (ADR-0013).
    """
    mac_table: dict[str, int] = dict(initial_mac) if initial_mac else {}
    while True:
        frame: _Frame = yield inbound.get()
        mac_table[frame.src_mac] = frame.in_port
        dst_port = mac_table.get(frame.dst_mac)
        if dst_port == frame.in_port:
            continue  # hairpin, drop silently
        if ingress is None:
            if dst_port is None:
                # Unknown destination: flood to all ports except the one it arrived on
                _replicate(frame, out_channels)
            else:
                out_channels[dst_port].enqueue(frame)
            continue
        account = ingress.accounts[frame.in_port]
        if not account.admit(frame.size_bytes):
            ingress.dropped += 1  # tail drop: no egress port is looked at
            continue
        stored = frame.stored = _Stored(account)
        if dst_port is None:
            for port, ch in out_channels.items():
                if port != frame.in_port and ch.enqueue(copy.copy(frame)):
                    stored.copies += 1
        elif out_channels[dst_port].enqueue(frame):
            stored.copies = 1
        # a store never pops a frame within the put that queues it, so the
        # account is charged before any copy can be taken
        if stored.copies:
            account.charge(frame.size_bytes)


def _adapter_receiver(
    env: _Environment,
    inbound: simpy.Store,
    mac: str,
    counters: _AdapterCounters,
    pn: _PnConsumer,
) -> Generator:
    while True:
        frame: _Frame = yield inbound.get()
        # A NIC accepts only frames addressed to its own MAC. Frames that reach
        # it because a switch flooded them (unknown destination) are dropped
        # and do not count towards any metric.
        if frame.dst_mac != mac:
            continue
        counters.record(frame, env.now)
        if frame.pn_ar_id is not None:
            pn.record(frame, env.now)


def _throughput_sampler(
    env: _Environment,
    counters: _AdapterCounters,
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    last_bytes = 0
    last_t = 0
    for tick in _ticks(env, period):
        yield tick
        delta = counters.bytes_received - last_bytes
        last_bytes = counters.bytes_received
        window_ns = env.now - last_t  # instants are rounded: can differ by 1 ns
        last_t = env.now
        mbps = delta * 8 * tb.NS_PER_US / window_ns  # bits per µs is Mb/s
        samples.append((tb.ns_to_ms(env.now), mbps))


def _count_sampler(
    env: _Environment,
    count: Callable[[], int],
    period: tb.Exact,
    samples: Samples,
    window: bool = False,
) -> Generator[simpy.Event]:
    """A count at each sample: so far, or with *window* in the window."""
    last = 0
    for tick in _ticks(env, period):
        yield tick
        total = count()
        samples.append((tb.ns_to_ms(env.now), float(total - last if window else total)))
        if window:
            last = total


def _episodes(
    ports: list[_LinkChannel], ingress: _Ingress | None, metric: str
) -> Callable[[], int]:
    """A switch's drop episodes, or its accounts' time in the drop state in
    ns, by its ingress or its egress accounts (ADR-0014)."""
    if metric.startswith("ingress_"):
        if ingress is None:
            accounts = []
        elif ingress.state is not None:
            accounts = [ingress.state]  # the switch's entries and time (ADR-0016)
        else:
            accounts = list(ingress.accounts.values())
    else:
        accounts = [ch.egress for ch in ports if ch.egress is not None]
    if metric.endswith("_episodes_total"):
        return lambda: sum(a.episodes for a in accounts)
    return lambda: sum(a.drop_time_ns() for a in accounts)


def _time_sampler(
    env: _Environment, ns: Callable[[], int], period: tb.Exact, samples: Samples
) -> Generator[simpy.Event]:
    """A time so far, in ms, rounded once."""
    for tick in _ticks(env, period):
        yield tick
        samples.append((tb.ns_to_ms(env.now), ns() / tb.NS_PER_MS))


def _dropped(ports: list[_LinkChannel], ingress: _Ingress | None, metric: str) -> Callable[[], int]:
    """Frames a switch dropped: by its egress accounts, its ingress accounts,
    or both (ADR-0013)."""
    if metric == "ingress_drops_total":
        return lambda: 0 if ingress is None else ingress.dropped
    if metric == "egress_drops_total" or ingress is None:
        return lambda: sum(ch.frames_dropped for ch in ports)
    return lambda: ingress.dropped + sum(ch.frames_dropped for ch in ports)


def _sent(channel: _LinkChannel) -> Callable[[], int]:
    return lambda: channel.frames_sent


def _received(counters: _AdapterCounters, metric: str) -> Callable[[], int]:
    if metric == "frames_received":
        return lambda: counters.frames_received
    if metric == "modbus_timeouts":
        return lambda: counters.modbus_timeouts
    if metric == "modbus_skipped":
        return lambda: counters.modbus_skipped
    return lambda: counters.modbus_requests - counters.modbus_responses  # modbus_unanswered


def _polled(slave: _PollCounts, metric: str) -> Callable[[], int]:
    """A Modbus count of the polls of one slave (ADR-0013)."""
    if metric == "modbus_timeouts":
        return lambda: slave.timeouts
    if metric == "modbus_skipped":
        return lambda: slave.skipped
    return lambda: slave.requests - slave.responses  # modbus_unanswered


def _queue_depth_sampler(
    env: _Environment,
    out_channels: dict[int, _LinkChannel],
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    for tick in _ticks(env, period):
        yield tick
        depth = sum(len(ch.queue.items) for ch in out_channels.values())
        samples.append((tb.ns_to_ms(env.now), float(depth)))


def _utilization_sampler(
    env: _Environment,
    channels: list[_LinkChannel],
    period: tb.Exact,
    samples: Samples,
) -> Generator:
    speed = sum(tb.exact(ch.speed_mbps) for ch in channels)  # Mb/s, exact
    last_bits = 0
    last_t = 0
    for tick in _ticks(env, period):
        yield tick
        # wire occupancy (ADR-0010): frames plus 20 B of overhead each
        bits = sum(ch.bits_sent + ch.frames_sent * WIRE_OVERHEAD_BYTES * 8 for ch in channels)
        window_ns = env.now - last_t
        # bits / (speed · 10⁶ bit/s · window_ns · 10⁻⁹ s) · 100 %
        util = tb.quotient((bits - last_bits) * 100_000, speed * window_ns)
        last_bits, last_t = bits, env.now
        samples.append((tb.ns_to_ms(env.now), util))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


_ENDPOINTS = (
    model.AdapterNode,
    model.MqttBrokerNode,
    model.ModbusSlaveNode,
    model.ProfinetDeviceNode,
)


def _lookup(nodes: dict[str, model.NodeSpec], name: str, what: str) -> model.NodeSpec:
    try:
        return nodes[name]
    except KeyError:
        raise ValueError(f"{what}: reference to undeclared node {name!r}") from None


def run_simulation(
    scenario: model.Scenario,
    *,
    _all_samples: dict[str, Samples] | None = None,
    _sim_state: dict | None = None,
    _n_chunks: int = 1,
    _checked: bool = False,
) -> SimulationResult:
    """Run *scenario* on SimPy. Duration and seed come from ``scenario.run``.

    The scenario is checked first (``smolpy.backends.check``), unless the
    caller has done it already (*_checked*).
    """
    from smolpy.core.network import RunStats, SimulationResult

    if not _checked:
        from smolpy.backends import check

        check(scenario, "simpy")
    assert scenario.run is not None
    t_start = time.perf_counter()

    nodes: dict[str, model.NodeSpec] = {n.name: n for n in scenario.nodes}
    env = _Environment()
    duration_ns = tb.ms_to_ns(scenario.run.duration_ms)
    # One generator per traffic statement, seeded from its stable source id.
    stream = {id(t): sid for t, sid in zip(scenario.traffic, source_ids(scenario), strict=True)}

    run_seed = scenario.run.seed

    def rng(t: model.TrafficSpec) -> random.Random:
        return random.Random(stream_seed(run_seed, stream[id(t)]))

    # Traffic statements, grouped by the node that runs them, in scenario order.
    sourced: dict[str, list[model.TrafficSpec]] = defaultdict(list)
    for t in scenario.traffic:
        sourced[model.traffic_owner(t)].append(t)

    # Every node gets an inbound store where delivered frames arrive
    inbound: dict[str, simpy.Store] = {name: simpy.Store(env) for name in nodes}

    # the polls of every slave, by its MAC, counted at their masters (ADR-0013)
    polled: dict[str, _PollCounts] = {
        node.mac: _PollCounts()
        for node in nodes.values()
        if isinstance(node, model.ModbusSlaveNode)
    }
    adapter_counters: dict[str, _AdapterCounters] = {
        name: _AdapterCounters(polled)
        for name, node in nodes.items()
        if isinstance(node, model.AdapterNode)
    }

    pn_consumers: dict[str, _PnConsumer] = {
        name: _PnConsumer(env)
        for name, node in nodes.items()
        if isinstance(node, (model.AdapterNode, model.ProfinetDeviceNode))
    }

    def _strict(node: model.NodeSpec) -> bool:
        return not (isinstance(node, model.SwitchNode) and node.qos == "fifo")

    def _accounted(node: model.NodeSpec) -> bool:
        # only switches have buffer accounts; end stations never drop
        return isinstance(node, model.SwitchNode) and node.bounded

    def _account(node: model.SwitchNode, ingress: bool) -> _Account | None:
        if not ingress:
            limit = node.egress_limit
            if limit is None:
                return None
            return _Account(limit[0], limit[1], env, node.cell_bytes, node.buffer_resume)
        cap = node.ingress_limit
        return _Account(
            None if cap is None else cap[0],
            node.ingress_unit,  # type: ignore[arg-type]
            env,
            node.cell_bytes,
            node.ingress_buffer_resume,
            ingress_of[node.name],
            # with a switch-wide drop state the ports have none of their own
            None if node.ingress_drop == "switch" else node.ingress_drain,
        )

    def _egress(node: model.NodeSpec) -> _Account | None:
        return _account(node, False) if isinstance(node, model.SwitchNode) else None

    # ingress accounts, by switch and port (ADR-0013)
    ingress: dict[str, _Ingress] = {}
    for name, node in nodes.items():
        if isinstance(node, model.SwitchNode) and node.ingress_accounted:
            pool = node.pool_limit
            shared = (
                None
                if pool is None
                else _Pool(pool[0], node.ingress_reserve or 0, node.ingress_pool_count == "total")
            )
            ingress[name] = _Ingress({}, shared)
            if node.ingress_drop == "switch":
                ingress[name].state = _Account(
                    None,
                    node.ingress_unit,  # type: ignore[arg-type]
                    env,
                    switch=ingress[name],
                    drain=node.ingress_drain,
                )
                ingress[name].net = node.ingress_drain_count == "net"
                ingress[name].alpha = node.ingress_alpha
                ingress[name].mark = node.ingress_mark
    ingress_of = ingress

    # Build directed link channels from topology
    next_port: dict[str, int] = defaultdict(int)
    out_channels: dict[str, dict[int, _LinkChannel]] = defaultdict(dict)
    in_channels: dict[str, list[_LinkChannel]] = defaultdict(list)
    adapter_out: dict[str, _LinkChannel] = {}  # single outbound channel per leaf adapter
    static_macs: dict[str, dict[str, int]] = defaultdict(dict)  # switch_name → {mac: port}
    neighbours: dict[str, list[tuple[int, str]]] = defaultdict(list)  # name → [(port, peer)]

    for link in scenario.links:
        a = _lookup(nodes, link.a, f"link {link.a} -- {link.b}")
        b = _lookup(nodes, link.b, f"link {link.a} -- {link.b}")
        port_a = next_port[a.name]
        next_port[a.name] += 1
        port_b = next_port[b.name]
        next_port[b.name] += 1

        prop_ns = tb.propagation_ns(link.length_m)
        # ch_ab is a's egress queue, ch_ba is b's — each follows its owner's QoS.
        ch_ab = _LinkChannel(
            env,
            link.speed_mbps,
            prop_ns,
            port_b,
            inbound[b.name],
            _strict(a),
            _egress(a),
            _accounted(a),
        )
        ch_ba = _LinkChannel(
            env,
            link.speed_mbps,
            prop_ns,
            port_a,
            inbound[a.name],
            _strict(b),
            _egress(b),
            _accounted(b),
        )
        for node, port in ((a, port_a), (b, port_b)):
            if node.name in ingress:
                assert isinstance(node, model.SwitchNode)
                ingress[node.name].accounts[port] = _account(node, True)  # type: ignore[assignment]

        out_channels[a.name][port_a] = ch_ab
        out_channels[b.name][port_b] = ch_ba
        in_channels[b.name].append(ch_ab)
        in_channels[a.name].append(ch_ba)
        neighbours[a.name].append((port_a, b.name))
        neighbours[b.name].append((port_b, a.name))

        if isinstance(a, _ENDPOINTS):
            adapter_out[a.name] = ch_ab
            if isinstance(b, model.SwitchNode):
                static_macs[b.name][a.mac] = port_b
        if isinstance(b, _ENDPOINTS):
            adapter_out[b.name] = ch_ba
            if isinstance(a, model.SwitchNode):
                static_macs[a.name][b.mac] = port_a

    for name, node in nodes.items():
        if isinstance(node, model.SwitchNode) and node.mac_table == "topology":
            for mac, port in _topology_mac_table(nodes, name, neighbours).items():
                static_macs[name].setdefault(mac, port)

    def start_flows(name: str, node: model.NodeSpec) -> None:
        """The flows of an end station, in statement order (ADR-0013)."""
        ch = adapter_out.get(name)
        if ch is None:
            return
        for spec in sourced.get(name, []):
            if isinstance(spec, model.Flow):
                dst = _lookup(nodes, spec.destination, f"flow {name} -> {spec.destination}")
                env.process(
                    _traffic_gen(
                        env,
                        node.mac,  # type: ignore[union-attr]
                        dst.mac,  # type: ignore[union-attr]
                        spec.rate,
                        spec.size,
                        spec.pattern,
                        ch,
                        rng(spec),
                        delay_ns=tb.ms_to_ns(spec.delay_ms),
                        batch=spec.batch,
                        priority=spec.priority,
                    )
                )

    # Start one process per node
    ar_ids = itertools.count()
    for name, node in nodes.items():
        if isinstance(node, model.AdapterNode):
            env.process(
                _adapter_receiver(
                    env, inbound[name], node.mac, adapter_counters[name], pn_consumers[name]
                )
            )
            ch = adapter_out.get(name)
            specs = sourced.get(name, [])
            for pn in specs:
                if not isinstance(pn, model.ProfinetAR):
                    continue
                ar_id = next(ar_ids)
                device = _lookup(nodes, pn.device, f"PROFINET AR {name} -> {pn.device}")
                device_ch = adapter_out.get(device.name)
                if ch is None or device_ch is None:
                    continue  # one side not wired — no cyclic exchange possible
                assert isinstance(device, model.ProfinetDeviceNode)
                cycle_ns = tb.ms_to_ns(pn.cycle_ms)
                delay_ns = tb.ms_to_ns(pn.delay_ms)
                pn_consumers[name].expect(ar_id, cycle_ns, pn.hold)  # inputs
                pn_consumers[device.name].expect(ar_id, cycle_ns, pn.hold)  # outputs
                env.process(
                    _pn_provider(
                        env,
                        node.mac,
                        device.mac,
                        ar_id,
                        pn_frame_size(pn.outputs),
                        cycle_ns,
                        ch,
                        PN_RT_PRIORITY,
                        delay_ns,
                    )
                )
                env.process(
                    _pn_provider(
                        env,
                        device.mac,
                        node.mac,
                        ar_id,
                        pn_frame_size(pn.inputs),
                        cycle_ns,
                        device_ch,
                        PN_RT_PRIORITY,
                        delay_ns,
                    )
                )
            if ch:
                start_flows(name, node)
                for spec in specs:
                    if isinstance(spec, model.Publish):
                        broker = _lookup(nodes, spec.broker, f"publish {name} -> {spec.broker}")
                        env.process(
                            _mqtt_traffic_gen(
                                env,
                                node.mac,
                                broker.mac,  # type: ignore[union-attr]
                                spec.topic,
                                spec.rate,
                                mqtt_publish_frame_size(spec.topic, spec.payload, spec.qos),
                                spec.qos,
                                ch,
                                delay_ns=tb.ms_to_ns(spec.delay_ms),
                            )
                        )
                for spec in specs:
                    if isinstance(spec, model.Poll):
                        slave = _lookup(nodes, spec.slave, f"poll {name} -> {spec.slave}")
                        assert isinstance(slave, model.ModbusSlaveNode)
                        counters = adapter_counters[name]
                        transaction = None
                        if spec.outstanding is not None:
                            timeout_ns = (
                                None if spec.timeout_ms is None else tb.ms_to_ns(spec.timeout_ms)
                            )
                            transaction = _Transaction(counters, polled[slave.mac], timeout_ns)
                            counters.transactions[slave.mac] = transaction
                        env.process(
                            _modbus_poll_gen(
                                env,
                                node.mac,
                                slave.mac,
                                slave.unit_id,
                                spec.count,
                                MODBUS_REQUEST_BYTES,
                                spec.rate,
                                ch,
                                counters,
                                polled[slave.mac],
                                delay_ns=tb.ms_to_ns(spec.delay_ms),
                                transaction=transaction,
                            )
                        )
        elif isinstance(node, model.MqttBrokerNode):
            ch = adapter_out.get(name)
            if ch:
                topic_subs: dict[str, list[str]] = {}
                for route in sourced.get(name, []):
                    if isinstance(route, model.Route):
                        topic_subs[route.topic] = [
                            _lookup(nodes, s, f"route {name} {route.topic}").mac  # type: ignore[union-attr]
                            for s in route.subscribers
                        ]
                env.process(_mqtt_broker_forwarder(env, inbound[name], node.mac, topic_subs, ch))
            start_flows(name, node)
        elif isinstance(node, model.ModbusSlaveNode):
            ch = adapter_out.get(name)
            if ch:
                env.process(_modbus_slave_responder(env, inbound[name], node.mac, node.unit_id, ch))
            start_flows(name, node)
        elif isinstance(node, model.ProfinetDeviceNode):
            env.process(_pn_device_receiver(env, inbound[name], node.mac, pn_consumers[name]))
            start_flows(name, node)
        elif isinstance(node, model.SwitchNode):
            env.process(
                _switch_forwarder(
                    env,
                    inbound[name],
                    out_channels[name],
                    static_macs.get(name),
                    ingress.get(name),
                )
            )

    # Start metric samplers
    # Reuse an externally supplied dict (live mode) or create a fresh one.
    all_samples: dict[str, Samples] = _all_samples if _all_samples is not None else {}

    for obs in scenario.observations:
        target = _lookup(nodes, obs.target, f"observe {obs.metric} on {obs.target}")
        period = tb.period_ns(obs.every_ms)  # exact; each sample instant is rounded
        key = f"{obs.metric}:{target.name}"
        if key not in all_samples:
            all_samples[key] = []
        samples = all_samples[key]
        adapter = isinstance(target, model.AdapterNode)
        station = isinstance(target, _ENDPOINTS)
        # the polls of a slave, observed on it (ADR-0013)
        polls_of = polled.get(target.mac) if isinstance(target, model.ModbusSlaveNode) else None

        if obs.metric == "throughput" and adapter:
            env.process(_throughput_sampler(env, adapter_counters[target.name], period, samples))
        elif obs.metric == "latency" and adapter:
            sinks = adapter_counters[target.name].latency_sinks
            env.process(_window_sampler(env, sinks, _mean_us, period, samples))
        elif obs.metric == "queue_depth" and isinstance(target, model.SwitchNode):
            env.process(_queue_depth_sampler(env, out_channels[target.name], period, samples))
        elif obs.metric == "utilization":
            chs = in_channels.get(target.name, [])
            if chs:
                env.process(_utilization_sampler(env, chs, period, samples))
        elif obs.metric == "bytes_received" and adapter:
            env.process(_bytes_rx_sampler(env, adapter_counters[target.name], period, samples))
        elif obs.metric == "bytes_sent" and station:
            ch = adapter_out.get(target.name)
            if ch:
                env.process(_bytes_tx_sampler(env, ch, period, samples))
        elif obs.metric == "broker_queue" and isinstance(target, model.MqttBrokerNode):
            env.process(_broker_queue_sampler(env, inbound[target.name], period, samples))
        elif obs.metric == "modbus_latency" and (adapter or polls_of is not None):
            sinks = (
                adapter_counters[target.name].modbus_rtt_sinks if adapter else polls_of.rtt_sinks  # type: ignore[union-attr]
            )
            env.process(_window_sampler(env, sinks, _mean_us, period, samples))
        elif obs.metric == "pn_latency" and target.name in pn_consumers:
            sinks = pn_consumers[target.name].latency_sinks
            env.process(_window_sampler(env, sinks, _mean_us, period, samples))
        elif obs.metric == "pn_jitter" and target.name in pn_consumers:
            sinks = pn_consumers[target.name].jitter_sinks
            env.process(_window_sampler(env, sinks, _max_us, period, samples))
        elif obs.metric == "pn_watchdog_expiries" and target.name in pn_consumers:
            env.process(_pn_expiries_sampler(env, pn_consumers[target.name], period, samples))
        elif obs.metric in (
            "drops",
            "drops_total",
            "ingress_drops_total",
            "egress_drops_total",
        ) and isinstance(target, model.SwitchNode):
            ports = list(out_channels[target.name].values())
            dropped = _dropped(ports, ingress.get(target.name), obs.metric)
            window = obs.metric == "drops"
            env.process(_count_sampler(env, dropped, period, samples, window))
        elif obs.metric == "pool_occupancy" and isinstance(target, model.SwitchNode):
            accounts = ingress.get(target.name)
            shared_pool = None if accounts is None else accounts.pool
            occupancy = (
                (lambda p: lambda: p.use())(shared_pool) if shared_pool is not None else (lambda: 0)
            )
            env.process(_count_sampler(env, occupancy, period, samples))
        elif obs.metric in (
            "ingress_drop_episodes_total",
            "egress_drop_episodes_total",
            "ingress_drop_time_total",
            "egress_drop_time_total",
        ) and isinstance(target, model.SwitchNode):
            ports = list(out_channels[target.name].values())
            count = _episodes(ports, ingress.get(target.name), obs.metric)
            if obs.metric.endswith("_episodes_total"):
                env.process(_count_sampler(env, count, period, samples))
            else:
                env.process(_time_sampler(env, count, period, samples))
        elif obs.metric == "frames_sent" and station:
            ch = adapter_out.get(target.name)
            if ch:
                env.process(_count_sampler(env, _sent(ch), period, samples))
        elif (
            obs.metric
            in ("frames_received", "modbus_unanswered", "modbus_timeouts", "modbus_skipped")
            and adapter
        ):
            count = _received(adapter_counters[target.name], obs.metric)
            env.process(_count_sampler(env, count, period, samples))
        elif (
            obs.metric in ("modbus_unanswered", "modbus_timeouts", "modbus_skipped")
            and polls_of is not None
        ):
            env.process(_count_sampler(env, _polled(polls_of, obs.metric), period, samples))

    if _n_chunks > 1:
        sleep_s = 8.0 / _n_chunks  # target ~8 s total wall time
        for i in range(_n_chunks):
            if _sim_state is not None and _sim_state.get("stop", False):
                break
            while _sim_state is not None and _sim_state.get("paused", False):
                time.sleep(0.05)
            env.run(until=(i + 1) * duration_ns // _n_chunks)
            if _sim_state is not None:
                _sim_state["progress"] = (i + 1) / _n_chunks
            time.sleep(sleep_s)
    else:
        env.run(until=duration_ns)

    # SimPy draws one id from _eid for every scheduled event: processed events
    # are the ids drawn minus the events still queued.
    events = next(env._eid) - len(env._queue)
    stats = RunStats(backend="simpy", events=events, wall_s=time.perf_counter() - t_start)

    if _sim_state is not None:
        _sim_state["done"] = True

    return SimulationResult(metrics=all_samples, scenario=scenario, stats=stats)
