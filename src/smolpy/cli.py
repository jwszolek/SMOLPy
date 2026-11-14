from __future__ import annotations

import argparse
import pathlib
import runpy
import sys
import time
import traceback
from typing import TYPE_CHECKING, NoReturn

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from smolpy.model import Scenario

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
    ("███████╗███╗   ███╗ ██████╗ ██╗     ██████╗ ██╗   ██╗\n", "bold cyan"),
    ("██╔════╝████╗ ████║██╔═══██╗██║     ██╔══██╗╚██╗ ██╔╝\n", "bold cyan"),
    ("███████╗██╔████╔██║██║   ██║██║     ██████╔╝ ╚████╔╝ \n", "bold cyan"),
    ("╚════██║██║╚██╔╝██║██║   ██║██║     ██╔═══╝   ╚██╔╝  \n", "bold cyan"),
    ("███████║██║ ╚═╝ ██║╚██████╔╝███████╗██║        ██║   \n", "bold cyan"),
    ("╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚══════╝╚═╝        ╚═╝   \n", "bold cyan"),
    ("  Network Description Language & Discrete-Event Simulator", "dim white"),
)


def _print_banner() -> None:
    console.print()
    console.print(Panel(_BANNER, border_style="cyan", padding=(0, 2)))
    console.print()


def _error(message: str) -> NoReturn:
    console.print()
    console.print(Panel(message, title="[error]Error[/]", border_style="red", padding=(1, 2)))
    sys.exit(1)


def _cmd_run(
    script: str,
    output: str | None,
    text_mode: bool,
    extra_args: list[str],
    runs: int | None = None,
    workers: int | None = None,
) -> None:
    _print_banner()
    console.print(f"  [info]Script[/]   [dim]{script}[/]")
    console.print()

    import os

    if text_mode:
        os.environ["SMOLPY_TEXT_MODE"] = "1"

    is_smol = pathlib.Path(script).suffix == ".smol"
    if not is_smol and (runs is not None or workers is not None):
        _error("--runs and --workers apply to .smol files; in a Python script use run_many()")

    if is_smol:
        from smolpy.lang import SMOLSemanticError, SMOLSyntaxError, load_smol

        try:
            smol = load_smol(pathlib.Path(script))
        except (SMOLSyntaxError, SMOLSemanticError) as exc:
            _error(str(exc))
        n_runs = runs if runs is not None else smol.runs
        if n_runs > 1:
            if text_mode:
                _error(f"--text shows a single run; it cannot be used with {n_runs} runs")
            if smol.mode != "headless":
                _error(f"{n_runs} runs need mode=headless; the file sets mode={smol.mode}")
            _run_batch(smol.scenario, n_runs, workers, output)
            return

    start = time.perf_counter()
    result = None

    with Progress(
        SpinnerColumn(spinner_name="dots", style="cyan"),
        TextColumn("[bold white]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Running simulation…", total=None)

        try:
            if is_smol:
                from smolpy.lang import SMOLSemanticError, SMOLSyntaxError, interpret_and_run

                try:
                    result = interpret_and_run(pathlib.Path(script))
                except (SMOLSyntaxError, SMOLSemanticError) as exc:
                    progress.stop()
                    _error(str(exc))
            else:
                sys.argv = [script] + extra_args
                namespace = runpy.run_path(script, run_name="__main__")
                result = namespace.get("result")
            progress.update(task, description="Simulation complete")
        except Exception:
            progress.stop()
            _error(traceback.format_exc())

    _finished(time.perf_counter() - start)

    if output and result is not None:
        result.export(output)
        console.print(f"  Results exported → {output}")
        console.print()


def _run_batch(scenario: Scenario, runs: int, workers: int | None, output: str | None) -> None:
    """Run *scenario* *runs* times (ADR-0009), print the summary across runs,
    export every run, and exit with status 1 if any run failed."""
    from smolpy.batch import run_many

    start = time.perf_counter()
    with Progress(
        SpinnerColumn(spinner_name="dots", style="cyan"),
        TextColumn("[bold white]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(f"Running {runs} simulations…", total=None)
        try:
            batch = run_many(scenario, runs, workers=workers)
        except Exception:
            progress.stop()
            _error(traceback.format_exc())
        progress.update(task, description=f"{runs} simulations complete")

    _finished(time.perf_counter() - start)
    batch.report()
    if output:
        batch.export(output)
        console.print(f"  Runs exported → {output}")
        console.print()
    if batch.failures:
        sys.exit(1)


def _finished(elapsed: float) -> None:
    console.print()
    console.print(
        Panel(
            f"[success]Finished in {elapsed:.2f}s[/]",
            border_style="green",
            padding=(0, 2),
        )
    )
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


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text!r}")
    return value


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="FILE",
        help="Write metric time-series to FILE (.csv or .json); every run of a batch",
    )
    parser.add_argument(
        "--text",
        "-t",
        action="store_true",
        default=False,
        help="Force text-mode dashboard even if the script uses live=True",
    )
    parser.add_argument(
        "--runs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Run a .smol file N times with seeds seed, seed+1, ... (overrides runs=)",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Worker processes for --runs (default: one per CPU)",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smolpy",
        description="SMOLPy — Network simulation DSL and discrete-event simulator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser(
        "run",
        help="Execute a SMOLPy simulation script",
        description="Load and run a Python script or .smol file that uses the SMOLPy DSL.",
    )
    run_cmd.add_argument("script", help="Path to the .py script or .smol file to run")
    _add_run_options(run_cmd)
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
        # REMAINDER captures the options written after the script name.
        if pathlib.Path(args.script).suffix == ".smol":
            # A .smol file takes no arguments of its own: every one is an option.
            after = argparse.ArgumentParser(prog=f"smolpy run {args.script}")
            _add_run_options(after)
            late = after.parse_args(args.args)
            _cmd_run(
                args.script,
                late.output or args.output,
                args.text or late.text,
                [],
                late.runs if late.runs is not None else args.runs,
                late.workers if late.workers is not None else args.workers,
            )
        else:
            text = args.text or "--text" in args.args or "-t" in args.args
            extra = [a for a in args.args if a not in ("--text", "-t")]
            _cmd_run(args.script, args.output, text, extra, args.runs, args.workers)
    elif args.command == "demo":
        _cmd_demo()
