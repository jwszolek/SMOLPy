//! SMOLPy's native discrete-event engine (ADR-0008): a second implementation
//! of the semantics contract (docs/model-semantics.md), with no Python
//! dependency. `smolpy-engine-rs` exposes it to Python.

pub mod frames;
pub mod scenario;
pub mod sim;
pub mod timebase;

pub use scenario::CAPABILITIES;
pub use sim::Outcome;

/// Read a Scenario IR JSON document and run it.
pub fn run_json(text: &str) -> Result<Outcome, String> {
    let scenario = scenario::parse(text)?;
    sim::run(&scenario)
}
