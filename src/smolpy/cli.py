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

_THEME = Theme({
    "banner.title": "bold cyan",
    "banner.sub": "dim white",
    "info": "bold white",
    "success": "bold green",
    "error": "bold red",
    "dim": "dim white",
})

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


def _cmd_run(script: str, output: str | None, extra_args: list[str]) -> None:
    _print_banner()
    console.print(f"  [info]Script[/]   [dim]{script}[/]")
    console.print()

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
            console.print(Panel(
                traceback.format_exc(),
                title="[error]Error[/]",
                border_style="red",
                padding=(1, 2),
            ))
            sys.exit(1)

    elapsed = time.perf_counter() - start
    console.print()
    console.print(Panel(
        f"[success]Finished in {elapsed:.2f}s[/]",
        border_style="green",
        padding=(0, 2),
    ))
    console.print()

    if output:
        result = namespace.get("result")
        if result is not None:
            result.export(output)
            console.print(f"  Results exported → {output}")
            console.print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smolpy",
        description="PySMOL — Network simulation DSL and discrete-event simulator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser(
        "run",
        help="Execute a PySMOL simulation script",
        description="Load and run a Python script that uses the PySMOL DSL.",
    )
    run_cmd.add_argument("script", help="Path to the .py script to run")
    run_cmd.add_argument(
        "--output", "-o",
        default=None,
        metavar="FILE",
        help="Write metric time-series to FILE (.csv or .json)",
    )
    run_cmd.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to the script via sys.argv",
    )

    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args.script, args.output, args.args)
