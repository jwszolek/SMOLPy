//! Reads Scenario IR JSON, format "smol-ir" version 2 (ADR-0007, ADR-0010),
//! with the optional switch buffers of ADR-0011, the optional `outstanding`
//! and `timeout_ms` of polls (ADR-0012), the ingress accounts and limits in
//! frames of ADR-0013, the cells and resume levels of ADR-0014, and the
//! shared pool, reserves and drains of ADR-0015.
//!
//! The scenario has already been validated by `smolpy.model.validate`; this
//! reader is strict about structure (unknown fields, kinds and versions are
//! errors) and rejects everything the engine does not support yet, naming
//! it. Nothing is skipped silently.

use crate::rng::Batch;
use crate::timebase::Q;
use serde_json::{Map, Value};

pub const FORMAT: &str = "smol-ir";
pub const VERSION: u64 = 2;

#[derive(Debug, Clone)]
pub enum NodeKind {
    Adapter {
        mac: String,
    },
    ModbusSlave {
        mac: String,
        unit: u8,
    },
    MqttBroker {
        mac: String,
    },
    ProfinetDevice {
        mac: String,
    },
    /// `buffer`: the egress limit of each port, `ingress` its ingress limit
    /// (ADR-0011, ADR-0013); None is unbounded. `cell` is the cell size of
    /// limits in cells, and each resume level is in its limit's unit
    /// (ADR-0014).
    Switch {
        fifo: bool,
        topology: bool,
        buffer: Option<Limit>,
        ingress: Option<Limit>,
        cell: Option<u64>,
        buffer_resume: Option<u64>,
        ingress_resume: Option<u64>,
        /// a pool shared by the ingress ports beyond their reserves, and the
        /// drain that ends an ingress drop state, in the ingress unit
        /// (ADR-0015)
        pool: Option<Limit>,
        reserve: Option<u64>,
        drain: Option<u64>,
        /// one drop state for the whole switch, `ingress_drop: "switch"`
        /// (ADR-0016)
        switch_drop: bool,
        /// its drain counted net, `ingress_drain_count: "net"`: the fall of
        /// the switch's ingress occupancy since the entry (ADR-0017)
        drain_net: bool,
        /// the protection of a light port (ADR-0017 Part 2): the pool's use
        /// counted from the total occupancy, `ingress_pool_count: "total"`;
        /// the dynamic threshold α; the level above which a port is marked at
        /// the state's entry, in the ingress unit
        pool_total: bool,
        alpha: Option<f64>,
        mark: Option<u64>,
    },
}

/// A switch buffer limit, in frame bytes, frames or cells (ADR-0013,
/// ADR-0014).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Limit {
    Bytes(u64),
    Frames(u64),
    Cells(u64),
}

#[derive(Debug, Clone)]
pub struct Node {
    pub name: String,
    pub kind: NodeKind,
}

#[derive(Debug, Clone)]
pub struct Link {
    pub a: String,
    pub b: String,
    pub speed_mbps: Q,
    pub length_m: Q,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Pattern {
    Constant,
    Poisson,
    Bursty,
    Batch(Batch),
}

/// A flow's frame size: fixed (untagged, FCS included) or IMIX per frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Size {
    Fixed(u64),
    Imix,
}

#[derive(Debug, Clone)]
pub struct Flow {
    pub source: String,
    pub destination: String,
    pub rate: Q,
    pub size: Size,
    pub pattern: Pattern,
    pub delay_ms: Q,
    pub priority: u8,
}

/// Modbus TCP polling (FC 03): `count` registers at `rate` polls per second.
#[derive(Debug, Clone)]
pub struct Poll {
    pub source: String,
    pub slave: String,
    pub count: u16,
    pub rate: Q,
    pub delay_ms: Q,
    /// one request in flight (`outstanding=1`), or open loop (ADR-0012)
    pub one_in_flight: bool,
    /// the master's response timeout, with one request in flight
    pub timeout_ms: Option<Q>,
}

/// MQTT PUBLISH messages to a broker at `rate` messages per second.
#[derive(Debug, Clone)]
pub struct Publish {
    pub source: String,
    pub broker: String,
    pub topic: String,
    pub rate: Q,
    pub payload: u64,
    pub qos: u8,
    pub delay_ms: Q,
}

/// A broker's fan-out of one topic to its subscribers.
#[derive(Debug, Clone)]
pub struct Route {
    pub broker: String,
    pub topic: String,
    pub subscribers: Vec<String>,
}

/// A PROFINET RT application relationship: cyclic frames both ways, with
/// `inputs` bytes from the device and `outputs` bytes to it; the watchdog is
/// `hold` cycles.
#[derive(Debug, Clone)]
pub struct ProfinetAr {
    pub controller: String,
    pub device: String,
    pub cycle_ms: Q,
    pub inputs: u64,
    pub outputs: u64,
    pub hold: u64,
    pub delay_ms: Q,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Metric {
    Throughput,
    Latency,
    BytesSent,
    BytesReceived,
    QueueDepth,
    Utilization,
    ModbusLatency,
    BrokerQueue,
    PnLatency,
    PnJitter,
    PnWatchdogExpiries,
    Drops,
    DropsTotal,
    FramesSent,
    FramesReceived,
    ModbusUnanswered,
    ModbusTimeouts,
    ModbusSkipped,
    IngressDropsTotal,
    EgressDropsTotal,
    IngressDropEpisodesTotal,
    EgressDropEpisodesTotal,
    IngressDropTimeTotal,
    EgressDropTimeTotal,
    PoolOccupancy,
}

impl Metric {
    pub const ALL: [(&'static str, Metric); 25] = [
        ("throughput", Metric::Throughput),
        ("latency", Metric::Latency),
        ("bytes_sent", Metric::BytesSent),
        ("bytes_received", Metric::BytesReceived),
        ("queue_depth", Metric::QueueDepth),
        ("utilization", Metric::Utilization),
        ("modbus_latency", Metric::ModbusLatency),
        ("broker_queue", Metric::BrokerQueue),
        ("pn_latency", Metric::PnLatency),
        ("pn_jitter", Metric::PnJitter),
        ("pn_watchdog_expiries", Metric::PnWatchdogExpiries),
        ("drops", Metric::Drops),
        ("drops_total", Metric::DropsTotal),
        ("frames_sent", Metric::FramesSent),
        ("frames_received", Metric::FramesReceived),
        ("modbus_unanswered", Metric::ModbusUnanswered),
        ("modbus_timeouts", Metric::ModbusTimeouts),
        ("modbus_skipped", Metric::ModbusSkipped),
        ("ingress_drops_total", Metric::IngressDropsTotal),
        ("egress_drops_total", Metric::EgressDropsTotal),
        (
            "ingress_drop_episodes_total",
            Metric::IngressDropEpisodesTotal,
        ),
        (
            "egress_drop_episodes_total",
            Metric::EgressDropEpisodesTotal,
        ),
        ("ingress_drop_time_total", Metric::IngressDropTimeTotal),
        ("egress_drop_time_total", Metric::EgressDropTimeTotal),
        ("pool_occupancy", Metric::PoolOccupancy),
    ];

    pub fn name(self) -> &'static str {
        Metric::ALL.iter().find(|(_, m)| *m == self).unwrap().0
    }
}

#[derive(Debug, Clone)]
pub struct Observation {
    pub metric: Metric,
    pub target: String,
    pub every_ms: Q,
}

#[derive(Debug, Clone)]
pub struct Scenario {
    pub name: String,
    pub nodes: Vec<Node>,
    pub links: Vec<Link>,
    pub flows: Vec<Flow>,
    pub polls: Vec<Poll>,
    pub publishes: Vec<Publish>,
    pub routes: Vec<Route>,
    pub ars: Vec<ProfinetAr>,
    pub observations: Vec<Observation>,
    pub duration_ms: Q,
    pub seed: u64,
}

/// What the engine supports, for `smolpy.backends.Capabilities`.
pub struct Capabilities {
    pub nodes: &'static [&'static str],
    pub traffic: &'static [&'static str],
    pub patterns: &'static [&'static str],
    pub metrics: &'static [&'static str],
    pub features: &'static [&'static str],
    pub notes: &'static [(&'static str, &'static str)],
}

pub const CAPABILITIES: Capabilities = Capabilities {
    nodes: &[
        "adapter",
        "switch",
        "modbus_slave",
        "mqtt_broker",
        "profinet_device",
    ],
    traffic: &["flow", "poll", "publish", "route", "profinet_ar"],
    patterns: &["constant", "poisson", "bursty", "batch"],
    metrics: &[
        "throughput",
        "latency",
        "bytes_sent",
        "bytes_received",
        "queue_depth",
        "utilization",
        "modbus_latency",
        "broker_queue",
        "pn_latency",
        "pn_jitter",
        "pn_watchdog_expiries",
        "drops",
        "drops_total",
        "frames_sent",
        "frames_received",
        "modbus_unanswered",
        "modbus_timeouts",
        "modbus_skipped",
        "ingress_drops_total",
        "egress_drops_total",
        "ingress_drop_episodes_total",
        "egress_drop_episodes_total",
        "ingress_drop_time_total",
        "egress_drop_time_total",
        "pool_occupancy",
    ],
    features: &[
        "imix",
        "bounded_buffer",
        "modbus_outstanding",
        // ADR-0013
        "ingress_buffer",
        "buffer_frames",
        "station_flows",
        "slave_metrics",
        // ADR-0014
        "buffer_cells",
        "buffer_hysteresis",
        // ADR-0015
        "buffer_pool",
        "drain_resume",
        // ADR-0016
        "switch_drop_state",
        // ADR-0017
        "drain_net",
        "light_port",
    ],
    notes: &[],
};

type Res<T> = Result<T, String>;

fn unsupported(what: &str) -> String {
    format!("the rust backend does not support {what} yet")
}

/// An object whose fields are taken one by one; leftovers are an error.
struct Fields<'a> {
    what: String,
    map: Map<String, Value>,
    _marker: std::marker::PhantomData<&'a ()>,
}

impl<'a> Fields<'a> {
    fn new(value: Value, what: impl Into<String>) -> Res<Fields<'a>> {
        let what = what.into();
        match value {
            Value::Object(map) => Ok(Fields {
                what,
                map,
                _marker: Default::default(),
            }),
            _ => Err(format!("{what}: expected an object")),
        }
    }

    fn take(&mut self, key: &str) -> Res<Value> {
        self.map
            .remove(key)
            .ok_or_else(|| format!("{}: missing field {key:?}", self.what))
    }

    fn take_or(&mut self, key: &str) -> Option<Value> {
        self.map.remove(key)
    }

    fn string(&mut self, key: &str) -> Res<String> {
        match self.take(key)? {
            Value::String(s) => Ok(s),
            other => Err(format!(
                "{}: {key} must be a string, got {other}",
                self.what
            )),
        }
    }

    fn number(&mut self, key: &str) -> Res<Q> {
        let what = self.what.clone();
        number(&self.take(key)?, &format!("{what}: {key}"))
    }

    fn integer(&mut self, key: &str) -> Res<u64> {
        match self.take(key)? {
            Value::Number(n) => n
                .as_u64()
                .ok_or_else(|| format!("{}: {key} must be a non-negative integer", self.what)),
            other => Err(format!(
                "{}: {key} must be an integer, got {other}",
                self.what
            )),
        }
    }

    fn done(self) -> Res<()> {
        match self.map.keys().next() {
            Some(key) => Err(format!("{}: unknown field {key:?}", self.what)),
            None => Ok(()),
        }
    }
}

fn number(value: &Value, what: &str) -> Res<Q> {
    match value {
        // arbitrary_precision keeps the number exactly as written
        Value::Number(n) => Q::parse_decimal(&n.to_string()).map_err(|e| format!("{what}: {e}")),
        other => Err(format!("{what} must be a number, got {other}")),
    }
}

pub fn parse(text: &str) -> Res<Scenario> {
    let value: Value = serde_json::from_str(text).map_err(|e| format!("invalid JSON: {e}"))?;
    let mut top = Fields::new(value, "scenario")?;
    let format = top.string("format")?;
    if format != FORMAT {
        return Err(format!("not a {FORMAT} document (format={format:?})"));
    }
    let version = top.integer("version")?;
    if version != VERSION {
        return Err(format!(
            "unsupported {FORMAT} version {version}, expected {VERSION}"
        ));
    }
    let name = top.string("name")?;

    let mut nodes = Vec::new();
    for (i, v) in array(top.take_or("nodes"), "nodes")?
        .into_iter()
        .enumerate()
    {
        nodes.push(parse_node(v, i)?);
    }
    let mut links = Vec::new();
    for (i, v) in array(top.take_or("links"), "links")?
        .into_iter()
        .enumerate()
    {
        let mut f = Fields::new(v, format!("link {i}"))?;
        let link = Link {
            a: f.string("a")?,
            b: f.string("b")?,
            speed_mbps: f.number("speed_mbps")?,
            length_m: f.number("length_m")?,
        };
        if let Some(duplex) = f.take_or("duplex") {
            if duplex != Value::Bool(true) {
                return Err(unsupported("half-duplex links"));
            }
        }
        f.done()?;
        links.push(link);
    }
    let (mut flows, mut polls, mut publishes, mut routes, mut ars) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for (i, v) in array(top.take_or("traffic"), "traffic")?
        .into_iter()
        .enumerate()
    {
        match parse_traffic(v, i)? {
            Traffic::Flow(flow) => flows.push(flow),
            Traffic::Poll(poll) => polls.push(poll),
            Traffic::Publish(publish) => publishes.push(publish),
            Traffic::Route(route) => routes.push(route),
            Traffic::ProfinetAr(ar) => ars.push(ar),
        }
    }
    let mut observations = Vec::new();
    for (i, v) in array(top.take_or("observations"), "observations")?
        .into_iter()
        .enumerate()
    {
        let mut f = Fields::new(v, format!("observation {i}"))?;
        let metric = f.string("metric")?;
        let metric = Metric::ALL
            .iter()
            .find(|(n, _)| *n == metric)
            .map(|(_, m)| *m)
            .ok_or_else(|| unsupported(&format!("metric {metric:?}")))?;
        let obs = Observation {
            metric,
            target: f.string("target")?,
            every_ms: f.number("every_ms")?,
        };
        f.done()?;
        observations.push(obs);
    }
    let run = match top.take_or("run") {
        None | Some(Value::Null) => {
            return Err(format!("scenario {name:?} has no run configuration"))
        }
        Some(v) => v,
    };
    let mut r = Fields::new(run, "run")?;
    let duration_ms = r.number("duration_ms")?;
    let seed = match r.take_or("seed") {
        None => 42,
        Some(Value::Number(n)) => n
            .as_u64()
            .ok_or("run: seed must be a non-negative integer")?,
        Some(other) => return Err(format!("run: seed must be an integer, got {other}")),
    };
    r.done()?;
    top.done()?;
    Ok(Scenario {
        name,
        nodes,
        links,
        flows,
        polls,
        publishes,
        routes,
        ars,
        observations,
        duration_ms,
        seed,
    })
}

fn array(value: Option<Value>, what: &str) -> Res<Vec<Value>> {
    match value {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => Ok(items),
        Some(other) => Err(format!("{what} must be a list, got {other}")),
    }
}

fn parse_node(value: Value, i: usize) -> Res<Node> {
    let mut f = Fields::new(value, format!("node {i}"))?;
    let kind = f.string("kind")?;
    let name = f.string("name")?;
    f.what = format!("{kind} {name}");
    let kind = match kind.as_str() {
        "adapter" => {
            f.string("ip")?;
            NodeKind::Adapter {
                mac: f.string("mac")?,
            }
        }
        "switch" => {
            f.integer("ports")?;
            // mode="cut-through" is accepted and behaves as store-and-forward
            // (Model Semantics, open issue 2), as in the reference engine.
            if let Some(mode) = f.take_or("mode") {
                if !matches!(mode.as_str(), Some("store-and-forward" | "cut-through")) {
                    return Err(format!("switch {name}: unknown mode {mode}"));
                }
            }
            let qos = f
                .take_or("qos")
                .map(|v| v.as_str().unwrap_or("?").to_string());
            let fifo = match qos.as_deref() {
                None | Some("strict") => false,
                Some("fifo") => true,
                Some(other) => return Err(format!("switch {name}: unknown qos {other:?}")),
            };
            let table = f
                .take_or("mac_table")
                .map(|v| v.as_str().unwrap_or("?").to_string());
            let topology = match table.as_deref() {
                None | Some("learning") => false,
                Some("topology") => true,
                Some(other) => return Err(format!("switch {name}: unknown mac_table {other:?}")),
            };
            let mut take = |field: &str, zero: bool| -> Res<Option<u64>> {
                match f.take_or(field) {
                    None | Some(Value::Null) => Ok(None),
                    Some(v) => v
                        .as_u64()
                        .filter(|b| zero || *b > 0)
                        .map(Some)
                        .ok_or_else(|| format!("switch {name}: {field} {v}")),
                }
            };
            let mut limit = |prefix: &str| -> Res<Option<Limit>> {
                let units = [
                    take(&format!("{prefix}_bytes"), false)?.map(Limit::Bytes),
                    take(&format!("{prefix}_frames"), false)?.map(Limit::Frames),
                    take(&format!("{prefix}_cells"), false)?.map(Limit::Cells),
                ];
                let mut given = units.into_iter().flatten();
                match (given.next(), given.next()) {
                    (Some(_), Some(_)) => Err(format!("switch {name}: {prefix} in two units")),
                    (l, _) => Ok(l),
                }
            };
            let buffer = limit("buffer")?;
            let ingress = limit("ingress_buffer")?;
            let pool = limit("pool")?;
            let cell = take("cell_bytes", false)?;
            let buffer_resume = take("buffer_resume", true)?;
            let ingress_resume = take("ingress_buffer_resume", true)?;
            let reserve = take("ingress_reserve", false)?;
            let drain = take("ingress_drain", false)?;
            let mark = take("ingress_mark", false)?;
            let scope = f
                .take_or("ingress_drop")
                .map(|v| v.as_str().unwrap_or("?").to_string());
            let switch_drop = match scope.as_deref() {
                None | Some("port") => false,
                Some("switch") => true,
                Some(other) => {
                    return Err(format!("switch {name}: unknown ingress_drop {other:?}"))
                }
            };
            let count = f
                .take_or("ingress_drain_count")
                .map(|v| v.as_str().unwrap_or("?").to_string());
            let drain_net = match count.as_deref() {
                None | Some("freed") => false,
                Some("net") => true,
                Some(other) => {
                    return Err(format!(
                        "switch {name}: unknown ingress_drain_count {other:?}"
                    ))
                }
            };
            let pool_count = f
                .take_or("ingress_pool_count")
                .map(|v| v.as_str().unwrap_or("?").to_string());
            let pool_total = match pool_count.as_deref() {
                None | Some("excess") => false,
                Some("total") => true,
                Some(other) => {
                    return Err(format!(
                        "switch {name}: unknown ingress_pool_count {other:?}"
                    ))
                }
            };
            let alpha = match f.take_or("ingress_alpha") {
                None | Some(Value::Null) => None,
                Some(v) => Some(
                    v.as_f64()
                        .filter(|a| a.is_finite() && *a > 0.0)
                        .ok_or_else(|| format!("switch {name}: ingress_alpha {v}"))?,
                ),
            };
            let cells = |l: Option<Limit>| matches!(l, Some(Limit::Cells(_)));
            if (cells(buffer) || cells(ingress) || cells(pool)) != cell.is_some() {
                return Err(format!(
                    "switch {name}: cell_bytes with limits in cells only"
                ));
            }
            for (limit, resume, what) in [
                (buffer, buffer_resume, "buffer_resume"),
                (ingress, ingress_resume, "ingress_buffer_resume"),
            ] {
                let below = match (limit, resume) {
                    (_, None) => true,
                    (None, Some(_)) => false,
                    (Some(Limit::Bytes(l) | Limit::Frames(l) | Limit::Cells(l)), Some(r)) => r < l,
                };
                if !below {
                    return Err(format!("switch {name}: {what} needs a limit above it"));
                }
            }
            let unit = |l: Option<Limit>| l.map(|l| std::mem::discriminant(&l));
            if ingress.is_some() && pool.is_some() && unit(ingress) != unit(pool) {
                return Err(format!(
                    "switch {name}: ingress_buffer and pool in two units"
                ));
            }
            if reserve.is_some() && pool.is_none() {
                return Err(format!("switch {name}: ingress_reserve needs pool"));
            }
            if drain.is_some()
                && (ingress_resume.is_some() || (pool.is_none() && ingress.is_none()))
            {
                return Err(format!(
                    "switch {name}: ingress_drain needs pool or ingress_buffer, alone"
                ));
            }
            if switch_drop && drain.is_none() {
                return Err(format!(
                    "switch {name}: ingress_drop=switch needs ingress_drain"
                ));
            }
            if count.is_some() && !switch_drop {
                return Err(format!(
                    "switch {name}: ingress_drain_count needs ingress_drop=switch"
                ));
            }
            if (pool_count.is_some() || alpha.is_some() || mark.is_some())
                && !(switch_drop && drain_net)
            {
                return Err(format!(
                    "switch {name}: ingress_pool_count, ingress_alpha and ingress_mark need \
                     ingress_drop=switch and ingress_drain_count=net"
                ));
            }
            if (pool_count.is_some() || alpha.is_some()) && pool.is_none() {
                return Err(format!(
                    "switch {name}: ingress_pool_count and ingress_alpha need pool"
                ));
            }
            NodeKind::Switch {
                fifo,
                topology,
                buffer,
                ingress,
                cell,
                buffer_resume,
                ingress_resume,
                pool,
                reserve,
                drain,
                switch_drop,
                drain_net,
                pool_total,
                alpha,
                mark,
            }
        }
        "modbus_slave" => {
            f.string("ip")?;
            let mac = f.string("mac")?;
            let unit = f.integer("unit_id")?;
            let unit =
                u8::try_from(unit).map_err(|_| format!("modbus_slave {name}: unit_id {unit}"))?;
            NodeKind::ModbusSlave { mac, unit }
        }
        "mqtt_broker" => {
            f.string("ip")?;
            NodeKind::MqttBroker {
                mac: f.string("mac")?,
            }
        }
        "profinet_device" => {
            f.string("ip")?;
            NodeKind::ProfinetDevice {
                mac: f.string("mac")?,
            }
        }
        other => return Err(format!("unknown node kind {other:?}")),
    };
    f.done()?;
    Ok(Node { name, kind })
}

enum Traffic {
    Flow(Flow),
    Poll(Poll),
    Publish(Publish),
    Route(Route),
    ProfinetAr(ProfinetAr),
}

fn parse_traffic(value: Value, i: usize) -> Res<Traffic> {
    let mut f = Fields::new(value, format!("traffic {i}"))?;
    let kind = f.string("kind")?;
    match kind.as_str() {
        "flow" => parse_flow(f).map(Traffic::Flow),
        "poll" => {
            let source = f.string("source")?;
            let slave = f.string("slave")?;
            f.what = format!("poll {source} -> {slave}");
            f.integer("register")?; // the register address does not change the frames
            let count = f.integer("count")?;
            let count = u16::try_from(count).map_err(|_| format!("poll: count {count}"))?;
            let rate = f.number("rate")?;
            let delay_ms = match f.take_or("delay_ms") {
                None => Q::int(0),
                Some(v) => number(&v, "poll: delay_ms")?,
            };
            let one_in_flight = match f.take_or("outstanding") {
                None | Some(Value::Null) => false,
                Some(v) if v.as_u64() == Some(1) => true,
                Some(v) => return Err(format!("poll: outstanding {v}")),
            };
            let timeout_ms = match f.take_or("timeout_ms") {
                None | Some(Value::Null) => None,
                Some(v) => Some(number(&v, "poll: timeout_ms")?),
            };
            if timeout_ms.is_some() && !one_in_flight {
                return Err("poll: timeout_ms needs outstanding 1".to_string());
            }
            f.done()?;
            Ok(Traffic::Poll(Poll {
                source,
                slave,
                count,
                rate,
                delay_ms,
                one_in_flight,
                timeout_ms,
            }))
        }
        "publish" => {
            let source = f.string("source")?;
            let broker = f.string("broker")?;
            f.what = format!("publish {source} -> {broker}");
            let topic = f.string("topic")?;
            let rate = f.number("rate")?;
            let payload = f.integer("payload")?;
            let qos = f.integer("qos")?;
            let qos = u8::try_from(qos)
                .ok()
                .filter(|q| *q <= 1)
                .ok_or(format!("publish: qos {qos}"))?;
            let delay_ms = match f.take_or("delay_ms") {
                None => Q::int(0),
                Some(v) => number(&v, "publish: delay_ms")?,
            };
            f.done()?;
            Ok(Traffic::Publish(Publish {
                source,
                broker,
                topic,
                rate,
                payload,
                qos,
                delay_ms,
            }))
        }
        "route" => {
            let broker = f.string("broker")?;
            let topic = f.string("topic")?;
            f.what = format!("route {broker} topic {topic:?}");
            let subscribers = match f.take("subscribers")? {
                Value::Array(items) => items
                    .into_iter()
                    .map(|v| match v {
                        Value::String(s) => Ok(s),
                        other => Err(format!("route: subscriber must be a name, got {other}")),
                    })
                    .collect::<Res<Vec<String>>>()?,
                other => return Err(format!("route: subscribers must be a list, got {other}")),
            };
            f.done()?;
            Ok(Traffic::Route(Route {
                broker,
                topic,
                subscribers,
            }))
        }
        "profinet_ar" => {
            let controller = f.string("controller")?;
            let device = f.string("device")?;
            f.what = format!("PROFINET AR {controller} -> {device}");
            let ar = ProfinetAr {
                controller,
                device,
                cycle_ms: f.number("cycle_ms")?,
                inputs: f.integer("inputs")?,
                outputs: f.integer("outputs")?,
                hold: match f.take_or("hold") {
                    None => 3,
                    Some(v) => v
                        .as_u64()
                        .filter(|h| *h >= 1)
                        .ok_or(format!("PROFINET AR: invalid hold {v}"))?,
                },
                delay_ms: match f.take_or("delay_ms") {
                    None => Q::int(0),
                    Some(v) => number(&v, "PROFINET AR: delay_ms")?,
                },
            };
            f.done()?;
            Ok(Traffic::ProfinetAr(ar))
        }
        other => Err(format!("unknown traffic kind {other:?}")),
    }
}

fn parse_flow(mut f: Fields<'_>) -> Res<Flow> {
    let source = f.string("source")?;
    let destination = f.string("destination")?;
    f.what = format!("flow {source} -> {destination}");
    let rate = f.number("rate")?;
    let size = match f.take("size")? {
        Value::String(s) if s == "imix" => Size::Imix,
        Value::Number(n) => Size::Fixed(n.as_u64().ok_or("flow: size must be an integer")?),
        other => return Err(format!("flow: invalid size {other}")),
    };
    let pattern = f.string("pattern")?;
    let delay_ms = match f.take_or("delay_ms") {
        None => Q::int(0),
        Some(v) => number(&v, "flow: delay_ms")?,
    };
    let batch = match f.take_or("batch") {
        None | Some(Value::Null) => None,
        Some(v) => Some(parse_batch(v)?),
    };
    let pattern = match (pattern.as_str(), batch) {
        ("constant", _) => Pattern::Constant,
        ("poisson", _) => Pattern::Poisson,
        ("bursty", _) => Pattern::Bursty,
        ("batch", Some(b)) => Pattern::Batch(b),
        ("batch", None) => return Err("flow: pattern 'batch' needs a batch size".to_string()),
        (other, _) => return Err(format!("flow: unknown pattern {other:?}")),
    };
    let priority = match f.take_or("priority") {
        None => 0,
        Some(Value::Number(n)) => n.as_u64().filter(|p| *p <= 7).ok_or("flow: priority 0-7")? as u8,
        Some(other) => return Err(format!("flow: invalid priority {other}")),
    };
    f.done()?;
    Ok(Flow {
        source,
        destination,
        rate,
        size,
        pattern,
        delay_ms,
        priority,
    })
}

/// `{"kind": "fixed" | "geom" | "pareto", "param": ...}`
fn parse_batch(value: Value) -> Res<Batch> {
    let mut f = Fields::new(value, "flow batch")?;
    let kind = f.string("kind")?;
    let param = match f.take("param")? {
        Value::Number(n) => n.as_f64().ok_or("flow batch: param must be a number")?,
        other => return Err(format!("flow batch: param must be a number, got {other}")),
    };
    f.done()?;
    match kind.as_str() {
        "fixed" if param >= 1.0 && param.fract() == 0.0 => Ok(Batch::Fixed(param as u64)),
        "geom" if param >= 1.0 => Ok(Batch::Geom(param)),
        "pareto" if param > 1.0 => Ok(Batch::Pareto(param)),
        _ => Err(format!(
            "flow batch: invalid {kind} batch with param {param}"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const B1: &str = r#"{"format": "smol-ir", "version": 2, "name": "bench-b1", "nodes": [{"kind": "switch", "name": "sw", "ports": 4, "mode": "store-and-forward", "qos": "strict", "mac_table": "learning"}, {"kind": "adapter", "name": "tx", "ip": "10.0.0.1", "mac": "02:00:0a:00:00:01"}, {"kind": "adapter", "name": "rx", "ip": "10.0.0.2", "mac": "02:00:0a:00:00:02"}], "links": [{"a": "tx", "b": "sw", "speed_mbps": 1000, "length_m": 5, "duplex": true}, {"a": "rx", "b": "sw", "speed_mbps": 1000, "length_m": 5, "duplex": true}], "traffic": [{"kind": "flow", "source": "tx", "destination": "rx", "rate": 50000.0, "size": 512, "pattern": "constant", "delay_ms": 0.0, "batch": null, "priority": 0}], "observations": [{"metric": "throughput", "target": "rx", "every_ms": 100}], "run": {"duration_ms": 20.0, "seed": 42}}"#;

    #[test]
    fn reads_b1() {
        let sc = parse(B1).unwrap();
        assert_eq!(sc.nodes.len(), 3);
        assert_eq!(sc.flows[0].rate, Q::int(50_000));
        assert_eq!(sc.duration_ms, Q::int(20));
    }

    #[test]
    fn reads_the_count_of_the_drain() {
        // ADR-0017: freed or net, only with a switch-wide drop state
        let switch = |extra: &str| {
            parse(&B1.replace(
                "\"learning\"",
                &format!("\"learning\", \"pool_frames\": 10, \"ingress_drain\": 2{extra}"),
            ))
        };
        let net = switch(", \"ingress_drop\": \"switch\", \"ingress_drain_count\": \"net\"");
        assert!(matches!(
            net.unwrap().nodes[0].kind,
            NodeKind::Switch {
                switch_drop: true,
                drain_net: true,
                ..
            }
        ));
        let freed = switch(", \"ingress_drop\": \"switch\", \"ingress_drain_count\": \"freed\"");
        assert!(matches!(
            freed.unwrap().nodes[0].kind,
            NodeKind::Switch {
                switch_drop: true,
                drain_net: false,
                ..
            }
        ));
        assert!(switch(", \"ingress_drain_count\": \"net\"")
            .unwrap_err()
            .contains("ingress_drain_count needs ingress_drop=switch"));
        assert!(
            switch(", \"ingress_drop\": \"port\", \"ingress_drain_count\": \"freed\"")
                .unwrap_err()
                .contains("ingress_drain_count needs ingress_drop=switch")
        );
        assert!(
            switch(", \"ingress_drop\": \"switch\", \"ingress_drain_count\": \"all\"")
                .unwrap_err()
                .contains("unknown ingress_drain_count")
        );
        assert!(CAPABILITIES.features.contains(&"drain_net"));
    }

    #[test]
    fn reads_the_protection_of_a_light_port() {
        // ADR-0017 Part 2: with a switch-wide drop state counted net
        let switch = |extra: &str| {
            parse(&B1.replace(
                "\"learning\"",
                &format!(
                    "\"learning\", \"pool_frames\": 10, \"ingress_drain\": 2, \
                     \"ingress_drop\": \"switch\"{extra}"
                ),
            ))
        };
        let all = switch(
            ", \"ingress_drain_count\": \"net\", \"ingress_pool_count\": \"total\", \
             \"ingress_alpha\": 24.0, \"ingress_mark\": 4",
        );
        assert!(matches!(
            all.unwrap().nodes[0].kind,
            NodeKind::Switch {
                pool_total: true,
                alpha: Some(24.0),
                mark: Some(4),
                ..
            }
        ));
        let excess =
            switch(", \"ingress_drain_count\": \"net\", \"ingress_pool_count\": \"excess\"");
        assert!(matches!(
            excess.unwrap().nodes[0].kind,
            NodeKind::Switch {
                pool_total: false,
                alpha: None,
                mark: None,
                ..
            }
        ));
        for extra in [
            ", \"ingress_mark\": 4",
            ", \"ingress_drain_count\": \"freed\", \"ingress_alpha\": 2",
        ] {
            assert!(switch(extra)
                .unwrap_err()
                .contains("need ingress_drop=switch and"));
        }
        let net = ", \"ingress_drain_count\": \"net\"";
        for (extra, error) in [
            (", \"ingress_alpha\": 0", "ingress_alpha 0"),
            (", \"ingress_alpha\": -1.5", "ingress_alpha -1.5"),
            (", \"ingress_mark\": 0", "ingress_mark 0"),
            (
                ", \"ingress_pool_count\": \"all\"",
                "unknown ingress_pool_count",
            ),
        ] {
            assert!(switch(&format!("{net}{extra}"))
                .unwrap_err()
                .contains(error));
        }
        let no_pool = parse(&B1.replace(
            "\"learning\"",
            "\"learning\", \"ingress_buffer_frames\": 10, \"ingress_drain\": 2, \
             \"ingress_drop\": \"switch\", \"ingress_drain_count\": \"net\", \"ingress_alpha\": 2",
        ));
        assert!(no_pool.unwrap_err().contains("need pool"));
        assert!(CAPABILITIES.features.contains(&"light_port"));
    }

    #[test]
    fn is_strict() {
        assert!(parse(&B1.replace("\"version\": 2", "\"version\": 1"))
            .unwrap_err()
            .contains("version 1"));
        let bounded =
            parse(&B1.replace("\"learning\"", "\"learning\", \"buffer_bytes\": 4000")).unwrap();
        assert!(matches!(
            bounded.nodes[0].kind,
            NodeKind::Switch {
                buffer: Some(Limit::Bytes(4000)),
                ingress: None,
                ..
            }
        ));
        let ingress = parse(&B1.replace(
            "\"learning\"",
            "\"learning\", \"buffer_frames\": 3, \"ingress_buffer_frames\": 60",
        ))
        .unwrap();
        assert!(matches!(
            ingress.nodes[0].kind,
            NodeKind::Switch {
                buffer: Some(Limit::Frames(3)),
                ingress: Some(Limit::Frames(60)),
                ..
            }
        ));
        assert!(parse(&B1.replace(
            "\"learning\"",
            "\"learning\", \"ingress_buffer_bytes\": 4000, \"ingress_buffer_frames\": 6",
        ))
        .unwrap_err()
        .contains("ingress_buffer in two units"));
        let cells = parse(&B1.replace(
            "\"learning\"",
            "\"learning\", \"cell_bytes\": 128, \"ingress_buffer_cells\": 740, \"ingress_buffer_resume\": 668",
        ))
        .unwrap();
        assert!(matches!(
            cells.nodes[0].kind,
            NodeKind::Switch {
                ingress: Some(Limit::Cells(740)),
                cell: Some(128),
                ingress_resume: Some(668),
                buffer_resume: None,
                ..
            }
        ));
        assert!(parse(&B1.replace(
            "\"learning\"",
            "\"learning\", \"buffer_frames\": 4, \"buffer_resume\": 4",
        ))
        .unwrap_err()
        .contains("buffer_resume needs a limit above it"));
        assert!(
            parse(&B1.replace("\"learning\"", "\"learning\", \"buffer_bytes\": -1"))
                .unwrap_err()
                .contains("buffer_bytes -1")
        );
        assert!(
            parse(&B1.replace("\"priority\": 0", "\"priority\": 0, \"x\": 1"))
                .unwrap_err()
                .contains("unknown field \"x\"")
        );
        let poll = |fields: &str| {
            let traffic = format!(
                "\"traffic\": [{{\"kind\": \"poll\", \"source\": \"tx\", \"slave\": \"rx\", \"register\": 0, \"count\": 10, \"rate\": 100, \"delay_ms\": 0.0{fields}}}, "
            );
            parse(&B1.replace("\"traffic\": [", &traffic))
        };
        let open = poll("").unwrap();
        assert!(!open.polls[0].one_in_flight && open.polls[0].timeout_ms.is_none());
        let closed = poll(", \"outstanding\": 1, \"timeout_ms\": 0.3").unwrap();
        assert!(closed.polls[0].one_in_flight);
        assert_eq!(closed.polls[0].timeout_ms, Some(Q::new(3, 10).unwrap()));
        assert!(poll(", \"outstanding\": 1").unwrap().polls[0].one_in_flight);
        assert!(poll(", \"outstanding\": 2")
            .unwrap_err()
            .contains("poll: outstanding 2"));
        assert!(poll(", \"timeout_ms\": 50.0")
            .unwrap_err()
            .contains("timeout_ms needs outstanding 1"));
        let poisson = parse(&B1.replace("\"constant\"", "\"poisson\"")).unwrap();
        assert_eq!(poisson.flows[0].pattern, Pattern::Poisson);
        let imix = parse(&B1.replace("\"size\": 512", "\"size\": \"imix\"")).unwrap();
        assert_eq!(imix.flows[0].size, Size::Imix);
        let batch = parse(&B1.replace("\"constant\"", "\"batch\"").replace(
            "\"batch\": null",
            "\"batch\": {\"kind\": \"pareto\", \"param\": 2.5}",
        ))
        .unwrap();
        assert_eq!(batch.flows[0].pattern, Pattern::Batch(Batch::Pareto(2.5)));
        assert!(parse(&B1.replace("\"constant\"", "\"batch\""))
            .unwrap_err()
            .contains("needs a batch size"));
        let mqtt = parse(&B1.replace(
            "\"traffic\": [",
            "\"traffic\": [{\"kind\": \"publish\", \"source\": \"tx\", \"broker\": \"rx\", \"topic\": \"hala/temp\\u00b0C\", \"rate\": 10, \"payload\": 20, \"qos\": 1, \"delay_ms\": 0.0}, {\"kind\": \"route\", \"broker\": \"rx\", \"topic\": \"t\", \"subscribers\": [\"tx\"]}, ",
        ))
        .unwrap();
        assert_eq!(mqtt.publishes[0].topic, "hala/temp°C");
        assert_eq!(mqtt.publishes[0].qos, 1);
        assert_eq!(mqtt.routes[0].subscribers, ["tx"]);
        assert!(parse(&B1.replace("\"throughput\"", "\"frame_loss\""))
            .unwrap_err()
            .contains("metric \"frame_loss\""));
    }
}
