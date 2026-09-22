"""Tests for the .smol external DSL: parser, transformer, interpreter."""
from __future__ import annotations

import pytest

from smolpy.lang import SMOLSemanticError, SMOLSyntaxError, interpret_and_run
from smolpy.lang.ast import (
    AdapterDecl,
    FlowDecl,
    HubDecl,
    LinkDecl,
    ModbusSlaveDecl,
    MqttBrokerDecl,
    ObserveDecl,
    PollDecl,
    PublishDecl,
    RouteDecl,
    SimulateDecl,
    SwitchDecl,
)
from smolpy.lang.transformer import parse_smol

_FULL_TOPOLOGY = """
network "office-net" {
    adapter      host-A   ip=10.0.0.1
    adapter      server   ip=10.0.0.10
    switch       sw1      ports=8  mode=store-and-forward
    hub          hub1     ports=4
    mqtt_broker  broker1  ip=10.0.2.1
    modbus_slave sensor1  ip=10.0.3.1  unit_id=1

    link  host-A   --  sw1      speed=1000   length=5
    link  server   --  sw1      speed=10000  length=2
    link  broker1  --  sw1      speed=1000   length=2
    link  sensor1  --  sw1      speed=100    length=5

    flow     host-A  ->  server   rate=8000  size=1518  pattern=constant
    flow     host-A  ->  server   rate=4000  size=512   pattern=poisson  delay=2000

    publish  server  ->  broker1  topic="plant/temp"  rate=1.0  payload=20  qos=1
    route    broker1  topic="plant/temp"  ->  [server]

    poll     host-A  ->  sensor1  register=40001  count=10  rate=1.0

    observe  throughput      on  server   every=100
    observe  queue_depth     on  sw1      every=50
    observe  modbus_latency  on  host-A   every=500

    simulate  duration=5000  mode=headless
}
"""


# ---------------------------------------------------------------------------
# Parsing / AST shape
# ---------------------------------------------------------------------------

class TestParsing:
    def test_network_name(self) -> None:
        decl = parse_smol(_FULL_TOPOLOGY, "t.smol")
        assert decl.name == "office-net"

    def test_declaration_types_in_order(self) -> None:
        decl = parse_smol(_FULL_TOPOLOGY, "t.smol")
        kinds = [type(d) for d in decl.declarations]
        assert kinds == [
            AdapterDecl,
            AdapterDecl,
            SwitchDecl,
            HubDecl,
            MqttBrokerDecl,
            ModbusSlaveDecl,
            LinkDecl,
            LinkDecl,
            LinkDecl,
            LinkDecl,
            FlowDecl,
            FlowDecl,
            PublishDecl,
            RouteDecl,
            PollDecl,
            ObserveDecl,
            ObserveDecl,
            ObserveDecl,
            SimulateDecl,
        ]

    def test_hyphenated_identifier(self) -> None:
        decl = parse_smol(_FULL_TOPOLOGY, "t.smol")
        adapter = decl.declarations[0]
        assert isinstance(adapter, AdapterDecl)
        assert adapter.name == "host-A"

    def test_flow_delay_field_from_bare_delay_keyword(self) -> None:
        decl = parse_smol(_FULL_TOPOLOGY, "t.smol")
        flows = [d for d in decl.declarations if isinstance(d, FlowDecl)]
        assert flows[1].delay_ms == 2000.0

    def test_route_targets_list(self) -> None:
        decl = parse_smol(_FULL_TOPOLOGY, "t.smol")
        route = next(d for d in decl.declarations if isinstance(d, RouteDecl))
        assert route.targets == ["server"]

    def test_line_numbers_populated(self) -> None:
        decl = parse_smol(_FULL_TOPOLOGY, "t.smol")
        adapter = decl.declarations[0]
        assert adapter.line == 3  # first non-blank line of the block


# ---------------------------------------------------------------------------
# Interpreter — builds a real Network and runs it
# ---------------------------------------------------------------------------

class TestInterpreter:
    def test_full_topology_runs_and_produces_metrics(self, tmp_path) -> None:
        smol_file = tmp_path / "office.smol"
        smol_file.write_text(_FULL_TOPOLOGY)
        result = interpret_and_run(smol_file)

        assert result.metrics.get("throughput:server")
        assert result.metrics.get("queue_depth:sw1")
        assert result.metrics.get("modbus_latency:host-A")

    def test_modbus_latency_samples_are_positive(self, tmp_path) -> None:
        smol_file = tmp_path / "office.smol"
        smol_file.write_text(_FULL_TOPOLOGY)
        result = interpret_and_run(smol_file)
        vals = [v for _, v in result.metrics["modbus_latency:host-A"] if v > 0]
        assert vals

    def test_mqtt_routing_reaches_subscriber(self, tmp_path) -> None:
        smol_file = tmp_path / "office.smol"
        smol_file.write_text(_FULL_TOPOLOGY)
        result = interpret_and_run(smol_file)
        vals = [v for _, v in result.metrics["throughput:server"] if v > 0]
        assert vals


class TestSimulateSeed:
    _TEMPLATE = """
    network "t" {
        adapter a ip=10.0.0.1
        adapter b ip=10.0.0.2
        switch  sw1 ports=4
        link a -- sw1 speed=100 length=5
        link b -- sw1 speed=1000 length=2
        flow a -> b rate=2000 size=imix pattern=poisson
        observe latency on b every=50
        simulate duration=500 mode=headless%s
    }
    """

    def _metrics(self, tmp_path, extra: str):
        f = tmp_path / "t.smol"
        f.write_text(self._TEMPLATE % extra)
        return interpret_and_run(f).metrics

    def test_seed_is_parsed(self) -> None:
        decl = parse_smol(self._TEMPLATE % " seed=7", "t.smol")
        assert decl.declarations[-1].seed == 7

    def test_seed_defaults_to_42(self) -> None:
        decl = parse_smol(self._TEMPLATE % "", "t.smol")
        assert decl.declarations[-1].seed == 42

    def test_seed_changes_result_and_default_matches_42(self, tmp_path) -> None:
        default = self._metrics(tmp_path, "")
        assert default == self._metrics(tmp_path, " seed=42")
        assert default != self._metrics(tmp_path, " seed=1")


# ---------------------------------------------------------------------------
# Semantic errors
# ---------------------------------------------------------------------------

class TestSemanticErrors:
    def _run(self, tmp_path, source: str):
        smol_file = tmp_path / "bad.smol"
        smol_file.write_text(source)
        interpret_and_run(smol_file)

    def test_undeclared_node_reference(self, tmp_path) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            link a -- sw1 speed=100 length=5
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSemanticError, match="undeclared node 'sw1'"):
            self._run(tmp_path, source)

    def test_duplicate_node_name(self, tmp_path) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            adapter a ip=10.0.0.2
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSemanticError, match="already exists"):
            self._run(tmp_path, source)

    def test_wrong_node_type_for_statement(self, tmp_path) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            switch sw1 ports=4
            link a -- sw1 speed=100 length=5
            poll a -> sw1 register=1 count=1 rate=1.0
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSemanticError, match="expected ModbusSlave"):
            self._run(tmp_path, source)

    def test_invalid_metric_for_node_type(self, tmp_path) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            switch sw1 ports=4
            link a -- sw1 speed=100 length=5
            observe throughput on sw1 every=100
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSemanticError, match="expected Adapter"):
            self._run(tmp_path, source)

    def test_unsupported_metric_rejected(self, tmp_path) -> None:
        """frame_loss is declared in the Python API's MetricName type but has
        no dispatch branch in the engine at all — the .smol interpreter must
        not pretend it works."""
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            observe frame_loss on a every=100
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSemanticError, match="unknown or unsupported metric"):
            self._run(tmp_path, source)

    def test_missing_simulate_statement(self, tmp_path) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
        }
        """
        with pytest.raises(SMOLSemanticError, match="must include a 'simulate' statement"):
            self._run(tmp_path, source)

    def test_multiple_simulate_statements_rejected(self, tmp_path) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            simulate duration=1000 mode=headless
            simulate duration=2000 mode=headless
        }
        """
        with pytest.raises(SMOLSemanticError, match="only one 'simulate' statement"):
            self._run(tmp_path, source)


# ---------------------------------------------------------------------------
# Syntax errors
# ---------------------------------------------------------------------------

class TestSyntaxErrors:
    def test_missing_equals_sign(self) -> None:
        source = """
        network "t" {
            adapter a ip 10.0.0.1
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSyntaxError):
            parse_smol(source, "bad.smol")

    def test_unclosed_block(self) -> None:
        source = """
        network "t" {
            adapter a ip=10.0.0.1
            simulate duration=1000 mode=headless
        """
        with pytest.raises(SMOLSyntaxError):
            parse_smol(source, "bad.smol")

    def test_missing_required_argument(self) -> None:
        source = """
        network "t" {
            switch sw1
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSyntaxError, match="missing required argument 'ports'"):
            parse_smol(source, "bad.smol")

    def test_error_includes_file_and_line(self) -> None:
        source = """
        network "t" {
            adapter a ip 10.0.0.1
            simulate duration=1000 mode=headless
        }
        """
        with pytest.raises(SMOLSyntaxError) as exc_info:
            parse_smol(source, "bad.smol")
        assert exc_info.value.file == "bad.smol"
        assert exc_info.value.line == 3
