//! The Python module `smolpy_engine_rs`: the Rust backend of
//! `smolpy.backends` (ADR-0008).
//!
//! - `CAPABILITIES`: what the engine supports, as `smolpy.backends.Capabilities`.
//! - `run(scenario_json)`: `(metrics, events, wall_s)`, with the GIL released
//!   for the whole run.
//! - `_ms_to_ns`, `_transmission_ns`, `_propagation_ns`, `_instants`, `_ratio`,
//!   `_stream_seed`: the time rules and stream seeds, for tests against Python.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use smol_engine::timebase::{self as tb, Instants, Q};

fn value_error(e: String) -> PyErr {
    PyValueError::new_err(e)
}

#[pyfunction]
fn run(py: Python<'_>, scenario_json: String) -> PyResult<(Py<PyDict>, u64, f64)> {
    let outcome = py
        .detach(move || smol_engine::run_json(&scenario_json))
        .map_err(value_error)?;
    let metrics = PyDict::new(py);
    for (key, samples) in outcome.metrics {
        metrics.set_item(key, PyList::new(py, samples)?)?;
    }
    Ok((metrics.unbind(), outcome.events, outcome.wall_s))
}

fn decimal(text: &str) -> PyResult<Q> {
    Q::parse_decimal(text).map_err(value_error)
}

#[pyfunction]
fn _ms_to_ns(ms: &str) -> PyResult<u64> {
    tb::ms_to_ns(decimal(ms)?).map_err(value_error)
}

#[pyfunction]
fn _transmission_ns(bytes: u64, speed_mbps: &str) -> PyResult<u64> {
    tb::transmission_ns(bytes, decimal(speed_mbps)?).map_err(value_error)
}

#[pyfunction]
fn _propagation_ns(length_m: &str) -> PyResult<u64> {
    tb::propagation_ns(decimal(length_m)?).map_err(value_error)
}

/// The first `count` instants of a source with `rate` events per second
/// starting at `start_ns`.
#[pyfunction]
fn _instants(start_ns: u64, rate: &str, count: usize) -> PyResult<Vec<u64>> {
    let period = tb::interval_ns(decimal(rate)?).map_err(value_error)?;
    Ok(Instants::new(start_ns, period).take(count).collect())
}

/// The seed of a traffic statement's random stream.
#[pyfunction]
fn _stream_seed(run_seed: u64, source_id: &str) -> u64 {
    smol_engine::rng::stream_seed(run_seed, source_id)
}

/// `n / d` rounded once; both given as decimal strings (Python ints).
#[pyfunction]
fn _ratio(n: &str, d: &str) -> PyResult<f64> {
    let parse = |s: &str| {
        s.parse::<u128>()
            .map_err(|e| PyValueError::new_err(e.to_string()))
    };
    Ok(tb::ratio_f64(parse(n)?, parse(d)?))
}

#[pymodule]
fn smolpy_engine_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    let caps = smol_engine::CAPABILITIES;
    let d = PyDict::new(py);
    d.set_item("nodes", PyList::new(py, caps.nodes)?)?;
    d.set_item("traffic", PyList::new(py, caps.traffic)?)?;
    d.set_item("patterns", PyList::new(py, caps.patterns)?)?;
    d.set_item("metrics", PyList::new(py, caps.metrics)?)?;
    d.set_item("features", PyList::new(py, caps.features)?)?;
    let notes = PyDict::new(py);
    for (kind, note) in caps.notes {
        notes.set_item(kind, note)?;
    }
    d.set_item("notes", notes)?;
    d.set_item("half_duplex", false)?;
    m.add("CAPABILITIES", d)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(run, m)?)?;
    m.add_function(wrap_pyfunction!(_ms_to_ns, m)?)?;
    m.add_function(wrap_pyfunction!(_transmission_ns, m)?)?;
    m.add_function(wrap_pyfunction!(_propagation_ns, m)?)?;
    m.add_function(wrap_pyfunction!(_instants, m)?)?;
    m.add_function(wrap_pyfunction!(_ratio, m)?)?;
    m.add_function(wrap_pyfunction!(_stream_seed, m)?)?;
    Ok(())
}
