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


def _shape(cfg: dict) -> dict:
    """Sections, their knob lists, and the gate id/kind/key contract — the part
    of the config that must not drift; values are host-owned."""
    return {s: [(g["id"], g["kind"], sorted(g)) for g in cfg[s]] if s == "gates" else list(cfg[s])
            for s in cfg}


def test_console_script_config_matches_the_carve_out_contract():
    """The `devloop` console script resolves the same config *shape* as the
    rail it carved out of. Runs the script installed beside the interpreter
    running pytest, so the console-script wiring itself is under test (not just
    `cli.main`)."""
    script = _script()
    assert script.exists(), f"console script not installed at {script}"
    cfg = _config([str(script)])
    assert list(cfg) == list(GOLDEN)
    assert _shape(cfg) == _shape(GOLDEN)


def test_python_m_devloop_is_the_same_entry_point():
    """`python -m devloop` reaches the same main — the __main__.py form the
    issue names alongside the console script. Byte-equal to each other: the two
    entry points are one rail, whatever the config says."""
    assert _config([sys.executable, "-m", "devloop"]) == _config([str(_script())])
