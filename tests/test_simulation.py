"""Simulation engine and metric correctness tests."""
from __future__ import annotations

import json
import math
import pathlib

import pytest

from smolpy import Network, MQTTBroker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_net(duration_ms: float = 5_000):
    """One client → switch → server at 100 Mbps; returns (net, client, server)."""
    net = Network("simple")
    client = net.adapter("client", ip="10.0.0.1")
    server = net.adapter("server", ip="10.0.0.2")
    sw = net.switch("sw", ports=4)
    net.link(client, sw, speed=100, length=5)
    net.link(server, sw, speed=1_000, length=2)
    fps = int(100_000_000 / (1_518 * 8))
    client.sends(to=server, rate=fps, size=1_518, pattern="constant")
    net.observe("throughput",     on=server, every=200)
    net.observe("latency",        on=server, every=200)
    net.observe("bytes_received", on=server, every=500)
    net.observe("bytes_sent",     on=client, every=500)
    net.observe("queue_depth",    on=sw,     every=200)
    result = net.simulate(duration=duration_ms)
    return result, client, server, sw


# ---------------------------------------------------------------------------
# Metric population
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_throughput_samples_produced(self):
        result, *_ = _simple_net()
        samples = result.metrics.get("throughput:server", [])
        assert len(samples) > 5

    def test_throughput_near_100_mbps(self):
        result, *_ = _simple_net(duration_ms=10_000)
        vals = [v for _, v in result.metrics["throughput:server"] if v > 0]
        avg = sum(vals) / len(vals)
        # 100 Mbps link with 1518-byte frames; expect 90–100 Mbps after warmup
        assert 85 <= avg <= 105, f"expected ~100 Mb/s, got {avg:.1f}"

    def test_latency_is_positive(self):
        result, *_ = _simple_net()
        vals = [v for _, v in result.metrics["latency:server"] if v > 0]
        assert vals, "no positive latency samples"
        assert all(v > 0 for v in vals)

    def test_bytes_sent_monotonically_increases(self):
        result, *_ = _simple_net()
        vals = [v for _, v in result.metrics["bytes_sent:client"]]
        assert vals == sorted(vals), "bytes_sent should be non-decreasing"
        assert vals[-1] > 0

    def test_bytes_received_monotonically_increases(self):
        result, *_ = _simple_net()
        vals = [v for _, v in result.metrics["bytes_received:server"]]
        assert vals == sorted(vals)
        assert vals[-1] > 0

    def test_queue_depth_reasonable(self):
        """With a 1 Gbps server link and 100 Mbps client, queue should stay near zero."""
        result, *_ = _simple_net(duration_ms=10_000)
        vals = [v for _, v in result.metrics["queue_depth:sw"]]
        avg = sum(vals) / len(vals)
        assert avg < 50, f"queue avg {avg:.1f} is unrealistically high (MAC flooding?)"


# ---------------------------------------------------------------------------
# Staggered start / delay_ms
# ---------------------------------------------------------------------------

class TestDelayedStart:
    def test_delayed_client_sends_nothing_before_delay(self):
        net = Network("delay-test")
        client = net.adapter("client", ip="10.0.0.1")
        server = net.adapter("server", ip="10.0.0.2")
        sw = net.switch("sw", ports=4)
        net.link(client, sw, speed=100, length=5)
        net.link(server, sw, speed=1_000, length=2)
        client.sends(to=server, rate=100, size=512, pattern="constant", delay_ms=3_000)
        net.observe("bytes_sent", on=client, every=200)
        result = net.simulate(duration=5_000)
        samples = result.metrics["bytes_sent:client"]
        # All samples before t=2500 ms should be zero (delay is 3 s)
        early = [v for t, v in samples if t < 2_500]
        assert all(v == 0.0 for v in early), "client sent bytes before its delay expired"

    def test_bytes_sent_after_delay(self):
        net = Network("delay-test")
        client = net.adapter("client", ip="10.0.0.1")
        server = net.adapter("server", ip="10.0.0.2")
        sw = net.switch("sw", ports=4)
        net.link(client, sw, speed=100, length=5)
        net.link(server, sw, speed=1_000, length=2)
        client.sends(to=server, rate=100, size=512, pattern="constant", delay_ms=2_000)
        net.observe("bytes_sent", on=client, every=200)
        result = net.simulate(duration=5_000)
        samples = result.metrics["bytes_sent:client"]
        late = [v for t, v in samples if t > 4_000]
        assert late and late[-1] > 0, "client sent nothing after its delay expired"


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

class TestMQTT:
    def _mqtt_net(self, duration_ms: float = 10_000):
        net = Network("mqtt-test")
        sw = net.switch("sw", ports=6)
        broker = net.mqtt_broker("broker", ip="10.0.2.1")
        server = net.adapter("server", ip="10.0.3.1")
        sensor = net.adapter("sensor", ip="10.0.1.1")
        net.link(sensor, sw, speed=100, length=5)
        net.link(sw, broker, speed=1_000, length=2)
        net.link(broker, server, speed=1_000, length=2)
        broker.routes("plant/temp", to=[server])
        sensor.publishes(to=broker, topic="plant/temp", rate=1.0, payload=20, qos=1)
        net.observe("broker_queue", on=broker, every=500)
        net.observe("latency",      on=server, every=500)
        net.observe("throughput",   on=server, every=500)
        result = net.simulate(duration=duration_ms)
        return result

    def test_mqtt_broker_is_mqttbroker_type(self):
        net = Network("t")
        b = net.mqtt_broker("b", ip="10.0.0.1")
        assert isinstance(b, MQTTBroker)

    def test_broker_queue_samples_produced(self):
        result = self._mqtt_net()
        samples = result.metrics.get("broker_queue:broker", [])
        assert len(samples) > 5

    def test_broker_queue_stays_near_zero(self):
        """At 1 msg/s the 1 Gbps uplink should drain instantly."""
        result = self._mqtt_net()
        vals = [v for _, v in result.metrics["broker_queue:broker"]]
        avg = sum(vals) / len(vals)
        assert avg < 2, f"broker queue avg {avg:.2f} suggests a bottleneck"

    def test_server_receives_forwarded_messages(self):
        result = self._mqtt_net(duration_ms=15_000)
        samples = result.metrics.get("throughput:server", [])
        nonzero = [v for _, v in samples if v > 0]
        assert nonzero, "server received no forwarded MQTT messages"

    def test_server_latency_present(self):
        result = self._mqtt_net()
        samples = result.metrics.get("latency:server", [])
        assert samples, "no latency samples at server"
        assert all(v > 0 for _, v in samples)

    def test_mqtt_routes_registration(self):
        net = Network("t")
        sw = net.switch("sw", ports=4)
        broker = net.mqtt_broker("broker", ip="10.0.2.1")
        server = net.adapter("server", ip="10.0.3.1")
        broker.routes("sensors/data", to=[server])
        assert "sensors/data" in broker._routes
        assert server in broker._routes["sensors/data"]

    def test_publishes_adds_spec(self):
        net = Network("t")
        broker = net.mqtt_broker("broker", ip="10.0.2.1")
        sensor = net.adapter("sensor", ip="10.0.1.1")
        sensor.publishes(to=broker, topic="t/1", rate=2.0, payload=32, qos=0)
        assert len(sensor.mqtt_specs) == 1
        spec = sensor.mqtt_specs[0]
        assert spec.rate_hz == 2.0
        assert spec.qos == 0
        # frame_size = 54 (Ethernet+IP+TCP) + 2+2+3 (MQTT hdr + topic "t/1") + 32 payload = 93
        assert spec.frame_size == 54 + 2 + 2 + len("t/1") + 32


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_csv_creates_file(self, tmp_path):
        result, *_ = _simple_net()
        out = tmp_path / "metrics.csv"
        result.export(str(out))
        assert out.exists()
        assert out.stat().st_size > 0

    def test_export_csv_header(self, tmp_path):
        result, *_ = _simple_net()
        out = tmp_path / "metrics.csv"
        result.export(str(out))
        lines = out.read_text().splitlines()
        assert lines[0] == "time_ms,metric,value"

    def test_export_csv_row_count(self, tmp_path):
        result, *_ = _simple_net()
        out = tmp_path / "metrics.csv"
        result.export(str(out))
        rows = out.read_text().splitlines()[1:]  # skip header
        total_samples = sum(len(s) for s in result.metrics.values())
        assert len(rows) == total_samples

    def test_export_json_creates_file(self, tmp_path):
        result, *_ = _simple_net()
        out = tmp_path / "metrics.json"
        result.export(str(out))
        assert out.exists()

    def test_export_json_structure(self, tmp_path):
        result, *_ = _simple_net()
        out = tmp_path / "metrics.json"
        result.export(str(out))
        data = json.loads(out.read_text())
        assert "metrics" in data
        assert "network" in data
        for key, series in data["metrics"].items():
            assert isinstance(series, list)
            for pair in series:
                assert len(pair) == 2

    def test_export_explicit_format_override(self, tmp_path):
        result, *_ = _simple_net()
        out = tmp_path / "output.dat"
        result.export(str(out), format="json")
        data = json.loads(out.read_text())
        assert "metrics" in data

    def test_export_unknown_format_raises(self, tmp_path):
        result, *_ = _simple_net()
        with pytest.raises(ValueError, match="Unknown format"):
            result.export(str(tmp_path / "out.xyz"))
