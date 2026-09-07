"""Seams for ``devloop pack`` (funloops#28, dec-fd12489d, dec-d2de831e).

Two seams, both at the verb's stdout:

- **the pack** — with codegraph replaced by a fake binary that replays
  captured CLI output, the implementer pack is byte-pinned to a golden and
  byte-identical across two runs;
- **the degraded path** — ``CODEGRAPH_BIN`` at a nonexistent path prints
  the marked degraded block and exits 0 (never blocks).

Fixtures under ``fixtures/pack/``: ``repo/`` is the tree the captured output
describes; ``files.txt``, ``context.txt``, ``node-*.txt`` are codegraph
1.6.0's literal stdout over it (``init -y``, then ``files`` / ``context
--no-code <title>`` / ``node -f <file> --symbols-only``); ``issue.md`` is the
issue body; ``persona.md`` / ``constitution.md`` are two-line stand-ins for
the packaged docs so the golden pins the pack's composition, not their
prose; ``implementer.md`` is the golden, captured once and hand-checked.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from devloop import cli, github
from devloop.cli import main

FIX = Path(__file__).resolve().parent / "fixtures" / "pack"
TITLE = "fx: annotate the catalog with docstrings"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """The fixture tree in a repo of its own (the ``.git`` marker stops the
    constitution walk), the packaged docs swapped for the fixture stand-ins,
    and ``gh`` replaced by the fixture issue."""
    root = tmp_path / "repo"
    shutil.copytree(FIX / "repo", root)
    (root / ".git").mkdir()
    monkeypatch.setattr(cli, "PACKAGE_PERSONA", FIX / "persona.md")
    monkeypatch.setattr(cli, "PACKAGE_CONSTITUTION", FIX / "constitution.md")
    issue = json.dumps({"title": TITLE,
                        "body": (FIX / "issue.md").read_text(encoding="utf-8")})
    monkeypatch.setattr(github, "run", lambda args: issue)
    return root


def fake_codegraph(tmp_path):
    """Replays the captured output per verb; logs every verb it is asked for.
    argv is ``--no-color <verb> …``; for ``node`` the file is argv[6]."""
    log = tmp_path / "verbs.log"
    script = tmp_path / "codegraph"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$2" >> "{log}"\n'
        'case "$2" in\n'
        "  init|sync) ;;\n"
        f'  files) cat "{FIX}/files.txt" ;;\n'
        f'  context) cat "{FIX}/context.txt" ;;\n'
        f'  node) cat "{FIX}/node-$(basename "$6" .py).txt" ;;\n'
        '  *) echo "unexpected verb $2" >&2; exit 1 ;;\n'
        "esac\n")
    script.chmod(0o755)
    return script, log


def test_implementer_pack_is_golden_and_byte_stable(repo, tmp_path, capsys):
    binary, log = fake_codegraph(tmp_path)
    argv = ["pack", "7", "--role", "implementer", "--cwd", str(repo),
            "--codegraph-bin", str(binary)]
    assert main(argv) == 0
    first = capsys.readouterr().out
    assert first == (FIX / "implementer.md").read_text(encoding="utf-8")
    # no index in a fresh worktree → init; then the tiers in order, and one
    # node call per file the issue names (helper first: first mention wins)
    assert log.read_text().split() == ["init", "files", "context", "node", "node"]
    assert main(argv) == 0
    assert capsys.readouterr().out == first


def test_missing_codegraph_degrades_and_exits_zero(repo, monkeypatch, capsys):
    monkeypatch.setenv("CODEGRAPH_BIN", str(repo / "no-such-codegraph"))
    assert main(["pack", "7", "--role", "implementer", "--cwd", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out and "gather" in out
    assert "Project Structure" not in out
    assert "Fixture persona" in out  # the rest of the pack still ships


def test_judge_pack_carries_no_persona(repo, tmp_path, capsys):
    binary, _ = fake_codegraph(tmp_path)
    assert main(["pack", "7", "--role", "judge", "--cwd", str(repo),
                 "--codegraph-bin", str(binary)]) == 0
    out = capsys.readouterr().out
    assert "Fixture persona" not in out and "Fixture constitution" not in out
    assert "Project Structure" in out and TITLE in out
