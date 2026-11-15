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
//! - processes are created in SimPy's order: link transmitters, then node
//!   processes in node order, then metric samplers in observation order;
//! - the run stops before the first event at `t = duration`.

use crate::frames::{flow_frame_size, IFG_BYTES, PREAMBLE_SFD_BYTES, WIRE_OVERHEAD_BYTES};
use crate::rng::{stream_seed, Rng};
use crate::scenario::{Metric, NodeKind, Pattern, Scenario, Size};
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
}

#[derive(Clone, Copy)]
struct Frame {
    src: u32,
    dst: u32,
    size: u64,
    created: u64,
    priority: u8,
    in_port: i32,
}

enum Items {
    Fifo(VecDeque<u32>),
    /// SimPy's PriorityStore of PriorityItem((class, sequence), frame)
    Prio(BinaryHeap<Reverse<(i16, u64, u32)>>),
}

struct Store {
    items: Items,
    waiting: Option<u32>,
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
}

struct Adapter {
    mac: u32,
    inbound: u32,
    out: Option<u32>,
    bytes_received: u128,
    latency_windows: Vec<u32>,
}

struct Switch {
    inbound: u32,
    out: Vec<(i32, u32)>, // (port, channel), in port order: out[p].0 == p
    mac_table: HashMap<u32, i32>,
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
    flows: Vec<FlowGen>,
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

    fn get(&mut self, store: u32, proc: u32) {
        match self.stores[store as usize].pop() {
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

    fn enqueue(&mut self, chan: u32, f: u32) {
        let ch = &mut self.channels[chan as usize];
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
    }

    fn on_put(&mut self, store: u32) {
        let s = &mut self.stores[store as usize];
        if let Some(proc) = s.waiting {
            if let Some(f) = s.pop() {
                s.waiting = None;
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
                    for w in &ad.latency_windows {
                        self.windows[*w as usize].push(self.now - fr.created);
                    }
                }
                self.release(f);
                self.get(store, proc);
            }
            Proc::Switch(w) => {
                let fr = self.frames[f as usize];
                let sw = &mut self.switches[w as usize];
                sw.mac_table.insert(fr.src, fr.in_port);
                match sw.mac_table.get(&fr.dst).copied() {
                    None => {
                        // flood a copy to every port but the one it came from
                        let ports: Vec<(i32, u32)> = sw.out.clone();
                        for (port, ch) in ports {
                            if port != fr.in_port {
                                let copy = self.alloc(fr);
                                self.enqueue(ch, copy);
                            }
                        }
                        self.release(f);
                    }
                    Some(port) if port != fr.in_port => {
                        // ports are numbered 0, 1, ... in link order: out[port] is its channel
                        let ch = sw.out[port as usize].1;
                        self.enqueue(ch, f);
                    }
                    Some(_) => self.release(f), // hairpin: dropped
                }
                self.get(store, proc);
            }
            Proc::Flow(_) | Proc::Sampler(_) => unreachable!("these processes do not get"),
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
            Proc::AdapterRx(_) | Proc::Switch(_) => unreachable!("these processes do not time out"),
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
            src: fl.src,
            dst: fl.dst,
            size: flow_frame_size(size, fl.priority),
            created: self.now,
            priority: fl.priority,
            in_port: -1,
        };
        let chan = fl.chan;
        let f = self.alloc(frame);
        self.enqueue(chan, f);
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
        };
        let s = &mut self.samplers[i as usize];
        s.last_t = now;
        s.last = last;
        if let Some(v) = value {
            self.out[s.out].1.push((t_ms, v));
        }
    }
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
        flows: Vec::new(),
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
    for node in &sc.nodes {
        let s = e.store(Items::Fifo(VecDeque::new()));
        inbound.push(s);
        match &node.kind {
            NodeKind::Adapter { mac } => {
                let mac = intern(mac);
                e.adapters.push(Adapter {
                    mac,
                    inbound: s,
                    out: None,
                    bytes_received: 0,
                    latency_windows: Vec::new(),
                });
                role.push(e.adapters.len() - 1);
            }
            NodeKind::Switch { .. } => {
                e.switches.push(Switch {
                    inbound: s,
                    out: Vec::new(),
                    mac_table: HashMap::new(),
                });
                role.push(e.switches.len() - 1);
            }
        }
    }
    let is_switch = |i: usize| matches!(sc.nodes[i].kind, NodeKind::Switch { .. });
    let strict = |i: usize| !matches!(sc.nodes[i].kind, NodeKind::Switch { fifo: true, .. });
    let mac_of = |i: usize| match &sc.nodes[i].kind {
        NodeKind::Adapter { mac } => Some(mac.clone()),
        NodeKind::Switch { .. } => None,
    };

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
        if let Some(mac) = mac_of(a) {
            e.adapters[role[a]].out = Some(ch_ab);
            if is_switch(b) {
                static_macs[b].insert(intern(&mac), port_b);
            }
        }
        if let Some(mac) = mac_of(b) {
            e.adapters[role[b]].out = Some(ch_ba);
            if is_switch(a) {
                static_macs[a].insert(intern(&mac), port_a);
            }
        }
    }
    let _ = tx_procs;

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

    // node processes, in node order: an adapter's receiver, then its flows
    for (i, node) in sc.nodes.iter().enumerate() {
        match &node.kind {
            NodeKind::Adapter { .. } => {
                let a = role[i] as u32;
                e.process(Proc::AdapterRx(a));
                if let Some(chan) = e.adapters[a as usize].out {
                    for (n, flow) in sc.flows.iter().enumerate() {
                        if flow.source != node.name {
                            continue;
                        }
                        let dst = lookup(&flow.destination)?;
                        let dst_mac = mac_of(dst).ok_or("a flow goes to an adapter")?;
                        let delay_ns = tb::ms_to_ns(flow.delay_ms)?;
                        let period = tb::interval_ns(flow.rate)?;
                        // source id: earlier flows between the same two ends
                        let k = sc.flows[..n]
                            .iter()
                            .filter(|f| {
                                f.source == flow.source && f.destination == flow.destination
                            })
                            .count();
                        let id = format!("flow:{}->{}#{k}", flow.source, flow.destination);
                        e.flows.push(FlowGen {
                            src: e.adapters[a as usize].mac,
                            dst: intern(&dst_mac),
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
        let adapter = mac_of(target).is_some();
        let what = match obs.metric {
            Metric::Throughput if adapter => Some(Sampled::Throughput(role[target] as u32)),
            Metric::Latency if adapter => {
                e.windows.push(Vec::new());
                let w = (e.windows.len() - 1) as u32;
                e.adapters[role[target]].latency_windows.push(w);
                Some(Sampled::Latency(w))
            }
            Metric::QueueDepth if !adapter => Some(Sampled::QueueDepth(role[target] as u32)),
            Metric::Utilization if !in_channels[target].is_empty() => {
                let chans = in_channels[target].clone();
                let mut speed = Q::int(0);
                for c in &chans {
                    speed = speed.checked_add(speeds[*c as usize])?;
                }
                Some(Sampled::Utilization(chans, speed))
            }
            Metric::BytesReceived if adapter => Some(Sampled::BytesReceived(role[target] as u32)),
            Metric::BytesSent if adapter => e.adapters[role[target]].out.map(Sampled::BytesSent),
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
        }
    }
    Ok(Outcome {
        metrics: e.out,
        events: e.events + 1,
        wall_s: started.elapsed().as_secs_f64(),
    })
}
