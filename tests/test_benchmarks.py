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


# --- the testbed of ADR-0011 (benchmarks.loss) ---


def test_loss_scenario_offers_the_load_it_names() -> None:
    from benchmarks import loss
    from smolpy.model import Flow, SwitchNode
    from smolpy.model.frames import WIRE_OVERHEAD_BYTES

    sc = loss.scenario(0.996, duration_ms=100)
    (udp,) = [t for t in sc.traffic if isinstance(t, Flow)]
    assert udp.rate * (udp.size + WIRE_OVERHEAD_BYTES) * 8 / 100e6 == pytest.approx(0.996)
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert sw.buffer_bytes == 115_000


def test_loss_run_and_summary(tmp_path) -> None:
    from benchmarks import loss

    out = loss.run_loss(
        [0.9, 1.05], duration_ms=300, engines=["simpy"], out=tmp_path / "t.json", seeds=(1, 2)
    )
    doc = json.loads(out.read_text())
    assert doc["format"] == "smolpy-loss" and len(doc["runs"]) == 2 * 2 * 2
    for r in doc["runs"]:
        assert r["summary"]["rtt_max_ms"] < 10  # capped by the buffer
        assert r["rho"] < 1 or 0 < r["summary"]["dropped"] < 0.2
    table = loss.summarize_loss(out)
    assert table.count("| simpy |") == 4  # two buffers, two loads
    assert "Buffer 115000 B" in table and "Buffer 113900 B" in table


# --- the testbed of ADR-0012 (benchmarks.closed_loop) ---


def test_closed_loop_scenario_is_t1_with_one_request_in_flight() -> None:
    from benchmarks import closed_loop, loss
    from smolpy.model import Poll, SwitchNode

    sc = loss.scenario(0.99, 100, closed_loop.BUFFER, outstanding=1, timeout_ms=1_000)
    (poll,) = [t for t in sc.traffic if isinstance(t, Poll)]
    assert (poll.outstanding, poll.timeout_ms, poll.rate) == (1, 1_000, 100)
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert sw.buffer_bytes == 91_080  # 60 frames of 1 518 B
    open_loop = loss.scenario(0.99, 100)
    assert {o.metric for o in sc.observations} - {o.metric for o in open_loop.observations} == {
        "modbus_timeouts",
        "modbus_skipped",
    }


def test_closed_loop_run_and_summary(tmp_path) -> None:
    from benchmarks import closed_loop

    out = closed_loop.run_closed_loop(
        [1.05], duration_ms=2_000, engines=["simpy"], out=tmp_path / "t.json", seeds=(1, 2)
    )
    doc = json.loads(out.read_text())
    assert doc["format"] == "smolpy-closed-loop" and len(doc["runs"]) == 2 * 2
    for r in doc["runs"]:
        s = r["summary"]
        assert s["sent"] + s["skipped"] == 199  # every poll instant: 10, 20, ... 1 990 ms
        assert s["long_rtts_ms"] == []  # no TCP: a lost response is never late
        if r["timeout_ms"] is None:
            assert s["unanswered"] == 1 and s["timeouts"] == 0  # the first loss stops it
        else:
            assert s["unanswered"] - s["timeouts"] in (0, 1)
    table = closed_loop.summarize_closed_loop(out)
    assert "Timeout: none." in table and "Timeout: 1000 ms." in table
    assert table.count("| simpy |") == 2


def test_ingress_scenario_is_the_testbed_with_one_pi() -> None:
    """T3 (ADR-0013): the Pi is one Modbus slave that also sends the UDP
    stream; rho = 0.997 is the testbed's 8 100 frames/s, to 0.04 %."""
    from benchmarks import ingress
    from smolpy.model import Flow, Poll, SwitchNode

    sc = ingress.scenario(0.997, "ingress", 100)
    (flow,) = [t for t in sc.traffic if isinstance(t, Flow)]
    assert flow.source == "pi" and abs(flow.rate - 8_100) < 4
    polls = {
        t.slave: (t.rate, t.outstanding, t.timeout_ms) for t in sc.traffic if isinstance(t, Poll)
    }
    assert polls == {"pi": (100, 1, 1_000), "waveshare": (50, 1, 1_000)}
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert sw.ingress_limit == (60, "frames") and sw.egress_limit is None
    (sw,) = [n for n in ingress.scenario(0.997, "egress", 100).nodes if isinstance(n, SwitchNode)]
    assert sw.egress_limit == (91_080, "bytes") and sw.ingress_limit is None


def test_ingress_run_and_summary(tmp_path) -> None:
    from benchmarks import ingress

    out = ingress.run_ingress(
        [1.02], duration_ms=2_000, engines=["simpy"], out=tmp_path / "t.json", seeds=(1,)
    )
    doc = json.loads(out.read_text())
    assert doc["format"] == "smolpy-ingress" and len(doc["runs"]) == 2
    by_model = {r["model"]: r["summary"] for r in doc["runs"]}
    for s in by_model.values():
        assert s["pi"]["sent"] + s["pi"]["skipped"] == 199  # 10, 20, ... 1 990 ms
        assert s["waveshare"]["sent"] + s["waveshare"]["skipped"] == 99
    # with ingress accounts the Waveshare's port never fills
    assert by_model["ingress"]["waveshare"]["affected"] == 0
    assert by_model["ingress"]["egress_drops"] == 0 < by_model["ingress"]["ingress_drops"]
    assert by_model["egress"]["ingress_drops"] == 0 < by_model["egress"]["egress_drops"]
    table = ingress.summarize_ingress(out)
    assert table.count("| simpy |") == 2


def test_cells_scenario_is_the_testbed_in_cells() -> None:
    """T4 (ADR-0014): T3 with 740 cells of 128 B per ingress port resuming
    at 668, beside tail drop in cells and 60 frames."""
    from benchmarks import cells
    from smolpy.model import Flow, SwitchNode

    sc = cells.scenario("cells", duration_ms=100)
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert (sw.cell_bytes, sw.ingress_limit, sw.ingress_buffer_resume) == (128, (740, "cells"), 668)
    (flow,) = [t for t in sc.traffic if isinstance(t, Flow)]
    assert (flow.source, flow.rate, flow.pattern) == ("pi", 8_100, "poisson")
    batch = cells.scenario("frames", rate=7_500, duration_ms=100, batch=8)
    (flow,) = [t for t in batch.traffic if isinstance(t, Flow)]
    assert flow.pattern == "batch" and flow.batch.param == 8


def test_a_burst_fills_740_cells(tmp_path) -> None:
    """A burst of 64 B frames, one cell each, at 239 000 frames/s for 4 000
    frames: the port holds 740 cells, the frame in transmission and what
    drained, about 2 480."""
    from benchmarks import cells

    tail = cells.burst("cells-tail", 64, 4_000, 239_454)
    assert 740 + 2_400 < tail < 740 + 2_560
    assert cells.burst("frames", 64, 4_000, 239_454) < tail - 600  # 60 frames hold far less
    assert cells.burst("cells", 64, 40, 239_454) == 40  # under the limit nothing is lost


def test_cells_run_and_summary(tmp_path) -> None:
    from benchmarks import cells

    out = cells.run_cells(
        duration_ms=2_000,
        engines=["simpy"],
        out=tmp_path / "t.json",
        seeds=(1,),
        models=("cells", "cells-tail"),
        data=tmp_path / "missing",  # no burst CSVs here
    )
    doc = json.loads(out.read_text())
    assert doc["format"] == "smolpy-cells" and len(doc["runs"]) == 2 * 2
    for r in doc["runs"]:
        s = r["summary"]
        assert s["waveshare"]["affected"] == 0  # its port never fills
        assert s["episodes"] > 0 and s["run_lengths"]
        if r["model"] == "cells-tail":
            assert s["drop_time_ms"] == 0
    table = cells.summarize_cells(out)
    assert table.count("| simpy |") == 4


def test_pool_testbed_has_two_pis_and_the_waveshare() -> None:
    """ADR-0015's validation scenario: each Pi a Modbus slave with its
    flows, on ports 0 and 1, and the Waveshare."""
    from benchmarks import pool
    from smolpy.model import Flow, Poll, SwitchNode

    pis, mbps, poll_ms, registers, ws = pool.CASES["two 4050"]
    sc = pool.testbed(pool.switch(716, 54, 72), pis, 100, 1, mbps, poll_ms, registers, ws)
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert (sw.pool_cells, sw.ingress_reserve, sw.ingress_drain) == (716, 54, 72)
    assert [(t.source, t.rate) for t in sc.traffic if isinstance(t, Flow)] == [
        ("pi1", 4_050),
        ("pi2", 4_040),
    ]
    assert {t.slave for t in sc.traffic if isinstance(t, Poll)} == {"pi1", "pi2", "waveshare"}


def test_pool_burst_with_one_source_is_an_ingress_limit() -> None:
    """One source: a pool of P with a reserve of R holds what P + R does."""
    from benchmarks import pool

    pooled = pool.burst(pool.switch(716, 54, 72), 64, 4_000, 239_454, "simpy")
    single = pool.burst(pool.switch(770, 0, 72), 64, 4_000, 239_454, "simpy")
    assert pooled == single


def test_pool_validation_case_runs() -> None:
    from benchmarks import pool

    (row,) = pool.validate_case(
        pool.switch(716, 54, 72), "two 4050", 2_000, (1,), ["simpy"], lambda s: None
    )
    assert row["waveshare"]["affected"] == 0
    assert set(row["runs_by_port"]) <= {"0", "1"} and row["mean_run"] >= 1
    hit, total = row["overlap"]
    assert 0 <= hit <= total


def test_pool_hardware_numbers_are_computed_from_the_raw_files() -> None:
    from benchmarks import pool

    if not pool.DATA.is_dir():
        pytest.skip("the testbed's raw data is not here")
    hw = pool.hardware()
    (a, b) = hw["two 4050"]["udp"]
    assert 0.006 < a["loss"] < 0.007 and 3.1 < a["mean_run"] < 3.4
    assert hw["two 4050"]["waveshare"]["affected"] == 0
