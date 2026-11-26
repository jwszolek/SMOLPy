//! The event loop.
//!
//! Deterministic scenarios must give SimPy's results bit for bit (ADR-0007,
//! ADR-0008). That depends on processing simultaneous events in the same
//! order as SimPy, so this engine reproduces SimPy's event graph exactly as
//! `smolpy.sim.engine` creates it:
//!
//! - events are ordered by `(time, priority, sequence)`; process starts are
//!   URGENT, everything else NORMAL, and the sequence number is drawn when an
//!   event is scheduled;
//! - a store `put` schedules a Put event at once; a waiting `get` is
//!   triggered only when that event is processed, and resumes its process
//!   when the Get event is processed; a `get` on a non-empty store is
//!   scheduled at once;
//! - a switch's buffer accounts (ADR-0011, ADR-0013) count what its egress
//!   queues hold: a frame leaves its egress account, and its last copy its
//!   ingress account, when the store pops it, as SimPy's `_do_get` does, and
//!   a frame that does not fit is dropped before anything is scheduled;
//! - processes are created in SimPy's order: link transmitters, then node
//!   processes in node order, then metric samplers in observation order;
//! - the run stops before the first event at `t = duration`.
//!
//! A poll with one request in flight (ADR-0012) starts its request's timeout
//! right after the request's Put, as one timeout event: [`Ev::PollTimeout`].
//!
//! The PROFINET watchdog waits on `arrival | timeout`, a SimPy `AnyOf`
//! condition, which is reproduced event by event (see [`PnAr`]).

use crate::frames::{
    flow_frame_size, modbus_response_frame_size, mqtt_publish_frame_size, IFG_BYTES,
    MODBUS_REQUEST_BYTES, MQTT_PUBACK_BYTES, PREAMBLE_SFD_BYTES, WIRE_OVERHEAD_BYTES,
};
use crate::frames::{pn_frame_size, PN_RT_PRIORITY};
use crate::rng::{stream_seed, Rng};
use crate::scenario::{Limit, Metric, NodeKind, Pattern, Scenario, Size};
use crate::timebase::{self as tb, Instants, Q};
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, VecDeque};

const URGENT: u8 = 0;
const NORMAL: u8 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Ev {
    Init(u32),
    Timeout(u32),
    /// propagation of a frame on a channel has ended
    InFlight(u32, u32),
    Put(u32),
    /// store, process, frame
    Get(u32, u32, u32),
    /// a PROFINET watchdog's arrival event, of AR state and round
    Arrival(u32, u32),
    /// its timeout, of AR state and round
    Watchdog(u32, u32),
    /// its `arrival | timeout` condition, of AR state and round
    Condition(u32, u32),
    /// a Modbus request's timeout, of transaction slot and transaction id
    PollTimeout(u32, u64),
}

#[derive(Clone, Copy)]
struct Frame {
    src: u32,
    dst: u32,
    size: u64,
    created: u64,
    priority: u8,
    in_port: i32,
    modbus: Modbus,
    mqtt: Mqtt,
    /// the consumer-side PROFINET AR state this cyclic frame feeds; -1 if none
    pn_ar: i32,
    /// the ingress account entry that holds it while it is queued on a
    /// switch (ADR-0013), shared by a flooded frame's copies; -1 if none
    stored: i32,
}

/// The Modbus TCP fields of a frame; `unit < 0` on any other frame.
#[derive(Clone, Copy)]
struct Modbus {
    unit: i16,
    count: u16,
    response: bool,
    /// the MBAP transaction id, on polls with one request in flight
    tid: u64,
}

const NOT_MODBUS: Modbus = Modbus {
    unit: -1,
    count: 0,
    response: false,
    tid: 0,
};

/// The MQTT fields of a PUBLISH frame; `topic < 0` on any other frame,
/// a PUBACK included. Topics are numbered in order of first mention.
#[derive(Clone, Copy)]
struct Mqtt {
    topic: i32,
    qos: u8,
}

const NOT_MQTT: Mqtt = Mqtt { topic: -1, qos: 0 };

impl Frame {
    /// A frame an end station sends now, outside 802.1Q priorities.
    fn new(src: u32, dst: u32, size: u64, created: u64) -> Frame {
        Frame {
            src,
            dst,
            size,
            created,
            priority: 0,
            in_port: -1,
            modbus: NOT_MODBUS,
            mqtt: NOT_MQTT,
            pn_ar: -1,
            stored: -1,
        }
    }
}

/// What a switch buffer account counts a frame as (ADR-0013, ADR-0014).
#[derive(Clone, Copy, PartialEq)]
enum Unit {
    Bytes,
    Frames,
    /// cells of this many bytes, a frame taking ceil(bytes / cell)
    Cells(u64),
}

/// A switch buffer account (`_Account`): what it holds of the frames queued,
/// in frame bytes, frames or cells, against its limit. With a resume level,
/// a frame that does not fit puts it in the drop state if it holds more than
/// that level, and it then refuses every frame until a release brings it to
/// the level (ADR-0014). It counts its drop episodes, the runs of refusals
/// with no admission between them, and its time in the drop state.
#[derive(Clone, Copy)]
struct Account {
    limit: u64,
    unit: Unit,
    resume: Option<u64>,
    held: u64,
    dropping: bool,
    refused: bool,
    episodes: u64,
    since: u64,
    drop_ns: u64,
    /// an ingress account's drain, which ends its drop state (ADR-0015), and
    /// what its switch had freed when it entered it
    drain: Option<u64>,
    drain_start: u64,
    /// refused in the switch's drop state, marked at its entry or at a
    /// misfit in it (ADR-0017)
    marked: bool,
}

impl Account {
    fn new(limit: Option<Limit>, cell: Option<u64>, resume: Option<u64>) -> Option<Account> {
        let (limit, unit) = match limit? {
            Limit::Bytes(b) => (b, Unit::Bytes),
            Limit::Frames(n) => (n, Unit::Frames),
            Limit::Cells(n) => (
                n,
                Unit::Cells(cell.expect("a limit in cells has a cell size")),
            ),
        };
        Some(Account {
            limit,
            unit,
            resume,
            held: 0,
            dropping: false,
            refused: false,
            episodes: 0,
            since: 0,
            drop_ns: 0,
            drain: None,
            drain_start: 0,
            marked: false,
        })
    }

    fn units(&self, size: u64) -> u64 {
        match self.unit {
            Unit::Bytes => size,
            Unit::Frames => 1,
            Unit::Cells(cell) => size.div_ceil(cell),
        }
    }

    fn fits(&self, size: u64) -> bool {
        self.held + self.units(size) <= self.limit
    }

    /// Decide a frame: false if it is dropped, by tail drop or the drop
    /// state; a refusal that follows an admission starts an episode.
    fn admit(&mut self, size: u64, now: u64) -> bool {
        let fits = self.fits(size);
        self.decide(fits, now, 0)
    }

    /// Decide a frame that fits or not; *freed* is what the switch has
    /// freed so far, where a drain starts (ADR-0015).
    fn decide(&mut self, fits: bool, now: u64, freed: u64) -> bool {
        if fits && !self.dropping {
            self.refused = false;
            return true;
        }
        if !self.refused {
            self.refused = true;
            self.episodes += 1;
        }
        if !self.dropping {
            if self.drain.is_some() {
                self.dropping = true;
                self.since = now;
                self.drain_start = freed;
            } else if self.resume.is_some_and(|r| self.held > r) {
                self.dropping = true;
                self.since = now;
            }
        }
        false
    }

    fn leave(&mut self, now: u64) {
        self.dropping = false;
        self.drop_ns += now - self.since;
    }

    fn charge(&mut self, size: u64) {
        self.held += self.units(size);
    }

    fn release(&mut self, size: u64, now: u64) {
        self.held -= self.units(size);
        if self.dropping && self.resume.is_some_and(|r| self.held <= r) {
            self.leave(now);
        }
    }

    /// Its time in the drop state up to `now`.
    fn drop_time_ns(&self, now: u64) -> u64 {
        self.drop_ns + if self.dropping { now - self.since } else { 0 }
    }
}

/// A switch's shared buffer pool (`_Pool`, ADR-0015): what its ingress ports
/// hold beyond their reserves, the sum of max(0, held - reserve), against its
/// limit. With *total*, its use is the ports' whole occupancy less one
/// reserve (ADR-0017).
#[derive(Clone, Copy)]
struct Pool {
    limit: u64,
    reserve: u64,
    used: u64,
    total: bool,
}

impl Pool {
    fn beyond(&self, held: u64) -> u64 {
        held.saturating_sub(self.reserve)
    }

    /// Whether a port holding *held* can take *u* more.
    fn fits(&self, held: u64, u: u64) -> bool {
        self.used + self.beyond(held + u) - self.beyond(held) <= self.limit
    }
}

/// A frame admitted to an ingress account (`_Stored`): it leaves the
/// account when the last of its queued copies is taken.
struct Stored {
    switch: u32,
    port: u32,
    copies: u32,
}

enum Items {
    Fifo(VecDeque<u32>),
    /// SimPy's PriorityStore of PriorityItem((class, sequence), frame)
    Prio(BinaryHeap<Reverse<(i16, u64, u32)>>),
}

struct Store {
    items: Items,
    waiting: Option<u32>,
    /// the channel of an egress queue of a switch with buffer accounts:
    /// popping a frame releases its accounts
    accounted: Option<u32>,
}

impl Store {
    fn pop(&mut self) -> Option<u32> {
        match &mut self.items {
            Items::Fifo(q) => q.pop_front(),
            Items::Prio(h) => h.pop().map(|Reverse((_, _, f))| f),
        }
    }

    fn len(&self) -> usize {
        match &self.items {
            Items::Fifo(q) => q.len(),
            Items::Prio(h) => h.len(),
        }
    }
}

struct Channel {
    speed: Q,
    prop_ns: u64,
    in_port: i32,
    dst_store: u32,
    queue: u32,
    strict: bool,
    seq: u64,
    tx_ns: HashMap<u64, u64>,
    ifg_ns: u64,
    free_at: u64,
    bits_sent: u128,
    frames_sent: u128,
    sending: Option<u32>,
    /// the egress account of a bounded switch port (ADR-0011, ADR-0013)
    egress: Option<Account>,
    dropped: u64, // by the egress account
}

struct Adapter {
    mac: u32,
    inbound: u32,
    out: Option<u32>,
    bytes_received: u128,
    frames_received: u64,
    modbus_requests: u64,  // sent by its polls
    modbus_responses: u64, // accepted
    modbus_timeouts: u64,  // requests timed out (ADR-0012)
    modbus_skipped: u64,   // poll instants skipped (ADR-0012)
    /// polls with one request in flight: (slave MAC, transaction slot)
    transactions: Vec<(u32, u32)>,
    latency_windows: Vec<u32>,
    modbus_windows: Vec<u32>, // round-trip times of Modbus responses
    pn: u32,                  // its PROFINET consumer
}

/// The transaction slot of a poll with one request in flight (ADR-0012,
/// `_Transaction`): the request outstanding, if any, and when the previous
/// one completed.
struct Transaction {
    adapter: u32,
    slave: u32,
    timeout_ns: Option<u64>,
    sent: u64, // requests sent: the last one's id is sent - 1
    sent_at: u64,
    outstanding: bool,
    completed_at: Option<u64>,
}

impl Transaction {
    /// Whether a request may be sent now: none is outstanding, and the
    /// previous one did not complete at this very instant.
    fn free(&self, now: u64) -> bool {
        !self.outstanding && self.completed_at != Some(now)
    }

    /// Send a request now: its transaction id.
    fn send(&mut self, now: u64) -> u64 {
        self.sent += 1;
        self.sent_at = now;
        self.outstanding = true;
        self.sent - 1
    }

    /// Whether a response ends the outstanding request: its own, before the
    /// timeout. Otherwise it is discarded.
    fn accept(&mut self, tid: u64, now: u64) -> bool {
        if !self.outstanding || tid + 1 != self.sent {
            return false;
        }
        if self.timeout_ns.is_some_and(|t| now - self.sent_at >= t) {
            return false;
        }
        self.outstanding = false;
        self.completed_at = Some(now);
        true
    }
}

/// A PROFINET IO device: consumes its controller's outputs.
struct Device {
    mac: u32,
    inbound: u32,
    out: Option<u32>,
    pn: u32,
}

/// The PROFINET RT consumer of a node, across its ARs (`_PnConsumer`).
struct PnConsumer {
    latency_windows: Vec<u32>,
    jitter_windows: Vec<u32>,
    expiries: u64,
}

/// State of a SimPy event the watchdog waits on.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Signal {
    Pending,
    Triggered,
    Processed,
}

/// One direction of an AR at its consumer (`_PnAR`), with its watchdog.
///
/// Each round of the watchdog makes a new arrival event and a timeout of
/// `hold` cycles, and waits on the condition `arrival | timeout`, which is
/// triggered by whichever of the two is processed first and is then processed
/// itself, as a separate event. If the arrival has been processed by then,
/// the next round starts; otherwise the watchdog counts an expiry and waits
/// for the arrival alone. A frame triggers the arrival only while it is
/// pending, and the watchdog starts with the first frame.
struct PnAr {
    consumer: u32,
    cycle_ns: u64,
    timeout_ns: u64,
    last_arrival: Option<u64>,
    arrival: Option<Signal>, // None until the watchdog has started
    round: u32,
    condition_triggered: bool,
    expired: bool,
}

/// One side's cyclic frames: one frame per cycle after the delay
/// (`_pn_provider`).
struct PnProvider {
    frame: Frame,
    chan: u32,
    delay_ns: u64,
    cycle_ns: u64,
}

/// A Modbus TCP slave: answers Read Holding Registers for its unit id.
/// It also counts its polls, from every master (ADR-0013).
struct Slave {
    mac: u32,
    unit: u8,
    inbound: u32,
    out: Option<u32>,
    requests: u64,
    responses: u64,
    timeouts: u64,
    skipped: u64,
    modbus_windows: Vec<u32>,
}

/// An MQTT broker: forwards a PUBLISH to the topic's subscribers and
/// acknowledges QoS 1.
struct Broker {
    mac: u32,
    inbound: u32,
    out: Option<u32>,
    routes: Vec<Vec<u32>>, // topic -> subscriber MACs
}

/// Modbus polls and MQTT publications: the same frame at constant instants,
/// after an optional delay, as `_modbus_poll_gen` and `_mqtt_traffic_gen`.
struct Periodic {
    frame: Frame, // its `created` is set when it is sent
    chan: u32,
    delay_ns: u64,
    delayed: bool,
    instants: Instants,
    /// the adapter and the slave whose requests a Modbus poll counts
    requester: Option<(u32, u32)>,
    /// the transaction slot of a poll with one request in flight
    transaction: Option<u32>,
}

struct Switch {
    inbound: u32,
    out: Vec<(i32, u32)>, // (port, channel), in port order: out[p].0 == p
    mac_table: HashMap<u32, i32>,
    /// ingress accounts by port, if the switch has an ingress limit or a
    /// pool (ADR-0013, ADR-0015)
    ingress: Vec<Account>,
    ingress_dropped: u64,
    pool: Option<Pool>,
    /// units freed by releases, in the ingress unit, and the ports whose
    /// drop state a drain ends, in the order they entered it (ADR-0015)
    freed: u64,
    draining: Vec<usize>,
    /// the switch's own drop state, ended by its drain, with its entries
    /// and time, if it has one for every port (`_Ingress.state`, ADR-0016)
    state: Option<Account>,
    /// the ports' occupancies summed, and with the state's drain counted
    /// net, that sum at the state's entry (`_Ingress.held`, ADR-0017)
    held: u64,
    net: bool,
    entry_held: u64,
    /// the protection of a light port (`_Ingress.alpha`, `_Ingress.mark`,
    /// ADR-0017 Part 2)
    alpha: Option<f64>,
    mark: Option<u64>,
}

impl Switch {
    /// Decide a frame of ingress port *port* (`_Account.admit`).
    fn admit_ingress(&mut self, port: usize, size: u64, now: u64) -> bool {
        let fits = self.fits_ingress(port, size);
        if let Some(dropping) = self.state.as_ref().map(|s| s.dropping) {
            // `_Account._admit_in_switch`: in the state, by
            // `admits_in_state`; outside it, a refusal enters it
            if dropping {
                return self.admits_in_state(port, size);
            }
            if fits {
                return true;
            }
            let state = self.state.as_mut().expect("a switch-wide state");
            state.episodes += 1;
            state.dropping = true;
            state.since = now;
            state.drain_start = self.freed;
            self.entry_held = self.held;
            if let Some(mark) = self.mark {
                // the marks at the entry: each port above the level, and this one
                for acc in &mut self.ingress {
                    acc.marked = acc.held > mark;
                }
                self.ingress[port].marked = true;
            }
            return false;
        }
        let was = self.ingress[port].dropping;
        let freed = self.freed;
        let acc = &mut self.ingress[port];
        let ok = acc.decide(fits, now, freed);
        if !was && acc.dropping && acc.drain.is_some() {
            self.draining.push(port);
        }
        ok
    }

    /// What the pool holds, by its count (`_Pool.use`).
    fn pool_use(&self) -> u64 {
        self.pool
            .map_or(0, |p| if p.total { p.beyond(self.held) } else { p.used })
    }

    /// Whether a frame of *port* fits outside a drop state
    /// (`_Account.fits`): its cap, then its reserve or the pool (ADR-0015),
    /// and with a dynamic threshold, beyond its reserve, at most
    /// R + α·(P − pool use) held after it (ADR-0017).
    fn fits_ingress(&self, port: usize, size: u64) -> bool {
        let acc = &self.ingress[port];
        let u = acc.units(size);
        if !acc.fits(size) {
            return false;
        }
        let Some(p) = &self.pool else {
            return true;
        };
        let after = acc.held + u;
        let room = if p.total {
            after <= p.reserve || p.beyond(self.held + u) <= p.limit
        } else {
            p.fits(acc.held, u)
        };
        room && (after <= p.reserve
            || self.alpha.is_none_or(|alpha| {
                let free = p.limit as i64 - self.pool_use() as i64;
                after as f64 <= p.reserve as f64 + alpha * free as f64
            }))
    }

    /// Whether a frame of *port* is admitted in the switch's drop state
    /// (`_Account.admits_in_state`): only within its reserve (ADR-0016); with
    /// a mark, if its port is unmarked and it fits, a misfit marking the
    /// port (ADR-0017).
    fn admits_in_state(&mut self, port: usize, size: u64) -> bool {
        if self.mark.is_none() {
            let acc = &self.ingress[port];
            return acc.held + acc.units(size) <= self.pool.map_or(0, |p| p.reserve);
        }
        if self.ingress[port].marked {
            return false;
        }
        if self.fits_ingress(port, size) {
            return true;
        }
        self.ingress[port].marked = true;
        false
    }

    fn charge_ingress(&mut self, port: usize, size: u64) {
        let acc = &mut self.ingress[port];
        let u = acc.units(size);
        if let Some(p) = &mut self.pool {
            p.used = p.used + p.beyond(acc.held + u) - p.beyond(acc.held);
        }
        acc.held += u;
        self.held += u;
    }

    /// A frame of *port* freed (`_Account.release`): the pool and the
    /// switch's freed count, every drain that is complete, then the port's
    /// own resume level.
    fn release_ingress(&mut self, port: usize, size: u64, now: u64) {
        let u = self.ingress[port].units(size);
        let held = self.ingress[port].held;
        if let Some(p) = &mut self.pool {
            p.used = p.used + p.beyond(held - u) - p.beyond(held);
        }
        self.ingress[port].held -= u;
        self.held -= u;
        self.freed += u;
        if !self.draining.is_empty() {
            let freed = self.freed;
            let ingress = &mut self.ingress;
            self.draining.retain(|&q| {
                let a = &mut ingress[q];
                let done = freed - a.drain_start >= a.drain.expect("a drained account");
                if done {
                    a.leave(now);
                }
                !done
            });
        }
        if let Some(state) = &mut self.state {
            if state.dropping {
                let drain = state.drain.expect("a drained state");
                // freed since the entry, or net, the fall of the occupancy
                // since the entry (ADR-0017)
                let drained = if self.net {
                    self.held + drain <= self.entry_held
                } else {
                    self.freed - state.drain_start >= drain
                };
                if drained {
                    state.leave(now);
                }
            }
        }
        let acc = &mut self.ingress[port];
        if acc.dropping && acc.resume.is_some_and(|r| acc.held <= r) {
            acc.leave(now);
        }
    }
}

/// Which drops a switch's drop metric counts.
#[derive(Clone, Copy, PartialEq)]
enum Drops {
    All,
    Ingress,
    Egress,
}

/// Where a flow's generator is, as in `smolpy.sim.engine._traffic_gen`.
enum FlowState {
    /// waiting for `delay_ms` to pass
    Delay,
    /// constant, Poisson and batch patterns
    Run,
    /// bursty: `left` of `len` frames to go, spaced by `ticks`
    Burst {
        left: u64,
        len: u64,
        ticks: Instants,
    },
    /// bursty: the silence after a burst
    Silence,
}

struct FlowGen {
    src: u32,
    dst: u32,
    size: Size,
    priority: u8,
    chan: u32,
    delay_ns: u64,
    pattern: Pattern,
    state: FlowState,
    instants: Instants,
    mean_ns: f64, // the mean interval, for random gaps
    quarter: Q,   // T / 4, the spacing within a burst
    rng: Rng,
}

enum Sampled {
    Throughput(u32),
    Latency(u32),
    BytesSent(u32),
    BytesReceived(u32),
    QueueDepth(u32),
    Utilization(Vec<u32>, Q),
    BrokerQueue(u32),
    /// the window maximum, in µs
    Jitter(u32),
    WatchdogExpiries(u32),
    /// frames a switch dropped, by these channels' egress accounts or its
    /// ingress accounts, in the window or so far
    Drops(Vec<u32>, u32, Drops, bool),
    FramesSent(u32),
    FramesReceived(u32),
    ModbusUnanswered(u32),
    ModbusTimeouts(u32),
    ModbusSkipped(u32),
    /// the same, over the polls of a slave (ADR-0013)
    SlaveUnanswered(u32),
    SlaveTimeouts(u32),
    SlaveSkipped(u32),
    /// a switch's drop episodes, or its accounts' time in the drop state in
    /// ms, by these channels' egress accounts or its ingress accounts
    /// (ADR-0014): (channels, switch, ingress, time)
    Episodes(Vec<u32>, u32, bool, bool),
    /// a switch's pool use (ADR-0015)
    PoolOccupancy(u32),
}

struct Sampler {
    what: Sampled,
    out: usize,
    ticks: Instants,
    last_t: u64,
    last: u128,
}

enum Proc {
    Tx(u32),
    AdapterRx(u32),
    Switch(u32),
    Flow(u32),
    Sampler(u32),
    Slave(u32),
    Broker(u32),
    Periodic(u32),
    PnProvider(u32),
    PnDeviceRx(u32),
    Watchdog(u32),
}

pub struct Outcome {
    /// (metric key, samples of (time_ms, value)), in observation order
    pub metrics: Vec<(String, Vec<(f64, f64)>)>,
    pub events: u64,
    pub wall_s: f64,
}

struct Engine {
    now: u64,
    seq: u64,
    heap: BinaryHeap<Reverse<(u64, u8, u64, Ev)>>,
    events: u64,
    frames: Vec<Frame>,
    free: Vec<u32>,
    stores: Vec<Store>,
    channels: Vec<Channel>,
    adapters: Vec<Adapter>,
    switches: Vec<Switch>,
    stored: Vec<Stored>,
    stored_free: Vec<u32>,
    flows: Vec<FlowGen>,
    slaves: Vec<Slave>,
    /// a slave by its MAC
    slave_of: HashMap<u32, u32>,
    brokers: Vec<Broker>,
    periodic: Vec<Periodic>,
    transactions: Vec<Transaction>,
    devices: Vec<Device>,
    pn_consumers: Vec<PnConsumer>,
    pn_ars: Vec<PnAr>,
    pn_providers: Vec<PnProvider>,
    samplers: Vec<Sampler>,
    windows: Vec<Vec<u64>>,
    procs: Vec<Proc>,
    out: Vec<(String, Vec<(f64, f64)>)>,
}

impl Engine {
    fn schedule(&mut self, ev: Ev, priority: u8, delay: u64) {
        self.seq += 1;
        self.heap
            .push(Reverse((self.now + delay, priority, self.seq, ev)));
    }

    fn store(&mut self, items: Items) -> u32 {
        self.stores.push(Store {
            items,
            waiting: None,
            accounted: None,
        });
        (self.stores.len() - 1) as u32
    }

    fn process(&mut self, proc: Proc) {
        self.procs.push(proc);
        let id = (self.procs.len() - 1) as u32;
        self.schedule(Ev::Init(id), URGENT, 0);
    }

    fn alloc(&mut self, frame: Frame) -> u32 {
        match self.free.pop() {
            Some(i) => {
                self.frames[i as usize] = frame;
                i
            }
            None => {
                self.frames.push(frame);
                (self.frames.len() - 1) as u32
            }
        }
    }

    fn release(&mut self, f: u32) {
        self.free.push(f);
    }

    /// Pop the store's next frame. On a switch with buffer accounts, it
    /// leaves its egress account, and its last copy its ingress account.
    fn pop(&mut self, store: u32) -> Option<u32> {
        let s = &mut self.stores[store as usize];
        let f = s.pop()?;
        if let Some(c) = s.accounted {
            let now = self.now;
            let fr = &mut self.frames[f as usize];
            let size = fr.size;
            if let Some(egress) = &mut self.channels[c as usize].egress {
                egress.release(size, now);
            }
            if fr.stored >= 0 {
                let st = fr.stored as u32;
                fr.stored = -1;
                let entry = &mut self.stored[st as usize];
                entry.copies -= 1;
                if entry.copies == 0 {
                    let (w, port) = (entry.switch, entry.port);
                    self.switches[w as usize].release_ingress(port as usize, size, now);
                    self.stored_free.push(st);
                }
            }
        }
        Some(f)
    }

    fn new_stored(&mut self, switch: u32, port: u32) -> u32 {
        let entry = Stored {
            switch,
            port,
            copies: 0,
        };
        match self.stored_free.pop() {
            Some(i) => {
                self.stored[i as usize] = entry;
                i
            }
            None => {
                self.stored.push(entry);
                (self.stored.len() - 1) as u32
            }
        }
    }

    fn get(&mut self, store: u32, proc: u32) {
        match self.pop(store) {
            Some(f) => self.schedule(Ev::Get(store, proc, f), NORMAL, 0),
            None => self.stores[store as usize].waiting = Some(proc),
        }
    }

    fn put_fifo(&mut self, store: u32, f: u32) {
        match &mut self.stores[store as usize].items {
            Items::Fifo(q) => q.push_back(f),
            Items::Prio(_) => unreachable!("inbound stores are FIFO"),
        }
        self.schedule(Ev::Put(store), NORMAL, 0);
    }

    /// Queue a frame on a channel; false if its egress account drops it.
    fn enqueue(&mut self, chan: u32, f: u32) -> bool {
        let now = self.now;
        let ch = &mut self.channels[chan as usize];
        if let Some(egress) = &mut ch.egress {
            let size = self.frames[f as usize].size;
            if !egress.admit(size, now) {
                // tail drop: nothing is queued or scheduled
                ch.dropped += 1;
                self.release(f);
                return false;
            }
            egress.charge(size);
        }
        let class = if ch.strict {
            -(self.frames[f as usize].priority as i16)
        } else {
            0
        };
        let key = (class, ch.seq, f);
        ch.seq += 1;
        let queue = ch.queue;
        match &mut self.stores[queue as usize].items {
            Items::Prio(h) => h.push(Reverse(key)),
            Items::Fifo(_) => unreachable!("egress queues are priority stores"),
        }
        self.schedule(Ev::Put(queue), NORMAL, 0);
        true
    }

    fn on_put(&mut self, store: u32) {
        if let Some(proc) = self.stores[store as usize].waiting {
            if let Some(f) = self.pop(store) {
                self.stores[store as usize].waiting = None;
                self.schedule(Ev::Get(store, proc, f), NORMAL, 0);
            }
        }
    }

    fn start(&mut self, proc: u32) {
        match self.procs[proc as usize] {
            Proc::Tx(c) => {
                let q = self.channels[c as usize].queue;
                self.get(q, proc);
            }
            Proc::AdapterRx(a) => {
                let s = self.adapters[a as usize].inbound;
                self.get(s, proc);
            }
            Proc::Switch(w) => {
                let s = self.switches[w as usize].inbound;
                self.get(s, proc);
            }
            Proc::Flow(i) => {
                let fl = &mut self.flows[i as usize];
                if fl.delay_ns > 0 {
                    fl.state = FlowState::Delay;
                    let d = fl.delay_ns;
                    self.schedule(Ev::Timeout(proc), NORMAL, d);
                } else {
                    self.flow_begin(i, proc);
                }
            }
            Proc::Sampler(i) => {
                let t = self.samplers[i as usize].ticks.next().unwrap();
                self.schedule(Ev::Timeout(proc), NORMAL, t - self.now);
            }
            Proc::Slave(i) => {
                let s = self.slaves[i as usize].inbound;
                self.get(s, proc);
            }
            Proc::Broker(i) => {
                let s = self.brokers[i as usize].inbound;
                self.get(s, proc);
            }
            Proc::PnProvider(i) => {
                let delay = self.pn_providers[i as usize].delay_ns;
                if delay > 0 {
                    self.schedule(Ev::Timeout(proc), NORMAL, delay);
                } else {
                    self.pn_send(i, proc);
                }
            }
            Proc::PnDeviceRx(d) => {
                let s = self.devices[d as usize].inbound;
                self.get(s, proc);
            }
            Proc::Watchdog(r) => self.watchdog_round(r),
            Proc::Periodic(i) => {
                let pg = &mut self.periodic[i as usize];
                let delay = if pg.delay_ns > 0 {
                    pg.delayed = true;
                    pg.delay_ns
                } else {
                    pg.instants.next().unwrap() - self.now
                };
                self.schedule(Ev::Timeout(proc), NORMAL, delay);
            }
        }
    }

    fn on_get(&mut self, store: u32, proc: u32, f: u32) {
        match self.procs[proc as usize] {
            Proc::Tx(c) => {
                let size = self.frames[f as usize].size;
                let ch = &mut self.channels[c as usize];
                let tx = match ch.tx_ns.get(&size) {
                    Some(t) => *t,
                    None => {
                        let t = tb::transmission_ns(PREAMBLE_SFD_BYTES + size, ch.speed)
                            .expect("transmission time");
                        ch.tx_ns.insert(size, t);
                        t
                    }
                };
                // the inter-frame gap after the previous frame is waited out
                // in the same timeout, as in the reference engine
                let gap = ch.free_at.saturating_sub(self.now);
                ch.sending = Some(f);
                self.schedule(Ev::Timeout(proc), NORMAL, tx + gap);
            }
            Proc::AdapterRx(a) => {
                let fr = self.frames[f as usize];
                let ad = &mut self.adapters[a as usize];
                if fr.dst == ad.mac {
                    ad.bytes_received += fr.size as u128;
                    ad.frames_received += 1;
                    for w in &ad.latency_windows {
                        self.windows[*w as usize].push(self.now - fr.created);
                    }
                    let accepted = fr.modbus.response
                        && match ad.transactions.iter().find(|(mac, _)| *mac == fr.src) {
                            None => true,
                            // a late response: the master has given up on it
                            Some(&(_, t)) => {
                                self.transactions[t as usize].accept(fr.modbus.tid, self.now)
                            }
                        };
                    if accepted {
                        ad.modbus_responses += 1;
                        for w in &ad.modbus_windows {
                            self.windows[*w as usize].push(self.now - fr.created);
                        }
                        if let Some(&s) = self.slave_of.get(&fr.src) {
                            let sl = &mut self.slaves[s as usize];
                            sl.responses += 1;
                            for w in &sl.modbus_windows {
                                self.windows[*w as usize].push(self.now - fr.created);
                            }
                        }
                    }
                    if fr.pn_ar >= 0 {
                        let pn = ad.pn;
                        self.pn_record(pn, fr);
                    }
                }
                self.release(f);
                self.get(store, proc);
            }
            Proc::PnDeviceRx(d) => {
                let fr = self.frames[f as usize];
                let dev = &self.devices[d as usize];
                // a flow's frames (ADR-0013) are not cyclic: the consumer ignores them
                if fr.dst == dev.mac && fr.pn_ar >= 0 {
                    let pn = dev.pn;
                    self.pn_record(pn, fr);
                }
                self.release(f);
                self.get(store, proc);
            }
            Proc::Switch(w) => {
                self.forward(w, f);
                self.get(store, proc);
            }
            Proc::Slave(i) => {
                let fr = self.frames[f as usize];
                let sl = &self.slaves[i as usize];
                // a flooded frame for another station, or not for this unit: dropped
                if fr.dst == sl.mac && fr.modbus.unit == sl.unit as i16 {
                    let response = Frame {
                        modbus: Modbus {
                            unit: fr.modbus.unit,
                            count: fr.modbus.count,
                            response: true,
                            tid: fr.modbus.tid, // MBAP echoes the transaction id
                        },
                        // the request's origin time: modbus_latency is a round trip
                        ..Frame::new(
                            sl.mac,
                            fr.src,
                            modbus_response_frame_size(fr.modbus.count),
                            fr.created,
                        )
                    };
                    let chan = sl.out.expect("a slave process has a link");
                    let r = self.alloc(response);
                    self.enqueue(chan, r);
                }
                self.release(f);
                self.get(store, proc);
            }
            Proc::Broker(i) => {
                let fr = self.frames[f as usize];
                let (mac, chan) = {
                    let b = &self.brokers[i as usize];
                    (b.mac, b.out.expect("a broker process has a link"))
                };
                // a flooded frame for another station, or not a PUBLISH: dropped
                if fr.dst == mac && fr.mqtt.topic >= 0 {
                    let topic = fr.mqtt.topic as usize;
                    for k in 0..self.brokers[i as usize].routes[topic].len() {
                        let sub = self.brokers[i as usize].routes[topic][k];
                        let copy = Frame {
                            mqtt: Mqtt {
                                topic: fr.mqtt.topic,
                                qos: 0,
                            },
                            // the publisher's origin time: latency is end to end
                            ..Frame::new(mac, sub, fr.size, fr.created)
                        };
                        let c = self.alloc(copy);
                        self.enqueue(chan, c);
                    }
                    if fr.mqtt.qos >= 1 {
                        let ack = self.alloc(Frame::new(mac, fr.src, MQTT_PUBACK_BYTES, self.now));
                        self.enqueue(chan, ack);
                    }
                }
                self.release(f);
                self.get(store, proc);
            }
            Proc::Flow(_)
            | Proc::Sampler(_)
            | Proc::Periodic(_)
            | Proc::PnProvider(_)
            | Proc::Watchdog(_) => {
                unreachable!("these processes do not get")
            }
        }
    }

    /// A frame at a switch (`_switch_forwarder`). With ingress accounts it is
    /// dropped if its ingress port's account has no room; otherwise each
    /// copy is admitted by its egress port, and the frame is charged once if
    /// a copy was queued (ADR-0013).
    fn forward(&mut self, w: u32, f: u32) {
        let fr = self.frames[f as usize];
        let sw = &mut self.switches[w as usize];
        sw.mac_table.insert(fr.src, fr.in_port);
        let dst = sw.mac_table.get(&fr.dst).copied();
        if dst == Some(fr.in_port) {
            self.release(f); // hairpin: dropped
            return;
        }
        let entry = if sw.ingress.is_empty() {
            None
        } else {
            let port = fr.in_port as u32;
            if !sw.admit_ingress(port as usize, fr.size, self.now) {
                sw.ingress_dropped += 1; // tail drop: no egress port is looked at
                self.release(f);
                return;
            }
            let st = self.new_stored(w, port);
            self.frames[f as usize].stored = st as i32;
            Some((st, port))
        };
        let fr = self.frames[f as usize];
        let mut copies = 0;
        match dst {
            None => {
                // flood a copy to every port but the one it came from
                let ports: Vec<(i32, u32)> = self.switches[w as usize].out.clone();
                for (port, ch) in ports {
                    if port != fr.in_port {
                        let copy = self.alloc(fr);
                        copies += self.enqueue(ch, copy) as u32;
                    }
                }
                self.release(f);
            }
            Some(port) => {
                // ports are numbered 0, 1, ... in link order: out[port] is its channel
                let ch = self.switches[w as usize].out[port as usize].1;
                copies += self.enqueue(ch, f) as u32;
            }
        }
        // a store never pops a frame within the put that queues it, so the
        // account is charged before any copy can be taken
        if let Some((st, port)) = entry {
            if copies > 0 {
                self.stored[st as usize].copies = copies;
                self.switches[w as usize].charge_ingress(port as usize, fr.size);
            } else {
                self.stored_free.push(st);
            }
        }
    }

    fn on_timeout(&mut self, proc: u32) {
        match self.procs[proc as usize] {
            Proc::Tx(c) => {
                let now = self.now;
                let ch = &mut self.channels[c as usize];
                let f = ch.sending.take().expect("a frame in transmission");
                ch.free_at = now + ch.ifg_ns;
                ch.bits_sent += self.frames[f as usize].size as u128 * 8;
                ch.frames_sent += 1;
                let (prop, q) = (ch.prop_ns, ch.queue);
                self.schedule(Ev::InFlight(c, f), NORMAL, prop);
                self.get(q, proc);
            }
            Proc::Flow(i) => self.flow_step(i, proc),
            Proc::Sampler(i) => {
                self.sample(i);
                let t = self.samplers[i as usize].ticks.next().unwrap();
                self.schedule(Ev::Timeout(proc), NORMAL, t - self.now);
            }
            Proc::Periodic(i) => {
                let pg = &mut self.periodic[i as usize];
                if pg.delayed {
                    pg.delayed = false;
                } else {
                    self.periodic_send(i);
                }
                let t = self.periodic[i as usize].instants.next().unwrap();
                self.schedule(Ev::Timeout(proc), NORMAL, t - self.now);
            }
            Proc::PnProvider(i) => self.pn_send(i, proc),
            Proc::AdapterRx(_)
            | Proc::Switch(_)
            | Proc::Slave(_)
            | Proc::Broker(_)
            | Proc::PnDeviceRx(_)
            | Proc::Watchdog(_) => {
                unreachable!("these processes do not time out")
            }
        }
    }

    /// A poll or publication at one of its instants. A poll with one request
    /// in flight skips the instant while its slot is not free, and otherwise
    /// starts the request's timeout after its Put, as `_modbus_poll_gen`.
    fn periodic_send(&mut self, i: u32) {
        let now = self.now;
        let pg = &self.periodic[i as usize];
        let (chan, requester, slot) = (pg.chan, pg.requester, pg.transaction);
        let mut frame = Frame {
            created: now,
            ..pg.frame
        };
        let mut timeout = None;
        if let Some(t) = slot {
            let tr = &mut self.transactions[t as usize];
            if !tr.free(now) {
                self.adapters[tr.adapter as usize].modbus_skipped += 1;
                self.slaves[tr.slave as usize].skipped += 1;
                return;
            }
            frame.modbus.tid = tr.send(now);
            timeout = tr.timeout_ns.map(|d| (t, frame.modbus.tid, d));
        }
        if let Some((a, s)) = requester {
            self.adapters[a as usize].modbus_requests += 1;
            self.slaves[s as usize].requests += 1;
        }
        let f = self.alloc(frame);
        self.enqueue(chan, f);
        if let Some((t, tid, d)) = timeout {
            // both under 2^63 ns (`tb::ms_to_ns`): the sum fits
            self.schedule(Ev::PollTimeout(t, tid), NORMAL, d);
        }
    }

    /// The timeout of request *tid*: it times out if still outstanding.
    fn on_poll_timeout(&mut self, t: u32, tid: u64) {
        let tr = &mut self.transactions[t as usize];
        if tr.outstanding && tid + 1 == tr.sent {
            tr.outstanding = false;
            tr.completed_at = Some(self.now);
            self.adapters[tr.adapter as usize].modbus_timeouts += 1;
            self.slaves[tr.slave as usize].timeouts += 1;
        }
    }

    /// The first gap of a flow's pattern, after its delay.
    fn flow_begin(&mut self, i: u32, proc: u32) {
        let fl = &mut self.flows[i as usize];
        fl.state = FlowState::Run;
        let delay = match fl.pattern {
            Pattern::Constant => fl.instants.next().unwrap() - self.now,
            Pattern::Poisson => tb::draw_ns(fl.rng.expovariate(1.0 / fl.mean_ns)),
            Pattern::Batch(b) => tb::draw_ns(fl.rng.expovariate(1.0 / (fl.mean_ns * b.mean()))),
            Pattern::Bursty => return self.flow_burst(i, proc),
        };
        self.schedule(Ev::Timeout(proc), NORMAL, delay);
    }

    /// A new burst: L = max(1, floor(Pareto(1.5))) frames spaced T / 4.
    fn flow_burst(&mut self, i: u32, proc: u32) {
        let now = self.now;
        let fl = &mut self.flows[i as usize];
        let len = (fl.rng.paretovariate(1.5) as u64).max(1);
        let mut ticks = Instants::new(now, fl.quarter);
        let t = ticks.next().unwrap();
        fl.state = FlowState::Burst {
            left: len,
            len,
            ticks,
        };
        self.schedule(Ev::Timeout(proc), NORMAL, t - now);
    }

    fn flow_step(&mut self, i: u32, proc: u32) {
        let fl = &mut self.flows[i as usize];
        match &mut fl.state {
            FlowState::Delay => self.flow_begin(i, proc),
            FlowState::Silence => self.flow_burst(i, proc),
            FlowState::Run => {
                let delay = match fl.pattern {
                    Pattern::Constant => {
                        self.flow_send(i);
                        self.flows[i as usize].instants.next().unwrap() - self.now
                    }
                    Pattern::Poisson => {
                        self.flow_send(i);
                        let fl = &mut self.flows[i as usize];
                        tb::draw_ns(fl.rng.expovariate(1.0 / fl.mean_ns))
                    }
                    Pattern::Batch(b) => {
                        let k = b.draw(&mut fl.rng);
                        for _ in 0..k {
                            self.flow_send(i);
                        }
                        let fl = &mut self.flows[i as usize];
                        tb::draw_ns(fl.rng.expovariate(1.0 / (fl.mean_ns * b.mean())))
                    }
                    Pattern::Bursty => unreachable!("a bursty flow is in a burst or a silence"),
                };
                self.schedule(Ev::Timeout(proc), NORMAL, delay);
            }
            FlowState::Burst { .. } => {
                self.flow_send(i);
                let now = self.now;
                let fl = &mut self.flows[i as usize];
                let FlowState::Burst { left, len, ticks } = &mut fl.state else {
                    unreachable!()
                };
                *left -= 1;
                let delay = if *left > 0 {
                    ticks.next().unwrap() - now
                } else {
                    // OFF: silence with mean 0.75 T per frame, so a cycle averages T per frame
                    let len = *len as f64;
                    let silence = fl.mean_ns * len * fl.rng.uniform(0.3, 1.2);
                    fl.state = FlowState::Silence;
                    tb::draw_ns(silence)
                };
                self.schedule(Ev::Timeout(proc), NORMAL, delay);
            }
        }
    }

    /// One frame of a flow, its size drawn now for IMIX.
    fn flow_send(&mut self, i: u32) {
        let fl = &mut self.flows[i as usize];
        let size = match fl.size {
            Size::Fixed(s) => s,
            Size::Imix => {
                // 40 % 64 B, 57 % 594 B, 3 % 1 518 B (frames.IMIX_SIZES)
                let r = fl.rng.random();
                if r < 0.40 {
                    64
                } else if r < 0.97 {
                    594
                } else {
                    1518
                }
            }
        };
        let frame = Frame {
            priority: fl.priority,
            ..Frame::new(fl.src, fl.dst, flow_frame_size(size, fl.priority), self.now)
        };
        let chan = fl.chan;
        let f = self.alloc(frame);
        self.enqueue(chan, f);
    }

    /// A provider's frame, then the timeout to the next cycle.
    fn pn_send(&mut self, i: u32, proc: u32) {
        let p = &self.pn_providers[i as usize];
        let (frame, chan, cycle) = (
            Frame {
                created: self.now,
                ..p.frame
            },
            p.chan,
            p.cycle_ns,
        );
        let f = self.alloc(frame);
        self.enqueue(chan, f);
        self.schedule(Ev::Timeout(proc), NORMAL, cycle);
    }

    /// A cyclic frame at its consumer (`_PnConsumer.record`).
    fn pn_record(&mut self, consumer: u32, fr: Frame) {
        let now = self.now;
        let r = fr.pn_ar as usize;
        if self.pn_ars[r].consumer != consumer {
            return; // not one of this consumer's ARs
        }
        let c = &self.pn_consumers[consumer as usize];
        for w in &c.latency_windows {
            self.windows[*w as usize].push(now - fr.created);
        }
        let ar = &self.pn_ars[r];
        match ar.last_arrival {
            // supervision starts with the first frame, as after AR establishment
            None => self.process(Proc::Watchdog(r as u32)),
            Some(last) => {
                let jitter = (now - last).abs_diff(ar.cycle_ns);
                for w in &c.jitter_windows {
                    self.windows[*w as usize].push(jitter);
                }
            }
        }
        let ar = &mut self.pn_ars[r];
        ar.last_arrival = Some(now);
        if ar.arrival == Some(Signal::Pending) {
            ar.arrival = Some(Signal::Triggered);
            let round = ar.round;
            self.schedule(Ev::Arrival(r as u32, round), NORMAL, 0);
        }
    }

    /// A new watchdog round: a new arrival event, a timeout and the
    /// condition waiting on both.
    fn watchdog_round(&mut self, r: u32) {
        let ar = &mut self.pn_ars[r as usize];
        ar.round += 1;
        ar.arrival = Some(Signal::Pending);
        ar.condition_triggered = false;
        ar.expired = false;
        let (round, timeout) = (ar.round, ar.timeout_ns);
        self.schedule(Ev::Watchdog(r, round), NORMAL, timeout);
    }

    fn on_arrival(&mut self, r: u32, round: u32) {
        let ar = &mut self.pn_ars[r as usize];
        debug_assert_eq!(round, ar.round, "an arrival is always of the current round");
        ar.arrival = Some(Signal::Processed);
        if ar.expired {
            // the watchdog waited for this arrival alone: the next round
            self.watchdog_round(r);
        } else if !ar.condition_triggered {
            ar.condition_triggered = true;
            self.schedule(Ev::Condition(r, round), NORMAL, 0);
        }
    }

    fn on_watchdog(&mut self, r: u32, round: u32) {
        let ar = &mut self.pn_ars[r as usize];
        // a timeout of an earlier round, or one that lost to the arrival
        if round == ar.round && !ar.condition_triggered {
            ar.condition_triggered = true;
            self.schedule(Ev::Condition(r, round), NORMAL, 0);
        }
    }

    fn on_condition(&mut self, r: u32, round: u32) {
        let ar = &mut self.pn_ars[r as usize];
        debug_assert_eq!(round, ar.round);
        if ar.arrival == Some(Signal::Processed) {
            self.watchdog_round(r);
        } else {
            // real devices abort the AR here; it is counted and supervision
            // resumes with the next frame (ADR-0005)
            ar.expired = true;
            let c = ar.consumer;
            self.pn_consumers[c as usize].expiries += 1;
        }
    }

    fn on_in_flight(&mut self, c: u32, f: u32) {
        let ch = &self.channels[c as usize];
        let (port, dst) = (ch.in_port, ch.dst_store);
        self.frames[f as usize].in_port = port;
        self.put_fifo(dst, f);
    }

    fn sample(&mut self, i: u32) {
        let now = self.now;
        let t_ms = tb::ratio_f64(now as u128, tb::NS_PER_MS);
        let s = &self.samplers[i as usize];
        let window = (now - s.last_t) as u128;
        let (value, last) = match &s.what {
            Sampled::Throughput(a) => {
                let bytes = self.adapters[*a as usize].bytes_received;
                (
                    Some(tb::ratio_f64((bytes - s.last) * 8 * tb::NS_PER_US, window)),
                    bytes,
                )
            }
            Sampled::Latency(w) => {
                let values = &mut self.windows[*w as usize];
                let v = if values.is_empty() {
                    None
                } else {
                    let sum: u128 = values.iter().map(|v| *v as u128).sum();
                    let v = tb::ratio_f64(sum, values.len() as u128 * tb::NS_PER_US);
                    values.clear();
                    Some(v)
                };
                (v, 0)
            }
            Sampled::BytesSent(c) => {
                let bits = self.channels[*c as usize].bits_sent;
                (Some(tb::ratio_f64(bits / 8, 1_000_000)), 0)
            }
            Sampled::BytesReceived(a) => {
                let bytes = self.adapters[*a as usize].bytes_received;
                (Some(tb::ratio_f64(bytes, 1_000_000)), 0)
            }
            Sampled::QueueDepth(w) => {
                let depth: usize = self.switches[*w as usize]
                    .out
                    .iter()
                    .map(|(_, c)| self.stores[self.channels[*c as usize].queue as usize].len())
                    .sum();
                (Some(depth as f64), 0)
            }
            Sampled::Utilization(chans, speed) => {
                let bits: u128 = chans
                    .iter()
                    .map(|c| {
                        let ch = &self.channels[*c as usize];
                        ch.bits_sent + ch.frames_sent * WIRE_OVERHEAD_BYTES as u128 * 8
                    })
                    .sum();
                // bits · 10⁵ / (speed · window): speed is exact, in Mb/s
                let v = tb::ratio_f64((bits - s.last) * 100_000 * speed.den, speed.num * window);
                (Some(v), bits)
            }
            Sampled::BrokerQueue(store) => (Some(self.stores[*store as usize].len() as f64), 0),
            Sampled::Jitter(w) => {
                let values = &mut self.windows[*w as usize];
                let v = values
                    .iter()
                    .max()
                    .map(|m| tb::ratio_f64(*m as u128, tb::NS_PER_US));
                values.clear();
                (v, 0)
            }
            Sampled::WatchdogExpiries(c) => {
                (Some(self.pn_consumers[*c as usize].expiries as f64), 0)
            }
            Sampled::Drops(chans, w, which, window) => {
                let egress: u128 = chans
                    .iter()
                    .map(|c| self.channels[*c as usize].dropped as u128)
                    .sum();
                let ingress = self.switches[*w as usize].ingress_dropped as u128;
                let total = match which {
                    Drops::All => egress + ingress,
                    Drops::Ingress => ingress,
                    Drops::Egress => egress,
                };
                let v = if *window { total - s.last } else { total };
                (Some(v as f64), total)
            }
            Sampled::FramesSent(c) => (Some(self.channels[*c as usize].frames_sent as f64), 0),
            Sampled::FramesReceived(a) => {
                (Some(self.adapters[*a as usize].frames_received as f64), 0)
            }
            Sampled::ModbusUnanswered(a) => {
                let ad = &self.adapters[*a as usize];
                (Some((ad.modbus_requests - ad.modbus_responses) as f64), 0)
            }
            Sampled::ModbusTimeouts(a) => {
                (Some(self.adapters[*a as usize].modbus_timeouts as f64), 0)
            }
            Sampled::ModbusSkipped(a) => {
                (Some(self.adapters[*a as usize].modbus_skipped as f64), 0)
            }
            Sampled::SlaveUnanswered(s) => {
                let sl = &self.slaves[*s as usize];
                (Some((sl.requests - sl.responses) as f64), 0)
            }
            Sampled::SlaveTimeouts(s) => (Some(self.slaves[*s as usize].timeouts as f64), 0),
            Sampled::SlaveSkipped(s) => (Some(self.slaves[*s as usize].skipped as f64), 0),
            Sampled::PoolOccupancy(w) => {
                let used = self.switches[*w as usize].pool_use();
                (Some(used as f64), 0)
            }
            Sampled::Episodes(chans, w, ingress, time) => {
                let sw = &self.switches[*w as usize];
                let accounts: Vec<&Account> = if *ingress && sw.state.is_some() {
                    // the switch's entries and time (ADR-0016)
                    sw.state.iter().collect()
                } else if *ingress {
                    sw.ingress.iter().collect()
                } else {
                    chans
                        .iter()
                        .filter_map(|c| self.channels[*c as usize].egress.as_ref())
                        .collect()
                };
                let v = if *time {
                    let ns: u128 = accounts.iter().map(|a| a.drop_time_ns(now) as u128).sum();
                    tb::ratio_f64(ns, tb::NS_PER_MS)
                } else {
                    accounts.iter().map(|a| a.episodes).sum::<u64>() as f64
                };
                (Some(v), 0)
            }
        };
        let s = &mut self.samplers[i as usize];
        s.last_t = now;
        s.last = last;
        if let Some(v) = value {
            self.out[s.out].1.push((t_ms, v));
        }
    }
}

/// The flows of an end station, in statement order (ADR-0013), as the
/// reference engine's `start_flows`.
fn add_flows(
    e: &mut Engine,
    sc: &Scenario,
    name: &str,
    src: u32,
    chan: u32,
    station_mac: &[Option<u32>],
    lookup: &dyn Fn(&str) -> Result<usize, String>,
) -> Result<(), String> {
    for (n, flow) in sc.flows.iter().enumerate() {
        if flow.source != name {
            continue;
        }
        let dst = lookup(&flow.destination)?;
        let dst = station_mac[dst].ok_or("a flow goes to an end station")?;
        let delay_ns = tb::ms_to_ns(flow.delay_ms)?;
        let period = tb::interval_ns(flow.rate)?;
        // source id: earlier flows between the same two ends
        let k = sc.flows[..n]
            .iter()
            .filter(|f| f.source == flow.source && f.destination == flow.destination)
            .count();
        let id = format!("flow:{}->{}#{k}", flow.source, flow.destination);
        e.flows.push(FlowGen {
            src,
            dst,
            size: flow.size,
            priority: flow.priority,
            chan,
            delay_ns,
            pattern: flow.pattern,
            state: FlowState::Run,
            instants: Instants::new(delay_ns, period),
            mean_ns: tb::ratio_f64(period.num, period.den),
            quarter: Q::new(period.num, period.den * 4)?,
            rng: Rng::new(stream_seed(sc.seed, &id)),
        });
        e.process(Proc::Flow((e.flows.len() - 1) as u32));
    }
    Ok(())
}

/// Run a scenario: build the model as `smolpy.sim.engine.run_simulation`
/// does, then process events until `t = duration`.
pub fn run(sc: &Scenario) -> Result<Outcome, String> {
    let duration_ns = tb::ms_to_ns(sc.duration_ms)?;
    let mut e = Engine {
        now: 0,
        seq: 0,
        heap: BinaryHeap::new(),
        events: 0,
        frames: Vec::new(),
        free: Vec::new(),
        stores: Vec::new(),
        channels: Vec::new(),
        adapters: Vec::new(),
        switches: Vec::new(),
        stored: Vec::new(),
        stored_free: Vec::new(),
        flows: Vec::new(),
        slaves: Vec::new(),
        slave_of: HashMap::new(),
        brokers: Vec::new(),
        periodic: Vec::new(),
        transactions: Vec::new(),
        devices: Vec::new(),
        pn_consumers: Vec::new(),
        pn_ars: Vec::new(),
        pn_providers: Vec::new(),
        samplers: Vec::new(),
        windows: Vec::new(),
        procs: Vec::new(),
        out: Vec::new(),
    };

    let index: HashMap<&str, usize> = sc
        .nodes
        .iter()
        .enumerate()
        .map(|(i, n)| (n.name.as_str(), i))
        .collect();
    let lookup = |name: &str| -> Result<usize, String> {
        index
            .get(name)
            .copied()
            .ok_or_else(|| format!("reference to undeclared node {name:?}"))
    };
    let mut macs: HashMap<String, u32> = HashMap::new();
    let mut intern = |mac: &str| -> u32 {
        let n = macs.len() as u32;
        *macs.entry(mac.to_string()).or_insert(n)
    };

    // every node gets an inbound store; adapters and switches their runtime state
    let mut inbound = Vec::new();
    let mut role = Vec::new(); // index into adapters or switches
    let mut pn_of = Vec::new(); // the PROFINET consumer of adapters and devices
    for node in &sc.nodes {
        let s = e.store(Items::Fifo(VecDeque::new()));
        inbound.push(s);
        let pn = e.pn_consumers.len() as u32;
        if matches!(
            node.kind,
            NodeKind::Adapter { .. } | NodeKind::ProfinetDevice { .. }
        ) {
            e.pn_consumers.push(PnConsumer {
                latency_windows: Vec::new(),
                jitter_windows: Vec::new(),
                expiries: 0,
            });
            pn_of.push(Some(pn));
        } else {
            pn_of.push(None);
        }
        match &node.kind {
            NodeKind::Adapter { mac } => {
                let mac = intern(mac);
                e.adapters.push(Adapter {
                    mac,
                    inbound: s,
                    out: None,
                    bytes_received: 0,
                    frames_received: 0,
                    modbus_requests: 0,
                    modbus_responses: 0,
                    modbus_timeouts: 0,
                    modbus_skipped: 0,
                    transactions: Vec::new(),
                    latency_windows: Vec::new(),
                    modbus_windows: Vec::new(),
                    pn,
                });
                role.push(e.adapters.len() - 1);
            }
            NodeKind::ProfinetDevice { mac } => {
                let mac = intern(mac);
                e.devices.push(Device {
                    mac,
                    inbound: s,
                    out: None,
                    pn,
                });
                role.push(e.devices.len() - 1);
            }
            NodeKind::ModbusSlave { mac, unit } => {
                let mac = intern(mac);
                e.slave_of.insert(mac, e.slaves.len() as u32);
                e.slaves.push(Slave {
                    mac,
                    unit: *unit,
                    inbound: s,
                    out: None,
                    requests: 0,
                    responses: 0,
                    timeouts: 0,
                    skipped: 0,
                    modbus_windows: Vec::new(),
                });
                role.push(e.slaves.len() - 1);
            }
            NodeKind::MqttBroker { mac } => {
                let mac = intern(mac);
                e.brokers.push(Broker {
                    mac,
                    inbound: s,
                    out: None,
                    routes: Vec::new(),
                });
                role.push(e.brokers.len() - 1);
            }
            NodeKind::Switch { .. } => {
                e.switches.push(Switch {
                    inbound: s,
                    out: Vec::new(),
                    mac_table: HashMap::new(),
                    ingress: Vec::new(),
                    ingress_dropped: 0,
                    pool: None,
                    freed: 0,
                    draining: Vec::new(),
                    state: None,
                    held: 0,
                    net: false,
                    entry_held: 0,
                    alpha: None,
                    mark: None,
                });
                role.push(e.switches.len() - 1);
            }
        }
    }
    let is_switch = |i: usize| matches!(sc.nodes[i].kind, NodeKind::Switch { .. });
    let strict = |i: usize| !matches!(sc.nodes[i].kind, NodeKind::Switch { fifo: true, .. });
    // only switches have buffer accounts; end stations never drop
    let egress = |i: usize| match sc.nodes[i].kind {
        NodeKind::Switch {
            buffer,
            cell,
            buffer_resume,
            ..
        } => Account::new(buffer, cell, buffer_resume),
        _ => None,
    };
    let accounted = |i: usize| {
        matches!(
            sc.nodes[i].kind,
            NodeKind::Switch {
                buffer: Some(_),
                ..
            } | NodeKind::Switch {
                ingress: Some(_),
                ..
            } | NodeKind::Switch { pool: Some(_), .. }
        )
    };
    // every node but a switch is an end station with a MAC address
    let mac_of = |i: usize| match &sc.nodes[i].kind {
        NodeKind::Adapter { mac }
        | NodeKind::ModbusSlave { mac, .. }
        | NodeKind::MqttBroker { mac }
        | NodeKind::ProfinetDevice { mac } => Some(mac.clone()),
        NodeKind::Switch { .. } => None,
    };
    let is_adapter = |i: usize| matches!(sc.nodes[i].kind, NodeKind::Adapter { .. });
    let station_mac: Vec<Option<u32>> = (0..sc.nodes.len())
        .map(|i| mac_of(i).map(|m| intern(&m)))
        .collect();

    let mut next_port = vec![0i32; sc.nodes.len()];
    let mut in_channels: Vec<Vec<u32>> = vec![Vec::new(); sc.nodes.len()];
    let mut neighbours: Vec<Vec<(i32, usize)>> = vec![Vec::new(); sc.nodes.len()];
    let mut static_macs: Vec<HashMap<u32, i32>> = vec![HashMap::new(); sc.nodes.len()];
    let mut speeds: Vec<Q> = Vec::new(); // per channel, for utilization
    let mut tx_procs = Vec::new();
    for link in &sc.links {
        let (a, b) = (lookup(&link.a)?, lookup(&link.b)?);
        let (port_a, port_b) = (next_port[a], next_port[b]);
        next_port[a] += 1;
        next_port[b] += 1;
        let prop_ns = tb::propagation_ns(link.length_m)?;
        let ifg_ns = tb::transmission_ns(IFG_BYTES, link.speed_mbps)?;
        let mut channel = |e: &mut Engine, in_port: i32, dst: usize, owner: usize| -> u32 {
            let queue = e.store(Items::Prio(BinaryHeap::new()));
            if accounted(owner) {
                e.stores[queue as usize].accounted = Some(e.channels.len() as u32);
            }
            e.channels.push(Channel {
                speed: link.speed_mbps,
                prop_ns,
                in_port,
                dst_store: inbound[dst],
                queue,
                strict: strict(owner),
                seq: 0,
                tx_ns: HashMap::new(),
                ifg_ns,
                free_at: 0,
                bits_sent: 0,
                frames_sent: 0,
                sending: None,
                egress: egress(owner),
                dropped: 0,
            });
            speeds.push(link.speed_mbps);
            (e.channels.len() - 1) as u32
        };
        // ch_ab is a's egress queue, ch_ba is b's; each transmitter starts now
        let ch_ab = channel(&mut e, port_b, b, a);
        tx_procs.push(ch_ab);
        e.process(Proc::Tx(ch_ab));
        let ch_ba = channel(&mut e, port_a, a, b);
        tx_procs.push(ch_ba);
        e.process(Proc::Tx(ch_ba));

        for (node, port, ch) in [(a, port_a, ch_ab), (b, port_b, ch_ba)] {
            if is_switch(node) {
                e.switches[role[node]].out.push((port, ch));
            }
        }
        in_channels[b].push(ch_ab);
        in_channels[a].push(ch_ba);
        neighbours[a].push((port_a, b));
        neighbours[b].push((port_b, a));
        // an end station's single link is its way out (the model allows one)
        for (node, other, ch, port) in [(a, b, ch_ab, port_b), (b, a, ch_ba, port_a)] {
            if let Some(mac) = mac_of(node) {
                match &sc.nodes[node].kind {
                    NodeKind::Adapter { .. } => e.adapters[role[node]].out = Some(ch),
                    NodeKind::ModbusSlave { .. } => e.slaves[role[node]].out = Some(ch),
                    NodeKind::MqttBroker { .. } => e.brokers[role[node]].out = Some(ch),
                    NodeKind::ProfinetDevice { .. } => e.devices[role[node]].out = Some(ch),
                    NodeKind::Switch { .. } => unreachable!(),
                }
                if is_switch(other) {
                    static_macs[other].insert(intern(&mac), port);
                }
            }
        }
    }
    let _ = tx_procs;
    // ingress accounts, one per port (ADR-0013)
    for (i, node) in sc.nodes.iter().enumerate() {
        if let NodeKind::Switch {
            ingress,
            cell,
            ingress_resume,
            pool,
            reserve,
            drain,
            switch_drop,
            drain_net,
            pool_total,
            alpha,
            mark,
            ..
        } = node.kind
        {
            // with a pool and no cap of its own, an account is limited by
            // the pool alone, in the pool's unit
            let cap = ingress.or(pool.map(|p| match p {
                Limit::Bytes(_) => Limit::Bytes(u64::MAX / 2),
                Limit::Frames(_) => Limit::Frames(u64::MAX / 2),
                Limit::Cells(_) => Limit::Cells(u64::MAX / 2),
            }));
            if let Some(mut account) = Account::new(cap, cell, ingress_resume) {
                let sw = &mut e.switches[role[i]];
                if switch_drop {
                    // the ports have no drop state of their own (ADR-0016)
                    let mut state = account;
                    state.drain = drain;
                    sw.state = Some(state);
                    sw.net = drain_net;
                    sw.alpha = alpha;
                    sw.mark = mark;
                } else {
                    account.drain = drain;
                }
                sw.ingress = vec![account; sw.out.len()];
                sw.pool = pool.map(|p| Pool {
                    limit: match p {
                        Limit::Bytes(n) | Limit::Frames(n) | Limit::Cells(n) => n,
                    },
                    reserve: reserve.unwrap_or(0),
                    used: 0,
                    total: pool_total,
                });
            }
        }
    }

    // converged MAC tables: breadth-first search through switches, the first
    // port to reach a station wins (ties to the lowest port)
    for (i, node) in sc.nodes.iter().enumerate() {
        if let NodeKind::Switch { topology: true, .. } = node.kind {
            let mut seen = vec![false; sc.nodes.len()];
            seen[i] = true;
            let mut frontier = neighbours[i].clone();
            frontier.sort_by(|x, y| (x.0, &sc.nodes[x.1].name).cmp(&(y.0, &sc.nodes[y.1].name)));
            for (_, peer) in &frontier {
                seen[*peer] = true;
            }
            let mut table: HashMap<u32, i32> = HashMap::new(); // first port wins
            while !frontier.is_empty() {
                let mut next = Vec::new();
                for (port, name) in &frontier {
                    match mac_of(*name) {
                        None => {
                            for (_, peer) in &neighbours[*name] {
                                if !seen[*peer] {
                                    seen[*peer] = true;
                                    next.push((*port, *peer));
                                }
                            }
                        }
                        Some(mac) => {
                            table.entry(intern(&mac)).or_insert(*port);
                        }
                    }
                }
                frontier = next;
            }
            for (mac, port) in table {
                static_macs[i].entry(mac).or_insert(port);
            }
        }
    }

    // MQTT topics, numbered in order of first mention
    let mut topics: HashMap<&str, i32> = HashMap::new();
    for topic in sc
        .publishes
        .iter()
        .map(|p| p.topic.as_str())
        .chain(sc.routes.iter().map(|r| r.topic.as_str()))
    {
        let n = topics.len() as i32;
        topics.entry(topic).or_insert(n);
    }

    // node processes, in node order: an adapter's receiver, its PROFINET
    // providers, then its flows
    for (i, node) in sc.nodes.iter().enumerate() {
        match &node.kind {
            NodeKind::Adapter { .. } => {
                let a = role[i] as u32;
                e.process(Proc::AdapterRx(a));
                for ar in sc.ars.iter().filter(|ar| ar.controller == node.name) {
                    let d = lookup(&ar.device)?;
                    let NodeKind::ProfinetDevice { mac } = &sc.nodes[d].kind else {
                        return Err(format!(
                            "PROFINET AR {} -> {}: not a PROFINET device",
                            ar.controller, ar.device
                        ));
                    };
                    let device = &e.devices[role[d]];
                    let (Some(chan), Some(device_chan)) = (e.adapters[a as usize].out, device.out)
                    else {
                        continue; // one side not wired: no cyclic exchange
                    };
                    let (controller_pn, device_pn) = (e.adapters[a as usize].pn, device.pn);
                    let cycle_ns = tb::ms_to_ns(ar.cycle_ms)?;
                    let delay_ns = tb::ms_to_ns(ar.delay_ms)?;
                    let timeout_ns = ar
                        .hold
                        .checked_mul(cycle_ns)
                        .ok_or("PROFINET AR: watchdog time")?;
                    // the consumer states: inputs at the controller, outputs at the device
                    let state = |e: &mut Engine, consumer: u32| -> i32 {
                        e.pn_ars.push(PnAr {
                            consumer,
                            cycle_ns,
                            timeout_ns,
                            last_arrival: None,
                            arrival: None,
                            round: 0,
                            condition_triggered: false,
                            expired: false,
                        });
                        (e.pn_ars.len() - 1) as i32
                    };
                    let inputs = state(&mut e, controller_pn);
                    let outputs = state(&mut e, device_pn);
                    let (controller_mac, device_mac) = (e.adapters[a as usize].mac, intern(mac));
                    for (src, dst, size, chan, pn_ar) in [
                        (controller_mac, device_mac, ar.outputs, chan, outputs),
                        (device_mac, controller_mac, ar.inputs, device_chan, inputs),
                    ] {
                        e.pn_providers.push(PnProvider {
                            frame: Frame {
                                priority: PN_RT_PRIORITY,
                                pn_ar,
                                ..Frame::new(src, dst, pn_frame_size(size), 0)
                            },
                            chan,
                            delay_ns,
                            cycle_ns,
                        });
                        e.process(Proc::PnProvider((e.pn_providers.len() - 1) as u32));
                    }
                }
                if let Some(chan) = e.adapters[a as usize].out {
                    let src = e.adapters[a as usize].mac;
                    add_flows(&mut e, sc, &node.name, src, chan, &station_mac, &lookup)?;
                    // then its MQTT publications and its Modbus polls, as the
                    // reference engine starts them
                    for publish in sc.publishes.iter().filter(|p| p.source == node.name) {
                        let broker = lookup(&publish.broker)?;
                        let NodeKind::MqttBroker { mac } = &sc.nodes[broker].kind else {
                            return Err(format!(
                                "publish {} -> {}: not an MQTT broker",
                                publish.source, publish.broker
                            ));
                        };
                        // len(topic) in the reference engine counts characters
                        let size = mqtt_publish_frame_size(
                            publish.topic.chars().count() as u64,
                            publish.payload,
                            publish.qos,
                        );
                        let delay_ns = tb::ms_to_ns(publish.delay_ms)?;
                        e.periodic.push(Periodic {
                            frame: Frame {
                                mqtt: Mqtt {
                                    topic: topics[publish.topic.as_str()],
                                    qos: publish.qos,
                                },
                                ..Frame::new(e.adapters[a as usize].mac, intern(mac), size, 0)
                            },
                            chan,
                            delay_ns,
                            delayed: false,
                            instants: Instants::new(delay_ns, tb::interval_ns(publish.rate)?),
                            requester: None,
                            transaction: None,
                        });
                        e.process(Proc::Periodic((e.periodic.len() - 1) as u32));
                    }
                    for poll in sc.polls.iter().filter(|p| p.source == node.name) {
                        let slave = lookup(&poll.slave)?;
                        let NodeKind::ModbusSlave { mac, unit } = &sc.nodes[slave].kind else {
                            return Err(format!(
                                "poll {} -> {}: not a Modbus slave",
                                poll.source, poll.slave
                            ));
                        };
                        let delay_ns = tb::ms_to_ns(poll.delay_ms)?;
                        let slave_mac = intern(mac);
                        let s = e.slave_of[&slave_mac];
                        let transaction = if poll.one_in_flight {
                            e.transactions.push(Transaction {
                                adapter: a,
                                slave: s,
                                timeout_ns: poll.timeout_ms.map(tb::ms_to_ns).transpose()?,
                                sent: 0,
                                sent_at: 0,
                                outstanding: false,
                                completed_at: None,
                            });
                            let t = (e.transactions.len() - 1) as u32;
                            e.adapters[a as usize].transactions.push((slave_mac, t));
                            Some(t)
                        } else {
                            None
                        };
                        e.periodic.push(Periodic {
                            frame: Frame {
                                modbus: Modbus {
                                    unit: *unit as i16,
                                    count: poll.count,
                                    response: false,
                                    tid: 0,
                                },
                                ..Frame::new(
                                    e.adapters[a as usize].mac,
                                    slave_mac,
                                    MODBUS_REQUEST_BYTES,
                                    0,
                                )
                            },
                            chan,
                            delay_ns,
                            delayed: false,
                            instants: Instants::new(delay_ns, tb::interval_ns(poll.rate)?),
                            requester: Some((a, s)),
                            transaction,
                        });
                        e.process(Proc::Periodic((e.periodic.len() - 1) as u32));
                    }
                }
            }
            NodeKind::ModbusSlave { .. } => {
                let w = role[i];
                if let Some(chan) = e.slaves[w].out {
                    e.process(Proc::Slave(w as u32));
                    let src = e.slaves[w].mac;
                    add_flows(&mut e, sc, &node.name, src, chan, &station_mac, &lookup)?;
                }
            }
            NodeKind::MqttBroker { .. } => {
                let b = role[i];
                if e.brokers[b].out.is_some() {
                    let mut routes = vec![Vec::new(); topics.len()];
                    for route in sc.routes.iter().filter(|r| r.broker == node.name) {
                        let mut subs = Vec::new();
                        for name in &route.subscribers {
                            let sub = lookup(name)?;
                            let mac = mac_of(sub).ok_or("a route goes to adapters")?;
                            subs.push(intern(&mac));
                        }
                        routes[topics[route.topic.as_str()] as usize] = subs;
                    }
                    e.brokers[b].routes = routes;
                    e.process(Proc::Broker(b as u32));
                    let (src, chan) = (e.brokers[b].mac, e.brokers[b].out.unwrap());
                    add_flows(&mut e, sc, &node.name, src, chan, &station_mac, &lookup)?;
                }
            }
            NodeKind::ProfinetDevice { .. } => {
                let d = role[i];
                e.process(Proc::PnDeviceRx(d as u32));
                if let Some(chan) = e.devices[d].out {
                    let src = e.devices[d].mac;
                    add_flows(&mut e, sc, &node.name, src, chan, &station_mac, &lookup)?;
                }
            }
            NodeKind::Switch { .. } => {
                let w = role[i];
                e.switches[w].mac_table = static_macs[i].clone();
                e.process(Proc::Switch(w as u32));
            }
        }
    }

    // metric samplers, in observation order
    for obs in &sc.observations {
        let target = lookup(&obs.target)?;
        let key = format!("{}:{}", obs.metric.name(), sc.nodes[target].name);
        let out = match e.out.iter().position(|(k, _)| *k == key) {
            Some(i) => i,
            None => {
                e.out.push((key, Vec::new()));
                e.out.len() - 1
            }
        };
        let adapter = is_adapter(target);
        let slave = match sc.nodes[target].kind {
            NodeKind::ModbusSlave { .. } => Some(role[target] as u32),
            _ => None,
        };
        // an end station's single link out
        let out_of = |e: &Engine| match sc.nodes[target].kind {
            NodeKind::Adapter { .. } => e.adapters[role[target]].out,
            NodeKind::ModbusSlave { .. } => e.slaves[role[target]].out,
            NodeKind::MqttBroker { .. } => e.brokers[role[target]].out,
            NodeKind::ProfinetDevice { .. } => e.devices[role[target]].out,
            NodeKind::Switch { .. } => None,
        };
        let what = match obs.metric {
            Metric::Throughput if adapter => Some(Sampled::Throughput(role[target] as u32)),
            Metric::Latency if adapter => {
                e.windows.push(Vec::new());
                let w = (e.windows.len() - 1) as u32;
                e.adapters[role[target]].latency_windows.push(w);
                Some(Sampled::Latency(w))
            }
            Metric::QueueDepth if is_switch(target) => {
                Some(Sampled::QueueDepth(role[target] as u32))
            }
            Metric::ModbusLatency if adapter || slave.is_some() => {
                // the window mean of round-trip times, as latency
                e.windows.push(Vec::new());
                let w = (e.windows.len() - 1) as u32;
                match slave {
                    Some(s) => e.slaves[s as usize].modbus_windows.push(w),
                    None => e.adapters[role[target]].modbus_windows.push(w),
                }
                Some(Sampled::Latency(w))
            }
            Metric::Utilization if !in_channels[target].is_empty() => {
                let chans = in_channels[target].clone();
                let mut speed = Q::int(0);
                for c in &chans {
                    speed = speed.checked_add(speeds[*c as usize])?;
                }
                Some(Sampled::Utilization(chans, speed))
            }
            Metric::PnLatency | Metric::PnJitter if pn_of[target].is_some() => {
                let c = &mut e.pn_consumers[pn_of[target].unwrap() as usize];
                let w = e.windows.len() as u32;
                e.windows.push(Vec::new());
                if obs.metric == Metric::PnLatency {
                    c.latency_windows.push(w);
                    Some(Sampled::Latency(w))
                } else {
                    c.jitter_windows.push(w);
                    Some(Sampled::Jitter(w))
                }
            }
            Metric::PnWatchdogExpiries if pn_of[target].is_some() => {
                Some(Sampled::WatchdogExpiries(pn_of[target].unwrap()))
            }
            Metric::BrokerQueue if matches!(sc.nodes[target].kind, NodeKind::MqttBroker { .. }) => {
                Some(Sampled::BrokerQueue(inbound[target]))
            }
            Metric::BytesReceived if adapter => Some(Sampled::BytesReceived(role[target] as u32)),
            Metric::BytesSent => out_of(&e).map(Sampled::BytesSent),
            Metric::Drops
            | Metric::DropsTotal
            | Metric::IngressDropsTotal
            | Metric::EgressDropsTotal
                if is_switch(target) =>
            {
                let w = role[target] as u32;
                let chans = e.switches[w as usize].out.iter().map(|(_, c)| *c).collect();
                let which = match obs.metric {
                    Metric::IngressDropsTotal => Drops::Ingress,
                    Metric::EgressDropsTotal => Drops::Egress,
                    _ => Drops::All,
                };
                Some(Sampled::Drops(chans, w, which, obs.metric == Metric::Drops))
            }
            Metric::FramesSent => out_of(&e).map(Sampled::FramesSent),
            Metric::PoolOccupancy if is_switch(target) => {
                Some(Sampled::PoolOccupancy(role[target] as u32))
            }
            Metric::IngressDropEpisodesTotal
            | Metric::EgressDropEpisodesTotal
            | Metric::IngressDropTimeTotal
            | Metric::EgressDropTimeTotal
                if is_switch(target) =>
            {
                let w = role[target] as u32;
                let chans = e.switches[w as usize].out.iter().map(|(_, c)| *c).collect();
                let ingress = matches!(
                    obs.metric,
                    Metric::IngressDropEpisodesTotal | Metric::IngressDropTimeTotal
                );
                let time = matches!(
                    obs.metric,
                    Metric::IngressDropTimeTotal | Metric::EgressDropTimeTotal
                );
                Some(Sampled::Episodes(chans, w, ingress, time))
            }
            Metric::FramesReceived if adapter => Some(Sampled::FramesReceived(role[target] as u32)),
            Metric::ModbusUnanswered if adapter => {
                Some(Sampled::ModbusUnanswered(role[target] as u32))
            }
            Metric::ModbusTimeouts if adapter => Some(Sampled::ModbusTimeouts(role[target] as u32)),
            Metric::ModbusSkipped if adapter => Some(Sampled::ModbusSkipped(role[target] as u32)),
            Metric::ModbusUnanswered => slave.map(Sampled::SlaveUnanswered),
            Metric::ModbusTimeouts => slave.map(Sampled::SlaveTimeouts),
            Metric::ModbusSkipped => slave.map(Sampled::SlaveSkipped),
            _ => None,
        };
        if let Some(what) = what {
            let ticks = Instants::new(0, tb::period_ns(obs.every_ms)?);
            e.samplers.push(Sampler {
                what,
                out,
                ticks,
                last_t: 0,
                last: 0,
            });
            e.process(Proc::Sampler((e.samplers.len() - 1) as u32));
        }
    }

    let started = std::time::Instant::now();
    while let Some(&Reverse((time, _, _, ev))) = e.heap.peek() {
        if time >= duration_ns {
            break; // SimPy's `until` event: URGENT at t = duration
        }
        e.heap.pop();
        e.now = time;
        e.events += 1;
        match ev {
            Ev::Init(p) => e.start(p),
            Ev::Timeout(p) => e.on_timeout(p),
            Ev::InFlight(c, f) => e.on_in_flight(c, f),
            Ev::Put(s) => e.on_put(s),
            Ev::Get(s, p, f) => e.on_get(s, p, f),
            Ev::Arrival(r, round) => e.on_arrival(r, round),
            Ev::Watchdog(r, round) => e.on_watchdog(r, round),
            Ev::Condition(r, round) => e.on_condition(r, round),
            Ev::PollTimeout(t, tid) => e.on_poll_timeout(t, tid),
        }
    }
    Ok(Outcome {
        metrics: e.out,
        events: e.events + 1,
        wall_s: started.elapsed().as_secs_f64(),
    })
}
