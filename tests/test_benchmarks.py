"""Sanity checks for the ADR-0006 benchmark harness (small sizes only)."""

from __future__ import annotations

import json

import pytest

from benchmarks import worker
from benchmarks.runner import parse_size, summarize
from benchmarks.scenarios import SCENARIOS
from smolpy.sim import engine


@pytest.fixture
def injected_frames(monkeypatch):
    """Count frames built by generators and endpoints. Deliveries reuse the
    frame and flooding copies it with copy.copy, neither calls __init__."""
    counts = {"created": 0}
    frame_init = engine._Frame.__init__

    def counting_init(self, *args, **kwargs):
        counts["created"] += 1
        frame_init(self, *args, **kwargs)

    monkeypatch.setattr(engine._Frame, "__init__", counting_init)
    return counts


# frames_expected is rate x duration; every periodic source is off by at most
# one frame at the run boundaries, so B3's 1 000 sources need a longer run.
@pytest.mark.parametrize("scenario, frames", [("B1", 20_000), ("B2", 20_000), ("B3", 50_000)])
def test_scenario_injects_the_expected_number_of_frames(scenario, frames, injected_frames) -> None:
    built = SCENARIOS[scenario](frames, False)
    built.network.simulate(duration=built.duration_ms)
    injected = injected_frames["created"]
    assert injected == pytest.approx(built.frames_expected, rel=0.03)


def test_worker_record_is_complete_and_reproducible() -> None:
    a = worker.measure("B1", 5_000, True, seed=1, profile=None, out=None)
    b = worker.measure("B1", 5_000, True, seed=1, profile=None, out=None)
    assert a["status"] == "ok"
    assert a["events"] > 0 and a["events_per_s"] > 0 and a["peak_rss_kb"] > 0
    assert a["output_hash"] == b["output_hash"]


def test_parse_size() -> None:
    assert parse_size("100k") == 100_000
    assert parse_size("10M") == 10_000_000
    assert parse_size("100M") == 100_000_000
    assert parse_size("2500") == 2_500


def test_summarize_marks_differing_outputs(tmp_path) -> None:
    run = {
        "scenario": "B1", "frames_target": 100_000, "metrics": "off", "status": "ok",
        "run_s": 1.0, "events": 10, "events_per_s": 10.0, "frames_per_s": 5.0, "peak_rss_kb": 2048,
    }  # fmt: skip
    path = tmp_path / "r.json"
    runs = [{**run, "output_hash": "a"}, {**run, "output_hash": "b"}]
    path.write_text(json.dumps({"runs": runs}))
    assert "DIFFERS" in summarize(path)


def test_b4_is_ten_thousand_nodes_starting_converged() -> None:
    built = SCENARIOS["B4"](100_000_000, False)
    switches = [n for n in built.network._nodes.values() if hasattr(n, "mac_table_mode")]
    assert len(built.network._nodes) == 1 + 200 + 200 * 50
    assert {s.mac_table_mode for s in switches} == {"topology"}
    assert built.frames_per_s == 200_000
    assert built.duration_ms == 500_000


# --- the load study (benchmarks.load) ---


def test_load_scenario_offers_the_load_it_names() -> None:
    """rho counts the wire bits of every frame sent across the uplink: the
    protocols' responses and messages, and the camera that tops them up."""
    from benchmarks import load
    from smolpy.model import Flow
    from smolpy.model.frames import WIRE_OVERHEAD_BYTES

    sc = load.scenario(1.2, duration_ms=100)
    (camera,) = [t for t in sc.traffic if isinstance(t, Flow)]
    camera_bits = camera.rate * (camera.size + WIRE_OVERHEAD_BYTES) * 8
    assert camera_bits / (load.UPLINK_MBPS * 1e6) + load.protocol_load() == pytest.approx(1.2)
    assert 0.12 < load.protocol_load() < 0.13
    with pytest.raises(ValueError, match="at least the protocols' own load"):
        load.scenario(0.1)


def test_load_run_and_summary(tmp_path) -> None:
    from benchmarks import load

    out = load.run_load(
        [0.5, 1.5], duration_ms=50, engines=["simpy", "simpy"], out=tmp_path / "l.json"
    )
    doc = json.loads(out.read_text())
    assert doc["format"] == "smolpy-load" and len(doc["runs"]) == 4
    table = load.summarize_load(out, "simpy", "simpy")
    assert table.count("(+0.0 %)") == 8  # a backend against itself, two loads
