"""Seams for ``devloop pack`` (funloops#28, dec-fd12489d, dec-d2de831e).

Two seams, both at the verb's stdout:

- **the pack** — with codegraph replaced by a fake binary that replays
  captured CLI output, the implementer pack is byte-pinned to a golden and
  byte-identical across two runs;
- **the degraded path** — a missing binary, a verb that exits non-zero, or a
  read verb that prints nothing all print the marked degraded block and exit
  0 (never block, never a clean empty map).

Fixtures under ``fixtures/pack/``: ``repo/`` is the tree the captured output
describes; ``files.json``, ``context.txt``, ``node-*.txt`` are codegraph
1.6.0's literal stdout over it (``init -y``, then ``files -j`` / ``context
--no-code <title>`` / ``node -f <file> --symbols-only``); ``issue.md`` is the
issue body; ``persona.md`` / ``constitution.md`` are two-line stand-ins for
the packaged docs (the persona stand-in carries the constitution marker, as
the real one does) so the golden pins the pack's composition, not their
prose; ``implementer.md`` is the golden, captured once and hand-checked. The
REAL persona + rules + overlay splice is pinned in test_constitution.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from devloop import cli, github, pack

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
    monkeypatch.setattr(github, "run", lambda args, cwd=None: issue)
    return root


def fake_binary(tmp_path, code: str) -> Path:
    """An executable running ``code`` under this interpreter, on any OS:
    a .cmd launcher on Windows, a sh launcher elsewhere."""
    script = tmp_path / "fake_codegraph.py"
    script.write_text(code, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "codegraph.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "codegraph"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def fake_codegraph(tmp_path):
    """Replays the captured output per verb; logs every verb it is asked for.
    argv is ``--no-color <verb> …``; for ``node`` the file follows ``-f``."""
    log = tmp_path / "verbs.log"
    binary = fake_binary(tmp_path, f"""\
import sys
from pathlib import Path
verb, args = sys.argv[2], sys.argv[3:]
print(verb, file=open({str(log)!r}, "a"))
if verb in ("init", "sync"):
    sys.exit(0)
fix = Path({str(FIX)!r})
if verb == "node":
    out = fix / f"node-{{Path(args[args.index('-f') + 1]).stem}}.txt"
elif verb == "files" and "-j" in args:
    out = fix / "files.json"
elif verb == "context":
    out = fix / "context.txt"
else:
    sys.exit(f"unexpected verb {{verb}}")
sys.stdout.write(out.read_text(encoding="utf-8"))
""")
    return binary, log


def test_implementer_pack_is_golden_and_byte_stable(repo, tmp_path, capsys):
    binary, log = fake_codegraph(tmp_path)
    argv = ["pack", "7", "--role", "implementer", "--cwd", str(repo),
            "--codegraph-bin", str(binary)]
    assert cli.main(argv) == 0
    first = capsys.readouterr().out
    assert first == (FIX / "implementer.md").read_text(encoding="utf-8")
    # no index in a fresh worktree → init; then the tiers in order, and one
    # node call per file the issue names (helper first: first mention wins)
    assert log.read_text().split() == ["init", "files", "context", "node", "node"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == first


def test_render_tree_draws_depth_and_siblings_from_paths(tmp_path):
    """The catalog is drawn from ``files -j``'s records, not re-parsed from a
    drawn tree: directories before files at every level, names sorted,
    ``│`` continuation only while a sibling follows, and a module's
    responsibility appended wherever the path resolves to a docstring."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "deep.py").write_text('"""Deep: the leaf."""\n',
                                                      encoding="utf-8")
    files = [{"path": "pkg/zeta.py", "language": "python", "nodeCount": 2, "size": 1},
             {"path": "pkg/sub/deep.py", "language": "python", "nodeCount": 1, "size": 1},
             {"path": "tests/test_a.py", "language": "python", "nodeCount": 3, "size": 1},
             {"path": "README.md", "language": "markdown", "nodeCount": 0, "size": 1},
             {"path": "pkg/alpha.py", "language": "python", "nodeCount": 4, "size": 1}]
    assert pack.render_tree(files, tmp_path).splitlines() == [
        "Project Structure (5 files):",
        "",
        "├── pkg",
        "│   ├── sub",
        "│   │   └── deep.py (python, 1 symbols) — Deep: the leaf.",
        "│   ├── alpha.py (python, 4 symbols)",
        "│   └── zeta.py (python, 2 symbols)",
        "├── tests",
        "│   └── test_a.py (python, 3 symbols)",
        "└── README.md (markdown, 0 symbols)",
    ]


def test_missing_codegraph_degrades_and_exits_zero(repo, monkeypatch, capsys):
    monkeypatch.setenv("CODEGRAPH_BIN", str(repo / "no-such-codegraph"))
    assert cli.main(["pack", "7", "--role", "implementer", "--cwd", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out and "gather" in out
    assert "Project Structure" not in out
    assert "Fixture persona" in out  # the rest of the pack still ships


@pytest.mark.parametrize("code", [
    "import sys; sys.exit(1)",   # a verb that fails (here: sync/init itself)
    "import sys; sys.exit(0)",   # exits clean with NO output: not a map either
])
def test_failing_or_silent_codegraph_degrades(repo, tmp_path, capsys, code):
    binary = fake_binary(tmp_path, code)
    assert cli.main(["pack", "7", "--cwd", str(repo), "--codegraph-bin", str(binary)]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out
    assert "tier 1" not in out  # never an empty catalog under a clean heading


@pytest.mark.parametrize("persona", [None, "# No marker\n\nBe lazy.\n"])
def test_persona_missing_or_markerless_fails_closed(repo, tmp_path, monkeypatch,
                                                    capsys, persona):
    """A docs-less wheel (no persona file) and a persona without the
    constitution marker are the same rung: an error marker and exit 2, never
    a pack that dispatches without the rules or appends them silently."""
    path = tmp_path / "persona.md"
    if persona is not None:
        path.write_text(persona, encoding="utf-8")
    monkeypatch.setattr(cli, "PACKAGE_PERSONA", path)
    assert cli.main(["pack", "7", "--role", "implementer", "--cwd", str(repo)]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_judge_pack_carries_no_persona(repo, tmp_path, capsys):
    binary, _ = fake_codegraph(tmp_path)
    assert cli.main(["pack", "7", "--role", "judge", "--cwd", str(repo),
                 "--codegraph-bin", str(binary)]) == 0
    out = capsys.readouterr().out
    assert "Fixture persona" not in out and "Fixture constitution" not in out
    assert "Project Structure" in out and TITLE in out


def test_host_extension_files_land_under_their_headings(repo, tmp_path, capsys):
    binary, _ = fake_codegraph(tmp_path)
    (tmp_path / "prime.md").write_text("lesson: reuse fmt\n", encoding="utf-8")
    (tmp_path / "trace.md").write_text("round 1: red then green\n", encoding="utf-8")
    assert cli.main(["pack", "7", "--cwd", str(repo), "--codegraph-bin", str(binary),
                 "--prime", str(tmp_path / "prime.md"),
                 "--trace", str(tmp_path / "trace.md")]) == 0
    out = capsys.readouterr().out
    assert "## Prior lessons\n\nlesson: reuse fmt\n" in out
    assert "## Run trace\n\nround 1: red then green\n" in out
