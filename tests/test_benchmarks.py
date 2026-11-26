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


def test_switch_drop_measure_of_a_run() -> None:
    """ADR-0016's measure: a run's interval is between the admitted frames
    on either side of it; runs overlap if their intervals meet, and an
    episode merges the overlapping ones of both streams."""
    from benchmarks import switch_drop as sd

    a = sd.runs_from_decisions(
        [(0, True), (1, False), (2, False), (3, True), (8, False), (9, True)]
    )
    assert a == [(0, 3, 2), (3, 9, 1)]
    a = [(0.0, 3.0, 2), (5.0, 6.0, 1), (9.5, 11.0, 1)]
    b = [(0.0, 1.0, 3), (2.5, 4.0, 1), (10.0, 11.0, 1)]
    s = sd.overlap_stats(a, b)
    # a's first and last meet one of b's, a's second none; each of b's one of a's
    assert (s["overlapping"], s["runs_total"]) == (2 + 3, 6)
    assert (s["episodes"], s["episodes_both"]) == (3, 2)
    assert s["mean_run"] == [4 / 3, 5 / 3] and s["single"] == 4 / 6


def test_switch_drop_testbed_has_the_switch_scope() -> None:
    from benchmarks import pool
    from benchmarks import switch_drop as sd
    from smolpy.model import SwitchNode

    pis, mbps, poll_ms, registers, ws, seconds = sd.CASES["10Mb two 405"]
    assert (mbps, poll_ms, registers, seconds) == (10, 100, 4, 900)
    sc = pool.testbed(sd.switch(716, 54, 72), pis, 100, 1, mbps, poll_ms, registers, ws)
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert (sw.pool_cells, sw.ingress_reserve, sw.ingress_drain, sw.ingress_drop) == (
        716,
        54,
        72,
        "switch",
    )


def test_switch_drop_validation_case_runs() -> None:
    from benchmarks import switch_drop as sd

    sd.CASES["short"] = (*sd.CASES["10Mb two 405"][:5], 60)
    try:
        (row,) = sd.validate_case(sd.switch(716, 54, 72), "short", 0, (1,), ["simpy"], lambda s: 0)
    finally:
        del sd.CASES["short"]
    o = row["overlap"]
    assert o["overlapping"] / o["runs_total"] > 0.8  # the switch's state: runs come together
    assert row["control"]["overlapping"] / row["control"]["runs_total"] < 0.2
    assert all(1 <= x < 5 for x in row["mean_run"])


def test_switch_drop_hardware_numbers_are_computed_from_the_raw_files() -> None:
    from benchmarks import switch_drop as sd

    if not (sd.DATA / sd.TEN).is_dir():
        pytest.skip("the testbed's raw data is not here")
    hw = sd.hardware()
    h = hw["10Mb two 405"]
    assert [round(u["loss"], 4) for u in h["udp"]] == [0.0082, 0.0077]
    o, c = h["overlap"], h["control"]
    assert round(o["overlapping"] / o["runs_total"], 2) == 0.96
    assert round(o["episodes_both"] / o["episodes"], 2) == 0.93
    assert c["overlapping"] / c["runs_total"] < 0.05
    assert [round(p["affected"], 4) for p in h["pi"]] == [0.0061, 0.0062]
    assert h["waveshare"]["affected"] == 0


def _campaign(tmp_path):
    d = tmp_path / "camp"
    d.mkdir()
    (d / "plan.txt").write_text(
        "# a plan\nk2000_8400 6400 2000 1472 const  light CBR\n"
        "p5c4000_8400 6788 4000 554 poisson light Poisson, 5 cells\n"
    )
    (d / "RAPORT.md").write_text(
        "| case | source | loss Pi1 % | loss Pi2 % | run Pi1 | run Pi2 | x |\n"
        "|---|---|---|---|---|---|---|\n"
        "| **k2000_8400** (6400/2000, 3×) | sprzęt | 4.491–4.738 | 0.000–0.001 (0+1+1 ramek)"
        " | 6.33–6.37 | 1.00–1.00 | 1 |\n"
        "| | ADR-0016 | 3.0 | 3.0 | 5.0 | 5.0 | 1 |\n"
        "| **p5c4000_8400** (6788/4000, 3×) | sprzęt | 4.0 | 0.000–0.000 (0+0+0 ramek)"
        " | 6.55–6.72 | – | 0 |\n"
    )
    for r, n in ((1, 112_000), (2, 112_100)):
        (d / f"k2000_8400_r{r}_pi1_gen.txt").write_text(f"udp_gen: {n * 3} ramek w 56.00 s = x\n")
        (d / f"k2000_8400_r{r}_pi2_gen.txt").write_text(f"udp_gen: {n} ramek w 56.00 s = x\n")
    return d


def test_light_port_reads_a_campaign(tmp_path) -> None:
    from benchmarks import light_port as lp

    _campaign(tmp_path)
    assert lp.plan("camp", tmp_path) == {
        "k2000_8400": (6_400.0, 2_000.0, 1_472, "const"),
        "p5c4000_8400": (6_788.0, 4_000.0, 554, "poisson"),
    }
    hw = lp.hardware("camp", tmp_path)
    assert hw["k2000_8400"]["loss"] == pytest.approx([0.046145, 0.000005])
    assert hw["k2000_8400"]["mean_run"] == pytest.approx([6.35, 1.0])
    assert hw["p5c4000_8400"] == {"loss": [0.04, 0.0], "mean_run": [6.635, None]}
    r1, r2 = lp.sent_rates("camp", "k2000_8400", tmp_path)
    assert (r1, r2) == pytest.approx((3 * 112_050 / 56, 112_050 / 56))
    assert lp.sent_rates("camp", "p5c4000_8400", tmp_path) == (0.0, 0.0)
    assert lp.frame(554) == 600 and lp.light_index({"rates": (6_788, 4_000), "payload": 554}) == 1
    assert lp.light_index({"rates": (1_800, 6_290), "payload": 1_472}) == 0


def test_light_port_quick_net_has_the_quick_model_s_streams() -> None:
    from benchmarks import light_port as lp
    from smolpy.model import Flow, SwitchNode

    sc = lp.quick_net((6_000, 2_400, 554, "const")).to_scenario(duration=10)
    (sw,) = [n for n in sc.nodes if isinstance(n, SwitchNode)]
    assert (sw.pool_cells, sw.ingress_pool_count, sw.ingress_alpha, sw.ingress_mark) == (
        740,
        "total",
        24.0,
        204,
    )
    flows = [(f.rate, f.size, f.pattern) for f in sc.traffic if isinstance(f, Flow)]
    assert flows == [
        (6_000, 1_518, "poisson"),
        (2_400, 600, "constant"),
        (50, 64, "constant"),
        (100, 64, "constant"),
        (100, 64, "constant"),
    ]


def test_light_port_testbed_run_measures_each_stream() -> None:
    from benchmarks import light_port as lp

    run = lp._testbed((lp.LS108GP, (6_400, 2_000, 1_472, "const"), 1, 2_000))
    heavy, light = run["streams"]
    assert heavy["frames"] > 10_000 and 0.01 < heavy["loss"] < 0.1 and heavy["mean_run"] > 3
    assert light["loss"] < 0.01  # protected
    assert run["waveshare"]["affected"] == 0


def test_light_port_summary(tmp_path) -> None:
    from benchmarks import light_port as lp

    s = {"loss": 0.01, "loss_se": 0.001, "mean_run": 2.0, "mean_run_se": 0.1}
    q = {"loss": 0.011, "loss_se": 0.001, "mean_run": 2.1, "mean_run_se": 0.1}
    stream = {"frames": 1, "loss": 0.02, "mean_run": 6.0}
    light = {"frames": 1, "loss": 0.001, "mean_run": 1.5}
    doc = {
        "one_sender": {
            "adr_0016": {"loss": 0.005, "mean_run": 6.2},
            "part_2": {"loss": 0.0048, "mean_run": 6.3},
        },
        "quick_model": [
            {"case": "c", "heavy": {"smolpy": s, "quick": q}, "light": {"smolpy": s, "quick": q}}
        ],
        "validation": [
            {
                "campaign": "camp",
                "case": "k2000_8400",
                "rates": (6_400, 2_000),
                "payload": 1_472,
                "hardware": {"loss": [0.03, 0.002], "mean_run": [6.4, 1.2]},
                "runs": [{"streams": [stream, light], "waveshare": {"affected": 0}}],
            }
        ],
    }
    path = tmp_path / "lp.json"
    path.write_text(json.dumps(doc))
    text = lp.summarize(path)
    assert "| c | 1.000 / 1.100 | 1.000 / 1.100 | 2.00 / 2.10 | 2.00 / 2.10 |" in text
    assert "| k2000_8400 | 2.000 / 3.000 | 0.100 / 0.200 | 6.00 / 6.40 | 1.50 / 1.20 | 0 |" in text
    assert "light loss 0.100 pp (bias -0.100); heavy loss 1.000 pp (bias -1.000)" in text


def test_light_port_hardware_numbers_of_the_blind_campaign() -> None:
    from benchmarks import light_port as lp

    if not (lp.DATA / lp.CAMPAIGNS[0]).is_dir():
        pytest.skip("the testbed's raw data is not here")
    hw = lp.hardware(lp.CAMPAIGNS[0])
    assert len(hw) == 21 and len(lp.plan(lp.CAMPAIGNS[0])) == 21
    assert hw["k3000_8800"]["loss"] == pytest.approx([0.0821250, 0.06646])
    assert hw["k5c4000_8400"]["mean_run"][1] is None
    assert len(lp.hardware(lp.CAMPAIGNS[1])) == 44
