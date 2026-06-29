"""Rich text-mode live dashboard — no GUI required."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from smolpy.dsl.network import Network, SimulationResult

_N_CHUNKS = 200

_UNITS: dict[str, str] = {
    "throughput":    "Mb/s",
    "latency":       "µs",
    "frame_loss":    "%",
    "bytes_sent":    "MB",
    "bytes_received": "MB",
    "queue_depth":   "fr",
    "utilization":   "%",
    "collision_rate": "/s",
    "broker_queue":  "msgs",
}


def _unit(key: str) -> str:
    metric = key.partition(":")[0]
    return _UNITS.get(metric, "")


def _bar(fraction: float, width: int = 30) -> Text:
    filled = int(fraction * width)
    empty  = width - filled
    t = Text()
    t.append("█" * filled, style="bold cyan")
    t.append("░" * empty,  style="dim white")
    return t


def _build_display(
    network: Network,
    all_samples: dict[str, list],
    sim_state: dict,
    duration_ms: float,
    t0: float,
) -> Panel:
    progress   = sim_state.get("progress", 0.0)
    done       = sim_state.get("done", False)
    sim_t_s    = progress * duration_ms / 1_000
    wall_s     = time.perf_counter() - t0
    status_str = "● Done" if done else "● Simulating…"

    # ── progress row ──────────────────────────────────────────────────────
    bar   = _bar(progress)
    pct   = Text(f" {int(progress * 100):3d}%  ", style="bold white")
    sim_t = Text(f"t={sim_t_s:.1f}s / {duration_ms/1_000:.0f}s", style="dim white")
    wall  = Text(f"  ⏱ {wall_s:.1f}s", style="dim white")
    status = Text(f"  {status_str}", style="bold cyan" if not done else "bold green")

    progress_line = Text.assemble(bar, pct, sim_t, wall, status)

    # ── metrics table ─────────────────────────────────────────────────────
    tbl = Table(box=None, padding=(0, 2), show_header=True, header_style="bold white")
    tbl.add_column("Metric",  style="cyan",       no_wrap=True, min_width=30)
    tbl.add_column("Latest",  style="bold white",  justify="right", min_width=10)
    tbl.add_column("Avg",     style="white",        justify="right", min_width=10)
    tbl.add_column("Unit",    style="dim white",    min_width=6)

    for key in sorted(all_samples):
        samples = all_samples[key]
        if not samples:
            tbl.add_row(key, "—", "—", _unit(key))
            continue
        vals   = [v for _, v in samples]
        latest = vals[-1]
        avg    = sum(vals) / len(vals)
        tbl.add_row(key, f"{latest:.3f}", f"{avg:.3f}", _unit(key))

    body = Text.assemble(progress_line, "\n\n")
    return Panel(
        Columns([body, tbl], equal=False),
        title=f"[bold cyan]SMOLPy[/] — [white]{network.name}[/]",
        border_style="cyan" if not done else "green",
        padding=(1, 2),
    )


def show_text(network: Network, duration_ms: float) -> SimulationResult:
    """Run simulation in background thread; display live metric table in terminal."""
    from smolpy.sim.engine import run_simulation

    all_samples: dict[str, list] = {
        f"{obs.metric}:{obs.target.name}": []
        for obs in network._observations
    }
    sim_state: dict = {"progress": 0.0, "done": False, "result": None}

    def _run() -> None:
        result = run_simulation(
            network, duration_ms,
            _all_samples=all_samples,   # type: ignore[arg-type]
            _sim_state=sim_state,
            _n_chunks=_N_CHUNKS,
        )
        sim_state["result"] = result

    t0 = time.perf_counter()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    console = Console()
    with Live(console=console, refresh_per_second=4, screen=False) as live:
        while not sim_state.get("done", False):
            live.update(_build_display(network, all_samples, sim_state, duration_ms, t0))
            time.sleep(0.25)
        # One final render with done state
        live.update(_build_display(network, all_samples, sim_state, duration_ms, t0))

    thread.join(timeout=60)

    result = sim_state.get("result")
    if result is None:
        from smolpy.dsl.network import SimulationResult
        result = SimulationResult(metrics=all_samples, network=network)  # type: ignore[arg-type]
    return result
