from __future__ import annotations

from importlib import resources
from typing import Any

from lark import Lark, Token, Transformer, UnexpectedInput, v_args
from lark.exceptions import VisitError

from smolpy.lang.ast import (
    AdapterDecl,
    Decl,
    FlowDecl,
    HubDecl,
    LinkDecl,
    ModbusSlaveDecl,
    MqttBrokerDecl,
    NetworkDecl,
    ObserveDecl,
    PollDecl,
    PublishDecl,
    RouteDecl,
    SimulateDecl,
    SwitchDecl,
)
from smolpy.lang.errors import SMOLSyntaxError

_GRAMMAR = resources.files(__package__).joinpath("grammar.lark").read_text(encoding="utf-8")


def _coerce(raw: str) -> int | float | str:
    """Bare-word coercion: int, then float, then leave as string."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _require(kwargs: dict[str, Any], key: str, *, file: str, line: int, stmt: str) -> Any:
    if key not in kwargs:
        raise SMOLSyntaxError(file, line, f"'{stmt}' is missing required argument '{key}'")
    return kwargs[key]


@v_args(meta=True)
class _SmolTransformer(Transformer):
    def __init__(self, file: str) -> None:
        super().__init__()
        self.file = file

    # --- terminals ---

    def WORD(self, tok: Token) -> int | float | str:
        return _coerce(str(tok))

    def STRING(self, tok: Token) -> str:
        return str(tok)[1:-1]

    # --- shared helper rules ---

    def value(self, meta: Any, children: list) -> Any:
        (v,) = children
        return v

    def node_list(self, meta: Any, children: list) -> list[str]:
        return [str(c) for c in children]

    def arg(self, meta: Any, children: list) -> tuple[str, Any]:
        key, value = children
        return (str(key), value)

    # --- statements ---

    def adapter_decl(self, meta: Any, children: list) -> AdapterDecl:
        name, *pairs = children
        kw = dict(pairs)
        return AdapterDecl(
            name=str(name),
            ip=str(_require(kw, "ip", file=self.file, line=meta.line, stmt="adapter")),
            mac=str(kw["mac"]) if "mac" in kw else None,
            line=meta.line,
        )

    def switch_decl(self, meta: Any, children: list) -> SwitchDecl:
        name, *pairs = children
        kw = dict(pairs)
        return SwitchDecl(
            name=str(name),
            ports=int(_require(kw, "ports", file=self.file, line=meta.line, stmt="switch")),
            mode=str(kw.get("mode", "store-and-forward")),
            line=meta.line,
        )

    def hub_decl(self, meta: Any, children: list) -> HubDecl:
        name, *pairs = children
        kw = dict(pairs)
        return HubDecl(
            name=str(name),
            ports=int(_require(kw, "ports", file=self.file, line=meta.line, stmt="hub")),
            line=meta.line,
        )

    def mqtt_broker_decl(self, meta: Any, children: list) -> MqttBrokerDecl:
        name, *pairs = children
        kw = dict(pairs)
        return MqttBrokerDecl(
            name=str(name),
            ip=str(_require(kw, "ip", file=self.file, line=meta.line, stmt="mqtt_broker")),
            line=meta.line,
        )

    def modbus_slave_decl(self, meta: Any, children: list) -> ModbusSlaveDecl:
        name, *pairs = children
        kw = dict(pairs)
        return ModbusSlaveDecl(
            name=str(name),
            ip=str(_require(kw, "ip", file=self.file, line=meta.line, stmt="modbus_slave")),
            unit_id=int(
                _require(kw, "unit_id", file=self.file, line=meta.line, stmt="modbus_slave")
            ),
            line=meta.line,
        )

    def link_decl(self, meta: Any, children: list) -> LinkDecl:
        a, b, *pairs = children
        kw = dict(pairs)
        return LinkDecl(
            a=str(a),
            b=str(b),
            speed=float(_require(kw, "speed", file=self.file, line=meta.line, stmt="link")),
            length=float(_require(kw, "length", file=self.file, line=meta.line, stmt="link")),
            line=meta.line,
        )

    def flow_decl(self, meta: Any, children: list) -> FlowDecl:
        src, dst, *pairs = children
        kw = dict(pairs)
        size = kw.get("size", 512)
        return FlowDecl(
            src=str(src),
            dst=str(dst),
            rate=float(_require(kw, "rate", file=self.file, line=meta.line, stmt="flow")),
            size=size if isinstance(size, str) else int(size),
            pattern=str(kw.get("pattern", "constant")),
            delay_ms=float(kw.get("delay", 0.0)),
            line=meta.line,
        )

    def publish_decl(self, meta: Any, children: list) -> PublishDecl:
        src, broker, *pairs = children
        kw = dict(pairs)
        return PublishDecl(
            src=str(src),
            broker=str(broker),
            topic=str(_require(kw, "topic", file=self.file, line=meta.line, stmt="publish")),
            rate=float(kw.get("rate", 1.0)),
            payload=int(kw.get("payload", 20)),
            qos=int(kw.get("qos", 0)),
            delay_ms=float(kw.get("delay", 0.0)),
            line=meta.line,
        )

    def route_decl(self, meta: Any, children: list) -> RouteDecl:
        broker = children[0]
        targets = children[-1]
        pairs = children[1:-1]
        kw = dict(pairs)
        return RouteDecl(
            broker=str(broker),
            topic=str(_require(kw, "topic", file=self.file, line=meta.line, stmt="route")),
            targets=targets,
            line=meta.line,
        )

    def poll_decl(self, meta: Any, children: list) -> PollDecl:
        master, slave, *pairs = children
        kw = dict(pairs)
        return PollDecl(
            master=str(master),
            slave=str(slave),
            register=int(_require(kw, "register", file=self.file, line=meta.line, stmt="poll")),
            count=int(_require(kw, "count", file=self.file, line=meta.line, stmt="poll")),
            rate=float(kw.get("rate", 1.0)),
            delay_ms=float(kw.get("delay", 0.0)),
            line=meta.line,
        )

    def observe_decl(self, meta: Any, children: list) -> ObserveDecl:
        metric, target, *pairs = children
        kw = dict(pairs)
        return ObserveDecl(
            metric=str(metric),
            target=str(target),
            every=float(_require(kw, "every", file=self.file, line=meta.line, stmt="observe")),
            line=meta.line,
        )

    def simulate_decl(self, meta: Any, children: list) -> SimulateDecl:
        kw = dict(children)
        return SimulateDecl(
            duration=float(
                _require(kw, "duration", file=self.file, line=meta.line, stmt="simulate")
            ),
            mode=str(kw.get("mode", "headless")),
            line=meta.line,
        )

    def statement(self, meta: Any, children: list) -> Decl:
        (decl,) = children
        return decl

    def start(self, meta: Any, children: list) -> NetworkDecl:
        name, *decls = children
        return NetworkDecl(name=str(name), declarations=list(decls), line=meta.line)


def parse_smol(text: str, file: str) -> NetworkDecl:
    """Parse .smol source into a NetworkDecl AST. Raises SMOLSyntaxError on malformed input."""
    parser = Lark(_GRAMMAR, parser="lalr", propagate_positions=True)
    try:
        tree = parser.parse(text)
    except UnexpectedInput as exc:
        line = getattr(exc, "line", 1) or 1
        raise SMOLSyntaxError(file, line, str(exc).splitlines()[0]) from exc
    try:
        return _SmolTransformer(file).transform(tree)
    except VisitError as exc:
        if isinstance(exc.orig_exc, SMOLSyntaxError):
            raise exc.orig_exc from exc
        raise
