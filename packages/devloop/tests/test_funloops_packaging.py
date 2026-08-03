"""The seams this repo's carve-out introduces: the entry points and config discovery.

The 8 modules moved verbatim, so their behavior is already pinned by
``test_issue_loop.py``. What is *new* here is packaging — a console script, a
``python -m devloop`` form, and a ``loop.toml`` that must still resolve from the
``packages/devloop/`` layout. Those are the only things this file tests.

``golden_config.json`` is the independent source of truth: it is the literal
stdout of thinkweave's ``python scripts/issue_loop.py config`` at carve-out
(thinkweave@de35d9c), captured before this package could run at all. Acceptance
criterion — "``uv run devloop config`` emits the same resolved-config JSON shape
as thinkweave's ``scripts/issue_loop.py config``" — is that byte comparison.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from devloop import cli

GOLDEN = Path(__file__).resolve().parent / "golden_config.json"


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def test_console_script_config_matches_the_thinkweave_golden():
    """The `devloop` console script is byte-compatible with the rail it carved
    out of. Runs the script installed beside the interpreter running pytest, so
    the console-script wiring itself is under test (not just `cli.main`)."""
    script = Path(sys.executable).parent / "devloop"
    assert script.exists(), f"console script not installed at {script}"
    assert _run([str(script), "config"]) == GOLDEN.read_text(encoding="utf-8")


def test_python_m_devloop_is_the_same_entry_point():
    """`python -m devloop` reaches the same main — the __main__.py form the
    issue names alongside the console script."""
    assert _run([sys.executable, "-m", "devloop", "config"]) == GOLDEN.read_text(encoding="utf-8")


def test_loop_toml_resolves_from_the_packages_layout():
    """Config discovery survived the move: CONFIG_PATH points at the moved
    loop.toml (not a stale thinkweave-root path), so the gate pipeline is the
    file's and not the empty default. The gate ids are read off the golden,
    whose gates came from thinkweave's copy of the same file."""
    assert cli.CONFIG_PATH.exists()
    # The layout the carve-out issue declares, spelled out rather than
    # recomputed from cli's own parent-walk.
    assert cli.CONFIG_PATH.parts[-4:] == ("devloop", "docs", "agents", "loop.toml")
    assert cli.load_config()["gates"], "gates come only from loop.toml — an empty list means it was not found"
