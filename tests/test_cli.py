"""smolpy run with runs= and --runs, --workers, -o (ADR-0009, step 3)."""

from __future__ import annotations

import csv
import subprocess
import sys

import pytest

_SMOL = """
network "cell" {
    adapter a ip=10.0.0.1
    adapter b ip=10.0.0.2
    switch  sw ports=2
    link a -- sw speed=100 length=10
    link b -- sw speed=100 length=10
    flow a -> b rate=500 size=imix pattern=poisson
    observe bytes_received on b every=50
    simulate duration=200 seed=7%s
}
"""


def _smolpy(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from smolpy.cli import main; main()", "run", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _runs(path) -> set[tuple[str, str]]:
    with open(path, encoding="utf-8") as fh:
        return {(r["run"], r["seed"]) for r in csv.DictReader(fh)}


def test_runs_in_the_file(tmp_path) -> None:
    f = tmp_path / "cell.smol"
    f.write_text(_SMOL % " runs=3")
    done = _smolpy(str(f), "--workers", "2", "-o", str(tmp_path / "runs.csv"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 of 3 runs succeeded" in done.stdout
    assert "bytes_received:b" in done.stdout
    assert _runs(tmp_path / "runs.csv") == {("0", "7"), ("1", "8"), ("2", "9")}


def test_runs_option_overrides_the_file_before_or_after_the_script(tmp_path) -> None:
    f = tmp_path / "cell.smol"
    f.write_text(_SMOL % " runs=3")
    out = tmp_path / "runs.csv"
    assert _smolpy("--runs", "2", "--workers", "1", "-o", str(out), str(f)).returncode == 0
    assert _runs(out) == {("0", "7"), ("1", "8")}
    assert _smolpy(str(f), "--runs", "2", "--workers", "1", "-o", str(out)).returncode == 0
    assert _runs(out) == {("0", "7"), ("1", "8")}


def test_one_run_exports_a_single_result(tmp_path) -> None:
    f = tmp_path / "cell.smol"
    f.write_text(_SMOL % " runs=3")
    out = tmp_path / "one.csv"
    assert _smolpy(str(f), "--runs", "1", "-o", str(out)).returncode == 0
    with open(out, encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == ["time_ms", "metric", "value"]


@pytest.mark.parametrize(
    "extra, args, message",
    [
        (" runs=3", ["--text"], "--text shows a single run"),
        (" mode=text", ["--runs", "3"], "3 runs need mode=headless; the file sets mode=text"),
        ("", ["--runs", "0"], "--runs: must be a positive integer, got '0'"),
        ("", ["--workers", "x"], "--workers: must be a positive integer, got 'x'"),
        ("", ["--seed", "3"], "unrecognized arguments: --seed 3"),
        (" run=3", [], "'simulate' has no argument 'run'"),
    ],
)
def test_rejects(tmp_path, extra: str, args: list[str], message: str) -> None:
    f = tmp_path / "cell.smol"
    f.write_text(_SMOL % extra)
    done = _smolpy(str(f), *args)
    assert done.returncode != 0
    assert message in " ".join((done.stdout + done.stderr).split())


def test_runs_apply_to_smol_files_only(tmp_path) -> None:
    script = tmp_path / "s.py"
    script.write_text("import sys\nprint('args', sys.argv[1:])\n")
    done = _smolpy("--runs", "3", str(script))
    assert done.returncode == 1
    assert "--runs and --workers apply to .smol files" in " ".join(done.stdout.split())
    # after a Python script, the options are the script's own
    done = _smolpy(str(script), "--runs", "3")
    assert done.returncode == 0 and "args ['--runs', '3']" in done.stdout
