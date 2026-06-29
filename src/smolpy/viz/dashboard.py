from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import dearpygui.dearpygui as dpg
import networkx as nx

if TYPE_CHECKING:
    from smolpy.dsl.network import Network, SimulationResult

# ── Tunables ──────────────────────────────────────────────────────────────────
_NODE_R = 26
_MARGIN = 88
_DOTS_PER_LINK = 8
_SPEED = 0.38  # link traversals per wall-clock second
_HEADER_H = 120  # px consumed by header + legend + padding
_FONT_SCALE = 2.0  # global UI font scale (makes all DPG text readable)
_LABEL_SIZE = 22  # draw_text size for node labels
_N_CHUNKS = 200  # sim steps for live mode (~8 s wall time)

# ── Colours ───────────────────────────────────────────────────────────────────
_NODE_RGB: dict[str, tuple[int, int, int]] = {
    "Adapter": (72, 140, 230),
    "Switch": (60, 200, 120),
    "Hub": (230, 155, 60),
    "MQTTBroker": (200, 80, 200),
}
_LINK_COLOR = (100, 100, 125, 160)
_PARTICLE_COLOR = (85, 215, 255)
_TEXT_COLOR = (220, 220, 230, 230)
_RECEIVER_COLOR = (255, 180, 50)  # amber ring drawn around receiver nodes

_UNITS = {
    "throughput": "Mb/s",
    "latency": "µs",
    "frame_loss": "%",
    "collision_rate": "/s",
    "queue_depth": "frames",
    "utilization": "%",
    "bytes_sent": "MB",
    "bytes_received": "MB",
    "broker_queue": "msgs",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _compute_layout(
    graph: nx.Graph,
    node_types: dict[str, str],
    w: int,
    h: int,
    margin: int,
) -> dict[str, tuple[float, float]]:
    """Pure-Python layout: infrastructure nodes centre, adapters on outer ring."""
    cx, cy = w / 2, h / 2
    inner_r = min(w, h) * 0.16
    outer_r = min(w, h) / 2 - margin

    infra = [n for n in graph.nodes() if node_types.get(n) in ("Switch", "Hub")]
    leaves = [n for n in graph.nodes() if node_types.get(n) == "Adapter"]
    rest = [n for n in graph.nodes() if n not in infra and n not in leaves]

    pos: dict[str, tuple[float, float]] = {}
    for i, name in enumerate(infra):
        angle = 2 * math.pi * i / max(len(infra), 1) - math.pi / 2
        radius = inner_r if len(infra) > 1 else 0.0
        pos[name] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

    for i, name in enumerate(leaves + rest):
        angle = 2 * math.pi * i / max(len(leaves) + len(rest), 1) - math.pi / 2
        pos[name] = (cx + outer_r * math.cos(angle), cy + outer_r * math.sin(angle))
    return pos


def _canvas_dims(vp_w: int, vp_h: int) -> tuple[int, int, int, int]:
    canvas_w = max(280, int(vp_w * 0.46) - 20)
    canvas_h = max(220, vp_h - _HEADER_H)
    right_w = max(280, vp_w - canvas_w - 60)
    right_h = canvas_h + 28
    return canvas_w, canvas_h, right_w, right_h


def _draw_topology(
    graph: nx.Graph,
    node_types: dict[str, str],
    pos: dict[str, tuple[float, float]],
) -> list[str]:
    """Populate 'topo_canvas'; return pre-allocated animated dot tags."""
    for a, b in graph.edges():
        ax, ay = pos[a]
        bx, by = pos[b]
        dpg.draw_line(
            [ax, ay], [bx, by], color=list(_LINK_COLOR), thickness=2.5, parent="topo_canvas"
        )

    for name, (cx, cy) in pos.items():
        ntype = node_types.get(name, "Adapter")
        r, g, b = _NODE_RGB.get(ntype, _NODE_RGB["Adapter"])
        # Start all nodes dim; the render loop updates fill each frame
        dpg.draw_circle(
            [cx, cy],
            _NODE_R,
            color=[r, g, b, 255],
            fill=[r, g, b, 32],
            parent="topo_canvas",
            tag=f"_nc_{name}",
        )
        # Amber outer ring — visible only when this node is actively receiving
        dpg.draw_circle(
            [cx, cy],
            _NODE_R + 7,
            color=[*_RECEIVER_COLOR, 0],
            fill=[0, 0, 0, 0],
            thickness=3,
            parent="topo_canvas",
            tag=f"_nr_{name}",
        )
        label_x = cx - len(name) * (_LABEL_SIZE * 0.28)
        dpg.draw_text(
            [label_x, cy + _NODE_R + 6],
            name,
            color=list(_TEXT_COLOR),
            size=_LABEL_SIZE,
            parent="topo_canvas",
        )

    n_dots = graph.number_of_edges() * _DOTS_PER_LINK * 2
    dot_tags = []
    for i in range(n_dots):
        tag = f"_dot_{i}"
        dpg.draw_circle(
            [-80, -80],
            4,
            color=[*_PARTICLE_COLOR, 220],
            fill=[*_PARTICLE_COLOR, 190],
            parent="topo_canvas",
            tag=tag,
        )
        dot_tags.append(tag)
    return dot_tags


# ── Core dashboard ────────────────────────────────────────────────────────────


def _run_dashboard(
    network: Network,
    all_samples: dict[str, list],
    sim_state: dict,
    duration_ms: float,
) -> None:
    """Run the Dear PyGui event loop (must be called from the main thread)."""
    graph = network._graph
    node_types = {n: type(node).__name__ for n, node in network._nodes.items()}
    links = list(graph.edges())

    # metric_name → {target_name: [(t, v), ...]}
    metric_groups: defaultdict[str, list[str]] = defaultdict(list)
    for key in all_samples:
        metric, _, target = key.partition(":")
        if target not in metric_groups[metric]:
            metric_groups[metric].append(target)

    # ── DPG context ──────────────────────────────────────────────────────────
    dpg.create_context()
    dpg.set_global_font_scale(_FONT_SCALE)

    dpg.create_viewport(
        title=f"PySMOL  ·  {network.name}",
        width=1280,
        height=800,
        min_width=860,
        min_height=540,
    )
    dpg.setup_dearpygui()
    dpg.maximize_viewport()

    init_w, init_h = 1280, 800
    c_w, c_h, r_w, r_h = _canvas_dims(init_w, init_h)
    n_metrics = len(metric_groups)
    plot_h_init = max(130, (c_h - n_metrics * 14) // max(n_metrics, 1))

    # series_ids[full_key] = dpg series item id
    series_ids: dict[str, int] = {}
    # axis ids for fit_axis_data
    x_axes: dict[str, int] = {}
    y_axes: dict[str, int] = {}
    plot_tags: list[str] = []

    live_mode = not sim_state.get("done", True)

    # ── Simulation control callbacks (live mode only) ─────────────────────────
    def _toggle_pause(sender, app_data, user_data=None) -> None:
        if sim_state.get("done", False):
            return
        paused = not sim_state.get("paused", False)
        sim_state["paused"] = paused
        if paused:
            dpg.configure_item("btn_pause", label=" ▶  Resume ")
            dpg.set_value("status_text", "● Paused")
            dpg.configure_item("status_text", color=[200, 140, 60, 255])
        else:
            dpg.configure_item("btn_pause", label=" ⏸  Pause ")
            dpg.set_value("status_text", "● Simulating…")
            dpg.configure_item("status_text", color=[255, 200, 60, 255])

    def _stop_sim(sender, app_data, user_data=None) -> None:
        if sim_state.get("done", False):
            return
        sim_state["paused"] = False  # unblock thread so it can exit cleanly
        sim_state["stop"] = True
        dpg.configure_item("btn_pause", enabled=False)
        dpg.configure_item("btn_stop", enabled=False)

    with dpg.window(tag="main", no_title_bar=True, no_move=True, no_resize=True, no_scrollbar=True):
        with dpg.group(horizontal=True):
            dpg.add_text(
                f"  PySMOL  ·  {network.name}  ·  {duration_ms:,.0f} ms", color=[110, 190, 255, 255]
            )
            dpg.add_spacer(width=20)
            dpg.add_text(
                "● Simulating…" if live_mode else "● Done",
                tag="status_text",
                color=[255, 200, 60, 255] if live_mode else [80, 220, 120, 255],
            )
            if live_mode:
                dpg.add_spacer(width=20)
                dpg.add_button(label=" ⏸  Pause ", tag="btn_pause", callback=_toggle_pause)
                dpg.add_spacer(width=8)
                dpg.add_button(label=" ⏹  Stop  ", tag="btn_stop", callback=_stop_sim)

        if live_mode:
            dpg.add_progress_bar(default_value=0.0, width=-1, tag="progress_bar")
        dpg.add_separator()
        dpg.add_spacer(height=4)

        with dpg.group(horizontal=True):
            # ── Left: topology ────────────────────────────────────────────
            with dpg.group():
                dpg.add_text("Network Topology", color=[165, 165, 198])
                with dpg.drawlist(width=c_w, height=c_h, tag="topo_canvas"):
                    pass
                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    for label, (r, g, b) in _NODE_RGB.items():
                        dpg.add_text(f"● {label}   ", color=[r, g, b, 255])

            dpg.add_spacer(width=18)

            # ── Right: metric plots ───────────────────────────────────────
            with dpg.group():
                dpg.add_text("Simulation Metrics", color=[165, 165, 198])
                with dpg.child_window(width=r_w, height=r_h, border=False, tag="metrics_panel"):
                    for idx, (metric_name, targets) in enumerate(metric_groups.items()):
                        unit = _UNITS.get(metric_name, "")
                        label = f"{metric_name}  ({unit})" if unit else metric_name
                        plot_tag = f"_plot_{idx}"
                        plot_tags.append(plot_tag)
                        with dpg.plot(label=label, height=plot_h_init, width=-1, tag=plot_tag):
                            dpg.add_plot_legend()
                            x_ax = dpg.add_plot_axis(dpg.mvXAxis, label="time (ms)")
                            y_ax = dpg.add_plot_axis(dpg.mvYAxis, label=unit)
                            for tgt in targets:
                                full_key = f"{metric_name}:{tgt}"
                                init_samples = all_samples.get(full_key, [])
                                sid = dpg.add_line_series(
                                    [t for t, _ in init_samples],
                                    [v for _, v in init_samples],
                                    label=tgt,
                                    parent=y_ax,
                                )
                                series_ids[full_key] = sid
                            x_axes[metric_name] = x_ax
                            y_axes[metric_name] = y_ax
                            dpg.fit_axis_data(x_ax)
                            dpg.fit_axis_data(y_ax)
                        dpg.add_spacer(height=6)

    # Mutable state updated on resize
    pos: dict[str, tuple[float, float]] = {}
    dot_tags: list[str] = []

    def _rebuild(canvas_w: int, canvas_h: int) -> None:
        nonlocal pos, dot_tags
        dpg.delete_item("topo_canvas", children_only=True)
        pos = _compute_layout(graph, node_types, canvas_w, canvas_h, _MARGIN)
        dot_tags = _draw_topology(graph, node_types, pos)

    _rebuild(c_w, c_h)
    dpg.set_primary_window("main", True)
    dpg.show_viewport()

    last_vp: tuple[int, int] = (0, 0)
    t0 = time.perf_counter()

    while dpg.is_dearpygui_running():
        vp_w = dpg.get_viewport_width()
        vp_h = dpg.get_viewport_height()

        # ── Resize ───────────────────────────────────────────────────────
        if (vp_w, vp_h) != last_vp:
            last_vp = (vp_w, vp_h)
            c_w, c_h, r_w, r_h = _canvas_dims(vp_w, vp_h)

            dpg.configure_item("topo_canvas", width=c_w, height=c_h)
            dpg.configure_item("metrics_panel", width=r_w, height=r_h)

            if n_metrics > 0:
                plot_h = max(110, (c_h - n_metrics * 18) // n_metrics)
                for pt in plot_tags:
                    dpg.configure_item(pt, height=plot_h)

            _rebuild(c_w, c_h)

        # ── Live updates (plots + status) ─────────────────────────────────
        if live_mode:
            progress = float(sim_state.get("progress", 0.0))
            done = bool(sim_state.get("done", False))
            stopped = bool(sim_state.get("stop", False))

            dpg.configure_item("progress_bar", default_value=progress)
            if done:
                label = "● Stopped" if stopped else "● Done"
                color = [220, 80, 80, 255] if stopped else [80, 220, 120, 255]
                dpg.set_value("status_text", label)
                dpg.configure_item("status_text", color=color)
                dpg.configure_item("btn_pause", enabled=False)
                dpg.configure_item("btn_stop", enabled=False)

            for full_key, sid in series_ids.items():
                samples = all_samples.get(full_key, [])
                if samples:
                    dpg.set_value(sid, [[t for t, _ in samples], [v for _, v in samples]])

            for metric_name in metric_groups:
                if series_ids:  # at least one series exists
                    dpg.fit_axis_data(x_axes[metric_name])
                    dpg.fit_axis_data(y_axes[metric_name])

        # ── Node activity highlighting ────────────────────────────────────
        now = time.perf_counter() - t0

        sending: set[str] = set()  # transmitting — pulsing fill
        receiving: set[str] = set()  # receiving data — amber outer ring
        active: set[str] = set()  # forwarding — solid fill

        for key, samples in all_samples.items():
            if not samples:
                continue
            metric, _, target = key.partition(":")
            last_val = samples[-1][1]
            if metric == "bytes_sent" and last_val > 0.001:
                sending.add(target)
                active.add(target)
            elif metric == "bytes_received" and last_val > 0.001:
                receiving.add(target)
                active.add(target)
            elif metric == "throughput" and last_val > 0.001:
                active.add(target)

        if active:
            for name, ntype in node_types.items():
                if ntype in ("Switch", "Hub"):
                    active.add(name)

        for name in network._nodes:
            r, g, b = _NODE_RGB.get(node_types.get(name, "Adapter"), _NODE_RGB["Adapter"])
            # Inner fill — pulsing for senders, solid for forwarders, dim when idle
            if name in sending:
                alpha = int(180 + 60 * math.sin(now * 4.0))
                dpg.configure_item(f"_nc_{name}", fill=[r, g, b, alpha])
            elif name in active:
                dpg.configure_item(f"_nc_{name}", fill=[r, g, b, 180])
            else:
                dpg.configure_item(f"_nc_{name}", fill=[r, g, b, 32])
            # Amber outer ring — visible only on receivers that are not also senders
            if name in receiving and name not in sending:
                ring_a = int(160 + 60 * math.sin(now * 2.0))  # slower pulse
                dpg.configure_item(f"_nr_{name}", color=[*_RECEIVER_COLOR, ring_a])
            else:
                dpg.configure_item(f"_nr_{name}", color=[*_RECEIVER_COLOR, 0])

        # ── Particle animation ────────────────────────────────────────────
        dot_idx = 0
        for a, b in links:
            ax, ay = pos[a]
            bx, by = pos[b]
            for i in range(_DOTS_PER_LINK):
                phase = (now * _SPEED + i / _DOTS_PER_LINK) % 1.0
                dpg.configure_item(
                    dot_tags[dot_idx], center=[ax + phase * (bx - ax), ay + phase * (by - ay)]
                )
                dot_idx += 1
                phase = (now * _SPEED + (i + 0.5) / _DOTS_PER_LINK) % 1.0
                dpg.configure_item(
                    dot_tags[dot_idx], center=[bx + phase * (ax - bx), by + phase * (ay - by)]
                )
                dot_idx += 1

        dpg.render_dearpygui_frame()

    dpg.destroy_context()


# ── Public API ────────────────────────────────────────────────────────────────


def show(result: SimulationResult) -> None:
    """Open dashboard for a completed simulation result (static view)."""
    total_ms = max(
        (t for s in result.metrics.values() for t, _ in s),
        default=0.0,
    )
    _run_dashboard(
        result._network,
        result.metrics,  # type: ignore[arg-type]
        {"done": True, "progress": 1.0},
        total_ms,
    )


def show_live(network: Network, duration_ms: float) -> SimulationResult:
    """Open dashboard immediately; run simulation in background thread.

    Returns the completed SimulationResult when the user closes the window
    (or after the simulation finishes, whichever is later).
    """
    from smolpy.sim.engine import run_simulation

    # Pre-populate keys so the GUI can create plot series before data arrives
    all_samples: dict[str, list] = {
        f"{obs.metric}:{obs.target.name}": [] for obs in network._observations
    }
    sim_state: dict = {"progress": 0.0, "done": False, "result": None}

    def _run() -> None:
        result = run_simulation(
            network,
            duration_ms,
            _all_samples=all_samples,  # type: ignore[arg-type]
            _sim_state=sim_state,
            _n_chunks=_N_CHUNKS,
        )
        sim_state["result"] = result

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    _run_dashboard(network, all_samples, sim_state, duration_ms)

    thread.join(timeout=60)

    result = sim_state.get("result")
    if result is None:
        from smolpy.dsl.network import SimulationResult

        result = SimulationResult(metrics=all_samples, network=network)  # type: ignore[arg-type]
    return result
