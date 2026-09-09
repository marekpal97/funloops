"""Seams for ``devloop pack`` (funloops#28, dec-fd12489d, dec-d2de831e).

Two seams, both at the verb's stdout:

- **the pack** — with codegraph replaced by a fake binary that replays
  captured CLI output, both roles' packs are byte-pinned to a golden each and
  byte-identical across two runs (funloops#45, dec-2f8c2322: the rules are a
  `## Rules` section for both roles, the persona a `## Persona` section for
  the implementer only);
- **the degraded path** — a missing binary, a verb that exits non-zero, a
  read verb that prints nothing, or a ``files -j`` record off the declared
  shape all print the marked degraded block and exit 0 (never block, never a
  clean empty map).

Fixtures under ``fixtures/pack/``: ``repo/`` is the tree the captured output
describes; ``files.json``, ``context.txt``, ``context.json``, ``node-*.txt``
are codegraph 1.6.0's literal stdout over it (``init -y``, then ``files -j``
/ ``context --no-code <title>`` in markdown and ``-f json`` / ``node -f
<file> --symbols-only``); ``issue.md`` is the issue body; ``persona.md`` /
``constitution.md`` are two-line stand-ins for the packaged docs so the
goldens pin the pack's composition, not their prose; ``implementer.md`` /
``judge.md`` are the goldens, captured once and hand-checked. The REAL rules
+ overlay numbering is pinned in test_constitution.py.

The map's third seam is ``pack.Directory`` (funloops#46, dec-e6561edc): a
hand-built nested tree pins tier 1's directory lines, the fold order under a
budget, and tier 2's expansion of a named directory.
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
TITLE = "fx: run fmt over the catalog"  # hits two entry points, both under fx/


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
    argv is ``--no-color <verb> …``; for ``node`` the file follows ``-f``;
    ``context`` replays the JSON capture when ``-f json`` is asked for."""
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
    out = fix / ("context.json" if "json" in args else "context.txt")
else:
    sys.exit(f"unexpected verb {{verb}}")
sys.stdout.write(out.read_text(encoding="utf-8"))
""")
    return binary, log


@pytest.mark.parametrize("role", ["implementer", "judge"])
def test_pack_is_golden_and_byte_stable(repo, tmp_path, capsys, role):
    """Section order per funloops#45's Interfaces block: Issue, Rules, Persona
    (implementer only), Repo map, Prior lessons, Run trace, Standing orders
    (implementer only) — the judge's golden carries the rules and no persona."""
    binary, log = fake_codegraph(tmp_path)
    argv = ["pack", "7", "--role", role, "--cwd", str(repo),
            "--codegraph-bin", str(binary)]
    assert cli.main(argv) == 0
    first = capsys.readouterr().out
    assert first == (FIX / f"{role}.md").read_text(encoding="utf-8")
    # no index in a fresh worktree → init; then the tiers in order: the
    # catalog, the entry points (context as JSON), the spliced context, and
    # one node call per file the issue names (helper first: first mention wins)
    assert log.read_text().split() == ["init", "files", "context", "context",
                                       "node", "node"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == first


@pytest.fixture
def nested(tmp_path):
    """A nested tree for the map's seam: two packages with docstrings, a
    tests directory whose responsibility is its README's first line, a
    tools directory holding files only in subdirectories, a leaf package
    under a leaf package, and one backslash path (a Windows codegraph)."""
    for rel, text in {"pkg/__init__.py": '"""Pkg: the package."""\n',
                      "pkg/sub/__init__.py": '"""Sub: the leaf package."""\n',
                      "pkg/sub/deep.py": '"""Deep: the leaf.\n\nNot this."""\n',
                      "pkg/sub/deeper/z.py": "",
                      "tests/README.md": "\n# Tests: the suite\n\nNot this.\n",
                      "tests/test_a.py": "",
                      "tools/a/x.py": "", "tools/b/y.py": ""}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    files = [pack.FileRecord("pkg/__init__.py", "python", 1),
             pack.FileRecord("pkg/sub/__init__.py", "python", 1),
             pack.FileRecord("pkg/sub/deep.py", "python", 2),
             pack.FileRecord("pkg/sub/deeper/z.py", "python", 4),
             pack.FileRecord(r"tests\test_a.py", "python", 3),
             pack.FileRecord("tools/b/y.py", "python", 6),
             pack.FileRecord("tools/a/x.py", "python", 5)]
    return tmp_path, files


def test_tier1_lists_directories_with_counts_and_responsibility(nested):
    """One line per directory holding files, path-sorted, counting only the
    files directly under it; the responsibility is ``__init__.py``'s first
    docstring line, else the README's first line."""
    root, files = nested
    assert pack.Directory.tree(files).catalog(root, budget=99).splitlines() == [
        "Project Structure (7 files):",
        "",
        "pkg/ (1 files, 1 symbols) — Pkg: the package.",
        "pkg/sub/ (2 files, 3 symbols) — Sub: the leaf package.",
        "pkg/sub/deeper/ (1 files, 4 symbols)",
        "tests/ (1 files, 3 symbols) — Tests: the suite",
        "tools/a/ (1 files, 5 symbols)",
        "tools/b/ (1 files, 6 symbols)",
    ]
    with pytest.raises(pack.CodegraphUnavailable):  # a file that is also a directory
        pack.Directory.tree([*files, pack.FileRecord("pkg/sub/deep.py/x.py", "python", 1)])
    with pytest.raises(pack.CodegraphUnavailable):  # listed twice
        pack.Directory.tree([*files, pack.FileRecord("tools/a/x.py", "python", 1)])


@pytest.mark.parametrize("budget, lines", [
    (6, ["pkg/ (1 files, 1 symbols) — Pkg: the package.",
         "pkg/sub/ (2 files, 3 symbols) — Sub: the leaf package.",
         "pkg/sub/deeper/ (1 files, 4 symbols)",
         "tests/ (1 files, 3 symbols) — Tests: the suite",
         "tools/a/ (1 files, 5 symbols)",
         "tools/b/ (1 files, 6 symbols)"]),
    # pkg/sub (3 files) outranks tools (2 files) among the innermost folds
    (5, ["pkg/ (1 files, 1 symbols) — Pkg: the package.",
         "pkg/sub/ (3 files, 7 symbols) — Sub: the leaf package.",
         "tests/ (1 files, 3 symbols) — Tests: the suite",
         "tools/a/ (1 files, 5 symbols)",
         "tools/b/ (1 files, 6 symbols)"]),
    # then pkg (4 files) folds, counting its whole subtree
    (4, ["pkg/ (4 files, 8 symbols) — Pkg: the package.",
         "tests/ (1 files, 3 symbols) — Tests: the suite",
         "tools/a/ (1 files, 5 symbols)",
         "tools/b/ (1 files, 6 symbols)"]),
    # then tools, a directory with no files of its own
    (3, ["pkg/ (4 files, 8 symbols) — Pkg: the package.",
         "tests/ (1 files, 3 symbols) — Tests: the suite",
         "tools/ (2 files, 11 symbols)"]),
    # the floor: the root never folds
    (1, ["pkg/ (4 files, 8 symbols) — Pkg: the package.",
         "tests/ (1 files, 3 symbols) — Tests: the suite",
         "tools/ (2 files, 11 symbols)"]),
])
def test_tier1_folds_the_largest_innermost_directory_first(nested, budget, lines):
    root, files = nested
    assert pack.Directory.tree(files).catalog(root, budget=budget).splitlines()[2:] == lines


def test_tier2_expands_named_directories_to_file_lines(nested):
    """Each directory as its line over its files in today's file-line form;
    over budget, the largest group folds to its line alone; a directory the
    index does not hold is an empty line, never an error."""
    root, files = nested
    tree = pack.Directory.tree(files)
    assert tree.slice(["pkg/sub"], root, budget=99).splitlines() == [
        "pkg/sub/ (2 files, 3 symbols) — Sub: the leaf package.",
        "├── __init__.py (python, 1 symbols) — Sub: the leaf package.",
        "└── deep.py (python, 2 symbols) — Deep: the leaf.",
    ]
    assert tree.slice(["pkg/sub", "tools/a"], root, budget=4).splitlines() == [
        "pkg/sub/ (2 files, 3 symbols) — Sub: the leaf package.",
        "",
        "tools/a/ (1 files, 5 symbols)",
        "└── x.py (python, 5 symbols)",
    ]
    assert tree.slice(["nope"], root, budget=99) == "nope/ (0 files, 0 symbols)"
    assert tree.slice([], root, budget=99) == ""


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
    # a `files -j` record that is not the declared shape (a renamed key)
    'print(\'[{"path": "fx/core.py", "language": "python", "node_count": 5}]\')',
])
def test_failing_silent_or_misshapen_codegraph_degrades(repo, tmp_path, capsys, code):
    binary = fake_binary(tmp_path, code)
    assert cli.main(["pack", "7", "--cwd", str(repo), "--codegraph-bin", str(binary)]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out
    assert "tier 1" not in out  # never an empty catalog under a clean heading


@pytest.mark.parametrize("role, doc", [("implementer", "PACKAGE_PERSONA"),
                                       ("judge", "PACKAGE_CONSTITUTION")])
def test_missing_persona_or_rules_fail_closed(repo, tmp_path, monkeypatch,
                                              capsys, role, doc):
    """A docs-less wheel (no persona, no packaged rules) is an error marker
    and exit 2 for every role that needs the file — never a pack that
    dispatches without the rules or the persona."""
    monkeypatch.setattr(cli, doc, tmp_path / "absent.md")
    assert cli.main(["pack", "7", "--role", role, "--cwd", str(repo)]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


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
