"""The seams this repo's carve-out introduces: the entry points.

``golden_config.json`` is the independent source of truth: it is the literal
stdout of thinkweave's ``python scripts/issue_loop.py config`` at carve-out
(thinkweave@de35d9c), captured before this package could run at all.

S1's criterion was a byte comparison against it. #149 retires that: funloops now
owns a real ``loop.toml`` describing *funloops* (its own test command, its own
sensitive paths), so byte-equality with another repo's resolved config is no
longer a property worth having — a pass would mean funloops was still
configured as thinkweave. What survives is the part the golden actually
witnesses: the resolved-config **contract shape** — which sections exist, which
knobs each carries, which gates run in which order with which kind. Host-owned
*values* are free to differ; the shape is not.

Growing the contract (a new knob, a new gate) means **extending** this file to
match — it is a recorded shape, not an unreproducible artifact, and a failure
here is the reminder to update it deliberately rather than a reason to delete
the test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "golden_config.json").read_text(encoding="utf-8"))


def _config(argv: list[str]) -> dict:
    out = subprocess.run([*argv, "config"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _script() -> Path:
    return Path(sys.executable).parent / "devloop"


def test_console_script_config_matches_the_carve_out_contract():
    """The `devloop` console script resolves the same config *shape* as the
    rail it carved out of. Runs the script installed beside the interpreter
    running pytest, so the console-script wiring itself is under test (not just
    `cli.main`)."""
    script = _script()
    assert script.exists(), f"console script not installed at {script}"
    cfg = _config([str(script)])

    assert list(cfg) == list(GOLDEN)
    for section in (s for s in GOLDEN if s != "gates"):
        assert list(cfg[section]) == list(GOLDEN[section]), section
    assert [(g["id"], g["kind"]) for g in cfg["gates"]] == \
           [(g["id"], g["kind"]) for g in GOLDEN["gates"]]
    # Same gate *keys* too — a gate that quietly lost its threshold or its
    # rerun list is a contract break the id/kind pair would not catch.
    for got, want in zip(cfg["gates"], GOLDEN["gates"], strict=True):
        assert set(got) == set(want), got["id"]


def test_python_m_devloop_is_the_same_entry_point():
    """`python -m devloop` reaches the same main — the __main__.py form the
    issue names alongside the console script. Byte-equal to each other: the two
    entry points are one rail, whatever the config says."""
    assert _config([sys.executable, "-m", "devloop"]) == _config([str(_script())])
