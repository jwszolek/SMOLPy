"""Modbus and MQTT over ns-3's TCP against SimPy, as a shared link loads up
(ADR-0004, Phase 6 exit criterion: "under load, the differences are
quantified and documented").

Scenario L1, a plant cell: a SCADA master polls four Modbus slaves, and two
sensors publish MQTT to a broker that forwards to an HMI. The slaves and
sensors sit behind an edge switch whose 10 Mb/s uplink to the core also
carries a camera's UDP stream to a recorder. The camera's rate sets the
offered load of the uplink, rho: the wire bits of every frame the scenario
sends across it (responses, PUBLISH messages, video), as a share of its
capacity. TCP's own acknowledgements are not part of rho: they are what
ns-3 adds.

Each load runs on SimPy and on ns-3 with the same samplers; the record keeps
each metric's mean over the run, its last sample, and the run's statistics.

    python -m benchmarks load [--loads 0.5 0.9 1.0 1.2] [--duration 20000]
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

from benchmarks.runner import RESULTS_DIR, environment
from smolpy import Network, backends
from smolpy.model import Scenario
from smolpy.model.frames import (
    WIRE_OVERHEAD_BYTES,
    modbus_response_frame_size,
    mqtt_publish_frame_size,
)

UPLINK_MBPS = 10
CAMERA_FRAME = 1_500
SLAVES = 4
POLL_RATE = 100  # polls/s per slave
REGISTERS = 50
PUBLISHES = (  # topic, rate (msg/s), payload (B), QoS
    ("cell/temp", 200, 32, 1),
    ("cell/vib", 200, 200, 0),
)
LOADS = (0.5, 0.8, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.35, 1.5)
DURATION_MS = 20_000
METRICS = ("modbus_latency:scada", "latency:hmi", "bytes_received:scada", "bytes_received:hmi")


def _wire_bits(frame_bytes: int) -> int:
    return (frame_bytes + WIRE_OVERHEAD_BYTES) * 8


def protocol_load() -> float:
    """The uplink's offered load without the camera: Modbus responses and
    PUBLISH messages, as a share of its capacity."""
    bits = SLAVES * POLL_RATE * _wire_bits(modbus_response_frame_size(REGISTERS))
    bits += sum(
        rate * _wire_bits(mqtt_publish_frame_size(topic, payload, qos))
        for topic, rate, payload, qos in PUBLISHES
    )
    return bits / (UPLINK_MBPS * 1e6)


def scenario(rho: float, duration_ms: float = DURATION_MS) -> Scenario:
    """L1 with the uplink's offered load at *rho*."""
    camera_share = rho - protocol_load()
    if camera_share < 0:
        raise ValueError(f"rho must be at least the protocols' own load, {protocol_load():.4f}")
    net = Network("L1")
    core = net.switch("core", ports=5)
    edge = net.switch("edge", ports=SLAVES + len(PUBLISHES) + 2)
    scada = net.adapter("scada", ip="10.0.0.1")
    hmi = net.adapter("hmi", ip="10.0.0.2")
    recorder = net.adapter("recorder", ip="10.0.0.3")
    broker = net.mqtt_broker("broker", ip="10.0.0.4")
    for node in (scada, hmi, recorder, broker):
        net.link(node, core, speed=100, length=10)
    net.link(edge, core, speed=UPLINK_MBPS, length=100)
    slaves = [net.modbus_slave(f"s{i}", ip=f"10.0.1.{i + 1}", unit_id=i + 1) for i in range(SLAVES)]
    sensors = [net.adapter(f"t{i}", ip=f"10.0.2.{i + 1}") for i in range(len(PUBLISHES))]
    camera = net.adapter("camera", ip="10.0.3.1")
    for node in (*slaves, *sensors, camera):
        net.link(node, edge, speed=100, length=10)
    for i, slave in enumerate(slaves):
        scada.polls(slave, register=0, count=REGISTERS, rate=POLL_RATE, delay_ms=0.1 * i)
    for i, (sensor, (topic, rate, payload, qos)) in enumerate(zip(sensors, PUBLISHES, strict=True)):
        sensor.publishes(broker, topic=topic, rate=rate, payload=payload, qos=qos, delay_ms=i / 20)
        broker.routes(topic, to=[hmi])
    if camera_share > 0:
        rate = camera_share * UPLINK_MBPS * 1e6 / _wire_bits(CAMERA_FRAME)
        camera.sends(recorder, rate=rate, size=CAMERA_FRAME)
    every = duration_ms / 20
    net.observe("modbus_latency", on=scada, every=every)
    net.observe("latency", on=hmi, every=every)
    net.observe("bytes_received", on=scada, every=every)
    net.observe("bytes_received", on=hmi, every=every)
    return net.to_scenario(duration=duration_ms)


def _summary(metrics: dict[str, list[tuple[float, float]]]) -> dict[str, dict[str, float]]:
    out = {}
    for key in METRICS:
        values = [v for _, v in metrics[key]]
        out[key] = {
            "mean": sum(values) / len(values) if values else math.nan,
            "last": values[-1] if values else math.nan,
        }
    return out


def run_load(
    loads: list[float],
    duration_ms: float,
    engines: list[str],
    label: str = "load",
    out: Path | None = None,
) -> Path:
    env = environment()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = out or RESULTS_DIR / f"{stamp}-{'-'.join(engines)}-{env['host']}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": "smolpy-load",
        "version": 1,
        "label": label,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "scenario": "L1",
        "duration_ms": duration_ms,
        "uplink_mbps": UPLINK_MBPS,
        "protocol_load": protocol_load(),
        "runs": [],
    }
    for rho in loads:
        sc = scenario(rho, duration_ms)
        for engine in engines:
            result = backends.get(engine).run(sc)
            record = {
                "rho": rho,
                "backend": engine,
                "events": result.stats.events,
                "wall_s": result.stats.wall_s,
                "metrics": _summary(result.metrics),
            }
            doc["runs"].append(record)
            out.write_text(json.dumps(doc, indent=1))  # keep partial progress
            m = record["metrics"]
            print(
                f"rho {rho:5.3f} {engine:6s} modbus {m[METRICS[0]]['mean'] / 1000:10.3f} ms"
                f"  mqtt {m[METRICS[1]]['mean'] / 1000:10.3f} ms"
                f"  hmi {m[METRICS[3]]['last']:.4f} MB  ({record['wall_s']:.1f} s)",
                flush=True,
            )
    return out


def _diff(got: float, want: float) -> str:
    if not want or math.isnan(got) or math.isnan(want):
        return "–"
    return f"{100 * (got - want) / want:+.1f} %"


def summarize_load(path: Path, reference: str = "simpy", candidate: str = "ns3") -> str:
    """A markdown table: each metric on the reference and the candidate."""
    doc = json.loads(path.read_text())
    runs = {(r["rho"], r["backend"]): r["metrics"] for r in doc["runs"]}
    loads = sorted({rho for rho, _ in runs})
    lines = [
        f"L1, {doc['duration_ms'] / 1000:g} s, {doc['uplink_mbps']} Mb/s uplink;"
        f" protocols alone: rho = {doc['protocol_load']:.3f}. {reference} → {candidate}.",
        "",
        "| rho | Modbus RTT, mean (ms) | MQTT latency, mean (ms) | to SCADA (MB) | to HMI (MB) |",
        "|---|---|---|---|---|",
    ]
    for rho in loads:
        a, b = runs.get((rho, reference)), runs.get((rho, candidate))
        if a is None or b is None:
            continue
        cells = []
        for key, stat, scale, fmt in (
            (METRICS[0], "mean", 1000, "{:.3f}"),
            (METRICS[1], "mean", 1000, "{:.3f}"),
            (METRICS[2], "last", 1, "{:.4f}"),
            (METRICS[3], "last", 1, "{:.4f}"),
        ):
            want, got = a[key][stat] / scale, b[key][stat] / scale
            cells.append(f"{fmt.format(want)} → {fmt.format(got)} ({_diff(got, want)})")
        lines.append(f"| {rho:.2f} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
