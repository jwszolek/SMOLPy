//! Reads Scenario IR JSON, format "smol-ir" version 2 (ADR-0007, ADR-0010).
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
    Adapter { mac: String },
    Switch { fifo: bool, topology: bool },
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Metric {
    Throughput,
    Latency,
    BytesSent,
    BytesReceived,
    QueueDepth,
    Utilization,
}

impl Metric {
    pub const ALL: [(&'static str, Metric); 6] = [
        ("throughput", Metric::Throughput),
        ("latency", Metric::Latency),
        ("bytes_sent", Metric::BytesSent),
        ("bytes_received", Metric::BytesReceived),
        ("queue_depth", Metric::QueueDepth),
        ("utilization", Metric::Utilization),
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
    nodes: &["adapter", "switch"],
    traffic: &["flow"],
    patterns: &["constant", "poisson", "bursty", "batch"],
    metrics: &[
        "throughput",
        "latency",
        "bytes_sent",
        "bytes_received",
        "queue_depth",
        "utilization",
    ],
    features: &["imix"],
    notes: &[
        ("mqtt_broker", "planned for Phase 5"),
        ("modbus_slave", "planned for Phase 5"),
        ("profinet_device", "planned for Phase 5"),
        ("publish", "planned for Phase 5"),
        ("route", "planned for Phase 5"),
        ("poll", "planned for Phase 5"),
        ("profinet_ar", "planned for Phase 5"),
    ],
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
    let mut flows = Vec::new();
    for (i, v) in array(top.take_or("traffic"), "traffic")?
        .into_iter()
        .enumerate()
    {
        flows.push(parse_traffic(v, i)?);
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
            NodeKind::Switch { fifo, topology }
        }
        "mqtt_broker" | "modbus_slave" | "profinet_device" => {
            return Err(unsupported(&format!("{kind} nodes (Phase 5)")))
        }
        other => return Err(format!("unknown node kind {other:?}")),
    };
    f.done()?;
    Ok(Node { name, kind })
}

fn parse_traffic(value: Value, i: usize) -> Res<Flow> {
    let mut f = Fields::new(value, format!("traffic {i}"))?;
    let kind = f.string("kind")?;
    if kind != "flow" {
        return match kind.as_str() {
            "publish" | "route" | "poll" | "profinet_ar" => {
                Err(unsupported(&format!("{kind} traffic (Phase 5)")))
            }
            other => Err(format!("unknown traffic kind {other:?}")),
        };
    }
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
    fn is_strict() {
        assert!(parse(&B1.replace("\"version\": 2", "\"version\": 1"))
            .unwrap_err()
            .contains("version 1"));
        assert!(
            parse(&B1.replace("\"priority\": 0", "\"priority\": 0, \"x\": 1"))
                .unwrap_err()
                .contains("unknown field \"x\"")
        );
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
        assert!(parse(&B1.replace("\"throughput\"", "\"pn_jitter\""))
            .unwrap_err()
            .contains("metric \"pn_jitter\""));
    }
}
