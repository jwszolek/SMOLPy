from __future__ import annotations

import argparse
import runpy
import sys
import time
import traceback

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text
from rich.theme import Theme

_THEME = Theme(
    {
        "banner.title": "bold cyan",
        "banner.sub": "dim white",
        "info": "bold white",
        "success": "bold green",
        "error": "bold red",
        "dim": "dim white",
    }
)

console = Console(theme=_THEME)

_BANNER = Text.assemble(
    ("██████╗ ██╗   ██╗███████╗███╗   ███╗ ██████╗ ██╗\n", "bold cyan"),
    ("██╔══██╗╚██╗ ██╔╝██╔════╝████╗ ████║██╔═══██╗██║\n", "bold cyan"),
    ("██████╔╝ ╚████╔╝ ███████╗██╔████╔██║██║   ██║██║\n", "bold cyan"),
    ("██╔═══╝   ╚██╔╝  ╚════██║██║╚██╔╝██║██║   ██║██║\n", "bold cyan"),
    ("██║        ██║   ███████║██║ ╚═╝ ██║╚██████╔╝███████╗\n", "bold cyan"),
    ("╚═╝        ╚═╝   ╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚══════╝\n", "bold cyan"),
    ("  Network Description Language & Discrete-Event Simulator", "dim white"),
)


def _print_banner() -> None:
    console.print()
    console.print(Panel(_BANNER, border_style="cyan", padding=(0, 2)))
    console.print()


def _cmd_run(script: str, output: str | None, text_mode: bool, extra_args: list[str]) -> None:
    _print_banner()
    console.print(f"  [info]Script[/]   [dim]{script}[/]")
    console.print()

    import os

    if text_mode:
        os.environ["SMOLPY_TEXT_MODE"] = "1"

    start = time.perf_counter()
    namespace: dict = {}

    with Progress(
        SpinnerColumn(spinner_name="dots", style="cyan"),
        TextColumn("[bold white]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Running simulation…", total=None)

        try:
            sys.argv = [script] + extra_args
            namespace = runpy.run_path(script, run_name="__main__")
            progress.update(task, description="Simulation complete")
        except Exception:
            progress.stop()
            console.print()
            console.print(
                Panel(
                    traceback.format_exc(),
                    title="[error]Error[/]",
                    border_style="red",
                    padding=(1, 2),
                )
            )
            sys.exit(1)

    elapsed = time.perf_counter() - start
    console.print()
    console.print(
        Panel(
            f"[success]Finished in {elapsed:.2f}s[/]",
            border_style="green",
            padding=(0, 2),
        )
    )
    console.print()

    if output:
        result = namespace.get("result")
        if result is not None:
            result.export(output)
            console.print(f"  Results exported → {output}")
            console.print()


def _cmd_demo() -> None:
    """Run a built-in 3-client saturation demo in text mode."""
    from smolpy import Network

    _print_banner()
    console.print("  [info]Demo[/]   3 clients → switch → server  (10 s, text mode)\n")

    net = Network("demo")
    sw = net.switch("core-sw", ports=8, mode="store-and-forward")
    server = net.adapter("server", ip="10.0.0.100")
    net.link(server, sw, speed=1_000, length=2)

    fps = int(100_000_000 / (1_518 * 8))
    for i in range(1, 4):
        c = net.adapter(f"client-{i}", ip=f"10.0.0.{i}")
        net.link(c, sw, speed=100, length=10)
        c.sends(to=server, rate=fps, size=1_518, pattern="constant", delay_ms=(i - 1) * 2_000)
        net.observe("bytes_sent", on=c, every=500)

    net.observe("throughput", on=server, every=200)
    net.observe("latency", on=server, every=200)
    net.observe("queue_depth", on=sw, every=200)
    net.observe("bytes_received", on=server, every=500)

    result = net.simulate(duration=10_000, text=True)
    result.report()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smolpy",
        description="SMOLPy — Network simulation DSL and discrete-event simulator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser(
        "run",
        help="Execute a SMOLPy simulation script",
        description="Load and run a Python script that uses the SMOLPy DSL.",
    )
    run_cmd.add_argument("script", help="Path to the .py script to run")
    run_cmd.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="FILE",
        help="Write metric time-series to FILE (.csv or .json)",
    )
    run_cmd.add_argument(
        "--text",
        "-t",
        action="store_true",
        default=False,
        help="Force text-mode dashboard even if the script uses live=True",
    )
    run_cmd.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to the script via sys.argv",
    )

    sub.add_parser(
        "demo",
        help="Run a built-in 3-client saturation demo (no script needed)",
        description="Quick sanity-check: 3 clients ramping onto a shared switch, text-mode output.",
    )

    args = parser.parse_args()

    if args.command == "run":
        # REMAINDER captures flags that appear after the script name, so check both
        text = args.text or "--text" in args.args or "-t" in args.args
        extra = [a for a in args.args if a not in ("--text", "-t")]
        _cmd_run(args.script, args.output, text, extra)
    elif args.command == "demo":
        _cmd_demo()
