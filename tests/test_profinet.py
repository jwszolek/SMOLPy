"""PROFINET RT cyclic I/O and 802.1Q strict-priority queuing (ADR-0005)."""

from __future__ import annotations

import pytest

from smolpy import Network, ProfinetDevice
from smolpy.core.adapter import pn_frame_size
from smolpy.lang import SMOLSemanticError, interpret_and_run


def _pn_net(*, qos: str = "strict", bulk: bool = False, cycle: int = 1, duration: float = 2_000):
    """PLC + IO device on one switch. With bulk=True a host on a 1 Gb/s link
    sends bursts of 100 full frames toward the PLC, congesting the PLC's
    100 Mb/s switch port that also carries the device's input frames."""
    net = Network("pn")
    sw = net.switch("sw", ports=6, qos=qos)
    plc = net.adapter("plc", ip="10.0.0.1")
    io1 = net.profinet_device("io1", ip="10.0.4.1")
    net.link(plc, sw, speed=100, length=5)
    net.link(io1, sw, speed=100, length=10)
    plc.controls(io1, cycle=cycle, inputs=32, outputs=16)
    if bulk:
        host = net.adapter("host", ip="10.0.0.2")
        net.link(host, sw, speed=1_000, length=5)
        host.sends(to=plc, rate=4_000, size=1_518, pattern="batch", batch=100)
    for metric in ("pn_latency", "pn_jitter", "pn_watchdog_expiries", "bytes_received"):
        net.observe(metric, on=plc, every=100)
    net.observe("pn_latency", on=io1, every=100)
    return net.simulate(duration=duration)


def _vals(result, key):
    return [v for _, v in result.metrics[key]]


# ---------------------------------------------------------------------------
# API and validation
# ---------------------------------------------------------------------------


class TestApi:
    def test_frame_size_padded_to_ethernet_minimum(self) -> None:
        assert pn_frame_size(10) == 60
        assert pn_frame_size(36) == 60
        assert pn_frame_size(100) == 124

    def test_controls_adds_spec(self) -> None:
        net = Network("t")
        plc = net.adapter("plc", ip="10.0.0.1")
        io1 = net.profinet_device("io1", ip="10.0.4.1")
        plc.controls(io1, cycle=8, inputs=32, outputs=200, hold=5)
        (spec,) = plc.profinet_specs
        assert isinstance(spec.device, ProfinetDevice)
        assert (spec.cycle_ms, spec.hold) == (8, 5)
        assert spec.input_frame_size == 60
        assert spec.output_frame_size == 224

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"cycle": 3, "inputs": 8, "outputs": 8},
            {"cycle": 1024, "inputs": 8, "outputs": 8},
            {"cycle": 4, "inputs": 0, "outputs": 8},
            {"cycle": 4, "inputs": 8, "outputs": 1441},
            {"cycle": 4, "inputs": 8, "outputs": 8, "hold": 0},
        ],
    )
    def test_invalid_ar_parameters_rejected(self, kwargs) -> None:
        net = Network("t")
        plc = net.adapter("plc", ip="10.0.0.1")
        io1 = net.profinet_device("io1", ip="10.0.4.1")
        with pytest.raises(ValueError):
            plc.controls(io1, **kwargs)

    def test_controls_requires_profinet_device(self) -> None:
        net = Network("t")
        plc = net.adapter("plc", ip="10.0.0.1")
        other = net.adapter("other", ip="10.0.0.2")
        with pytest.raises(ValueError, match="not a ProfinetDevice"):
            plc.controls(other, cycle=4, inputs=8, outputs=8)

    def test_duplicate_ar_rejected(self) -> None:
        net = Network("t")
        plc = net.adapter("plc", ip="10.0.0.1")
        io1 = net.profinet_device("io1", ip="10.0.4.1")
        plc.controls(io1, cycle=4, inputs=8, outputs=8)
        with pytest.raises(ValueError, match="already controls"):
            plc.controls(io1, cycle=8, inputs=8, outputs=8)

    def test_invalid_priority_rejected(self) -> None:
        net = Network("t")
        a = net.adapter("a", ip="10.0.0.1")
        b = net.adapter("b", ip="10.0.0.2")
        with pytest.raises(ValueError, match="0-7"):
            a.sends(to=b, rate=10, priority=8)

    def test_invalid_switch_qos_rejected(self) -> None:
        with pytest.raises(ValueError, match="qos"):
            Network("t").switch("sw", ports=4, qos="wfq")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cyclic exchange
# ---------------------------------------------------------------------------


class TestCyclicExchange:
    def test_one_input_frame_per_cycle(self) -> None:
        result = _pn_net(cycle=4, duration=1_000)
        last_t, received_mb = result.metrics["bytes_received:plc"][-1]
        frames = received_mb * 1_000_000 / pn_frame_size(32)
        assert abs(frames - last_t / 4) <= 1

    def test_both_directions_have_positive_latency(self) -> None:
        result = _pn_net()
        assert all(v > 0 for v in _vals(result, "pn_latency:plc"))
        assert all(v > 0 for v in _vals(result, "pn_latency:io1"))

    def test_idle_network_has_no_jitter_and_no_expiries(self) -> None:
        result = _pn_net()
        assert max(_vals(result, "pn_jitter:plc")) < 1.0
        assert _vals(result, "pn_watchdog_expiries:plc")[-1] == 0

    def test_delayed_start(self) -> None:
        net = Network("t")
        sw = net.switch("sw", ports=4)
        plc = net.adapter("plc", ip="10.0.0.1")
        io1 = net.profinet_device("io1", ip="10.0.4.1")
        net.link(plc, sw, speed=100, length=5)
        net.link(io1, sw, speed=100, length=5)
        plc.controls(io1, cycle=4, inputs=8, outputs=8, delay_ms=500)
        net.observe("pn_latency", on=plc, every=100)
        result = net.simulate(duration=1_000)
        assert min(t for t, _ in result.metrics["pn_latency:plc"]) > 500


# ---------------------------------------------------------------------------
# Strict priority vs FIFO under bursty load
# ---------------------------------------------------------------------------


class TestPrioritisation:
    def test_strict_priority_keeps_cyclic_data_on_time(self) -> None:
        strict = _pn_net(qos="strict", bulk=True)
        fifo = _pn_net(qos="fifo", bulk=True)
        assert max(_vals(strict, "pn_jitter:plc")) < 200
        assert max(_vals(fifo, "pn_jitter:plc")) > 10 * max(_vals(strict, "pn_jitter:plc"))

    def test_watchdog_expires_only_without_prioritisation(self) -> None:
        strict = _pn_net(qos="strict", bulk=True)
        fifo = _pn_net(qos="fifo", bulk=True)
        assert _vals(strict, "pn_watchdog_expiries:plc")[-1] == 0
        assert _vals(fifo, "pn_watchdog_expiries:plc")[-1] > 0

    def test_high_priority_bulk_traffic_defeats_prioritisation(self) -> None:
        """Mis-prioritised bulk traffic (PCP 7 > PROFINET's 6) is served first."""
        net = Network("t")
        sw = net.switch("sw", ports=6)
        plc = net.adapter("plc", ip="10.0.0.1")
        io1 = net.profinet_device("io1", ip="10.0.4.1")
        host = net.adapter("host", ip="10.0.0.2")
        net.link(plc, sw, speed=100, length=5)
        net.link(io1, sw, speed=100, length=10)
        net.link(host, sw, speed=1_000, length=5)
        plc.controls(io1, cycle=1, inputs=32, outputs=16)
        host.sends(to=plc, rate=4_000, size=1_518, pattern="batch", batch=100, priority=7)
        net.observe("pn_watchdog_expiries", on=plc, every=100)
        result = net.simulate(duration=2_000)
        assert result.metrics["pn_watchdog_expiries:plc"][-1][1] > 0


# ---------------------------------------------------------------------------
# .smol language
# ---------------------------------------------------------------------------

_SMOL = """
network "pn" {{
    adapter         plc   ip=10.0.0.1
    profinet_device io1   ip=10.0.4.1
    switch          sw    ports=4 qos={qos}

    link plc -- sw speed=100 length=5
    link io1 -- sw speed=100 length=10

    {control}

    observe pn_latency           on plc every=100
    observe pn_jitter            on io1 every=100
    observe pn_watchdog_expiries on plc every=100

    simulate duration=1000 mode=headless
}}
"""

_CONTROL = "control plc -> io1 cycle=4 inputs=32 outputs=16 hold=3"


class TestSmol:
    def _run(self, tmp_path, *, qos: str = "strict", control: str = _CONTROL):
        path = tmp_path / "pn.smol"
        path.write_text(_SMOL.format(qos=qos, control=control))
        return interpret_and_run(path)

    def test_runs_and_produces_pn_metrics(self, tmp_path) -> None:
        result = self._run(tmp_path)
        assert result.metrics["pn_latency:plc"]
        assert result.metrics["pn_jitter:io1"]
        assert result.metrics["pn_watchdog_expiries:plc"][-1][1] == 0

    def test_fifo_qos_accepted(self, tmp_path) -> None:
        assert self._run(tmp_path, qos="fifo").metrics["pn_latency:plc"]

    def test_control_target_must_be_profinet_device(self, tmp_path) -> None:
        with pytest.raises(SMOLSemanticError, match="expected ProfinetDevice"):
            self._run(tmp_path, control="control plc -> sw cycle=4 inputs=8 outputs=8")

    def test_invalid_cycle_is_semantic_error(self, tmp_path) -> None:
        with pytest.raises(SMOLSemanticError, match="cycle"):
            self._run(tmp_path, control="control plc -> io1 cycle=3 inputs=8 outputs=8")

    def test_invalid_qos_is_semantic_error(self, tmp_path) -> None:
        with pytest.raises(SMOLSemanticError, match="qos"):
            self._run(tmp_path, qos="wfq")

    def test_flow_priority(self, tmp_path) -> None:
        path = tmp_path / "prio.smol"
        path.write_text(
            """
            network "t" {
                adapter a ip=10.0.0.1
                adapter b ip=10.0.0.2
                switch  sw ports=4
                link a -- sw speed=100 length=5
                link b -- sw speed=100 length=5
                flow a -> b rate=100 priority=9
                simulate duration=100
            }
            """
        )
        with pytest.raises(SMOLSemanticError, match="0-7"):
            interpret_and_run(path)
