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
budget, tier 2's expansion of the directories the pointed files land in,
and the fold of a directory wider than the budget (funloops#53).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from devloop import cli, github, pack

FIX = Path(__file__).resolve().parent / "fixtures" / "pack"
TITLE = "fx: run fmt over the catalog"  # hits two entry points, both under fx/


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=fx", "-c", "user.email=fx@example.com",
                    "-c", "commit.gpgsign=false", *args],
                   cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """The fixture tree in a git repo of its own (the ``.git`` marker stops
    the constitution walk): tag ``base`` holds the package's ``__init__.py``,
    and HEAD adds ``core.py`` and ``helper.py``, the slice's touched modules.
    The packaged docs are swapped for the fixture stand-ins, and ``gh`` is
    replaced by the fixture issue."""
    root = tmp_path / "repo"
    shutil.copytree(FIX / "repo", root)
    git(root, "init", "-q")
    git(root, "add", "fx/__init__.py")
    git(root, "commit", "-qm", "base")
    git(root, "tag", "base")
    git(root, "add", ".")
    git(root, "commit", "-qm", "slice")
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


@pytest.mark.parametrize("role, posture", [("implementer", "writer"), ("judge", "reader")])
def test_pack_is_golden_and_byte_stable(repo, tmp_path, capsys, role, posture):
    """Section order per funloops#45's Interfaces block: Issue, Rules, Persona
    (writer posture only), Repo map, Prior lessons, Run trace, Standing orders
    (writer posture only) — the judge's golden carries the rules and no
    persona. The posture is the role's `[dispatch]` entry's (funloops#65)."""
    binary, log = fake_codegraph(tmp_path)
    argv = ["pack", "7", "--role", role, "--cwd", str(repo),
            "--codegraph-bin", str(binary), "--base-ref", "base"]
    assert cli.main(argv) == 0
    first = capsys.readouterr().out
    assert first == (FIX / f"{role}.md").read_text(encoding="utf-8")
    assert ("## Persona" in first) is (posture == "writer")
    assert ("## Standing orders" in first) is (posture == "writer")
    # the reader reads every module the slice touches in full, numbered
    assert ("## Touched modules" in first) is (posture == "reader")
    # no index in a fresh worktree → init; then the tiers in order: the
    # catalog, the entry points (context as JSON), the spliced context, and
    # one node call per file the issue names (helper first: first mention wins)
    assert log.read_text().split() == ["init", "files", "context", "context",
                                       "node", "node"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == first


def test_any_configured_role_packs_by_its_posture(repo, tmp_path, capsys):
    """A role is a `[dispatch]` entry (funloops#65): a reviewer declared by
    `--set` packs by its posture — a reader gets the judge's golden under its
    own header, no persona, no standing orders. A role no entry declares, or
    one whose entry names no posture, is an error marker and exit 2, never a
    pack shaped by a guess."""
    binary, _ = fake_codegraph(tmp_path)
    argv = ["pack", "7", "--cwd", str(repo), "--codegraph-bin", str(binary),
            "--base-ref", "base"]
    assert cli.main([*argv, "--role", "reviewer",
                     "--set", "dispatch.reviewer.posture=reader"]) == 0
    judge = (FIX / "judge.md").read_text(encoding="utf-8")
    assert capsys.readouterr().out == judge.replace("(judge)", "(reviewer)", 1)
    assert cli.main([*argv, "--role", "bogus"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert "bogus" in error and "implementer" in error
    assert cli.main([*argv, "--role", "reviewer",
                     "--set", "dispatch.reviewer.transport=herdr"]) == 2
    assert "posture" in json.loads(capsys.readouterr().out)["error"]


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


def test_tier2_expands_the_directories_of_the_pointed_files(nested):
    """Each directory a pointed file lands in, as its line over its files in
    today's file-line form; a directory the index does not hold is an empty
    line, never an error."""
    root, files = nested
    tree = pack.Directory.tree(files)
    assert tree.slice(["nope/x.md"], root, budget=99) == "nope/ (0 files, 0 symbols)"
    assert tree.slice([], root, budget=99) == ""


@pytest.fixture
def wide(nested):
    """``nested`` with a flat tests directory of twenty files — more than
    any budget below — as thinkweave's is (funloops#53)."""
    root, files = nested
    more = [pack.FileRecord(f"tests/test_{i:02d}.py", "python", 1) for i in range(1, 20)]
    return root, pack.Directory.tree([*files, *more])


@pytest.mark.parametrize("budget", [6, 2])  # room for some of the rest; none even for the pointed
def test_tier2_over_budget_shows_the_pointed_files_and_names_the_fold(wide, budget):
    """Rule 6: a directory larger than the budget never renders as its line
    alone. The pointed files show whatever the budget, the fold line names
    the count not shown, and no unpointed file pads the group."""
    root, tree = wide
    assert tree.slice(["tests/test_a.py", "tests/test_19.py"], root, budget=budget).splitlines() == [
        "tests/ (20 files, 22 symbols) — Tests: the suite",
        "├── test_19.py (python, 1 symbols)",
        "├── test_a.py (python, 3 symbols)",
        "└── … 18 more files",
    ]


def test_tier2_over_budget_without_a_pointed_file_shows_the_first_files(wide):
    """A pointed file the index does not hold (a README): the first files
    fill what is left of the budget, so something still shows below."""
    root, tree = wide
    assert tree.slice(["tests/README.md"], root, budget=3).splitlines() == [
        "tests/ (20 files, 22 symbols) — Tests: the suite",
        "├── test_01.py (python, 1 symbols)",
        "└── … 19 more files",
    ]


def test_tier2_two_groups_share_one_budget(wide):
    """Two groups under one budget: the small one opens whole, the wide one
    folds to its pointed file."""
    root, tree = wide
    assert tree.slice(["tests/test_a.py", "pkg/sub/deep.py"], root, budget=8).splitlines() == [
        "pkg/sub/ (2 files, 3 symbols) — Sub: the leaf package.",
        "├── __init__.py (python, 1 symbols) — Sub: the leaf package.",
        "└── deep.py (python, 2 symbols) — Deep: the leaf.",
        "",
        "tests/ (20 files, 22 symbols) — Tests: the suite",
        "├── test_a.py (python, 3 symbols)",
        "└── … 19 more files",
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


def test_shape_pack_holds_the_stack_modules_and_their_edges(repo, tmp_path, capsys):
    """The judge's shape posture over a stack of issues: every issue, the
    rules, every touched module in full, and codegraph's edges per module —
    no persona, no standing orders, no catalog."""
    binary, log = fake_codegraph(tmp_path)
    assert cli.main(["pack", "7", "8", "--role", "judge", "--posture", "shape",
                     "--cwd", str(repo), "--codegraph-bin", str(binary),
                     "--base-ref", "base"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Dispatch pack — issues #7, #8 (judge, shape)\n")
    assert "## Issue #7\n" in out and "## Issue #8\n" in out
    assert "## Rules" in out
    assert "### fx/core.py\n" in out and "### fx/helper.py\n" in out
    assert "  13  def run(x: int) -> str:\n" in out  # numbered for file:line citations
    assert "### fx/__init__.py" not in out             # untouched since base
    assert "## Module edges" in out and "no other indexed file depends on it" in out
    for gone in ("## Persona", "## Standing orders", "tier 1"):
        assert gone not in out
    assert log.read_text().split() == ["init", "node", "node"]


def test_shape_pack_without_codegraph_still_holds_every_module(repo, monkeypatch, capsys):
    monkeypatch.setenv("CODEGRAPH_BIN", str(repo / "no-such-codegraph"))
    assert cli.main(["pack", "7", "--role", "judge", "--posture", "shape",
                     "--cwd", str(repo), "--base-ref", "base"]) == 0
    out = capsys.readouterr().out
    assert "### fx/core.py\n" in out and "### fx/helper.py\n" in out
    edges = out[out.index("## Module edges"):out.index("## Shape brief")]
    assert "DEGRADED" in edges and "edges" in edges.splitlines()[2]
    assert len(edges.strip().splitlines()) == 3  # the heading, a blank, one line


@pytest.mark.parametrize("argv, needle", [
    (["7", "8", "--role", "judge", "--base-ref", "base"], "one issue"),                  # reader packs one slice
    (["7", "--role", "implementer", "--posture", "shape"], "posture"),  # not in its entry
    (["7", "--role", "judge", "--base-ref", "no-such-ref"], "no-such-ref"),
])
def test_pack_refuses_what_it_cannot_shape(repo, tmp_path, capsys, argv, needle):
    binary, _ = fake_codegraph(tmp_path)
    assert cli.main(["pack", *argv, "--cwd", str(repo), "--codegraph-bin", str(binary)]) == 2
    assert needle in json.loads(capsys.readouterr().out)["error"]


def test_writer_dispatch_file_is_the_pack_plus_its_lines(repo, tmp_path, capsys):
    """``--out`` writes the whole dispatch and prints its path: the pack, then
    the branch, baseline, return-file and worktree lines, in that order."""
    binary, _ = fake_codegraph(tmp_path)
    out, ret = tmp_path / "d.md", tmp_path / "r.md"
    assert cli.main(["pack", "7", "--cwd", str(repo), "--codegraph-bin", str(binary),
                     "--branch", "loop/x", "--baseline", "green",
                     "--return", str(ret), "--out", str(out)]) == 0
    assert capsys.readouterr().out.strip() == str(out)
    text = out.read_text(encoding="utf-8")
    golden = (FIX / "implementer.md").read_text(encoding="utf-8")
    assert text == golden + (f"\nBranch: loop/x\nBaseline: green\nReturn file: {ret}\n"
                             f"Worktree: {repo}\n")


def test_judge_dispatch_carries_brief_diff_check_and_evidence(repo, tmp_path, capsys):
    """The reader dispatch is complete: the judge brief with its return
    shape, the slice diff, the ``check --issue`` output and the evidence
    directory, then the return file and the worktree it judges."""
    binary, _ = fake_codegraph(tmp_path)
    check = tmp_path / "check.json"
    check.write_text('{"issue": 7, "summary": "1/1 passed"}\n', encoding="utf-8")
    out = tmp_path / "j.md"
    assert cli.main(["pack", "7", "--role", "judge", "--cwd", str(repo),
                     "--codegraph-bin", str(binary), "--base-ref", "base",
                     "--check-json", str(check), "--evidence", str(tmp_path / "r.md.demo"),
                     "--return", str(tmp_path / "j.json"), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith((FIX / "judge.md").read_text(encoding="utf-8"))
    assert "## Judge brief" in text and '"criteria"' in text and "rule:<n>" in text
    assert "## Diff\n" in text and "+def fmt(x: int) -> str:" in text
    assert '## Check output\n\n```json\n{"issue": 7, "summary": "1/1 passed"}\n```' in text
    assert f"Evidence directory: {tmp_path / 'r.md.demo'}\n" in text
    assert text.endswith(f"Return file: {tmp_path / 'j.json'}\nWorktree: {repo}\n")
    for gone in ("## Standing orders", "Branch:", "Baseline:"):
        assert gone not in text


def test_judge_brief_blocks_copied_patterns_never_untouched_code():
    """New code that copies a rule-breaking pattern is a ``rule:<n>``
    violation; code the diff does not touch never blocks."""
    brief = " ".join(pack.JUDGE_BRIEF.split())
    assert "New code that copies an existing rule-breaking pattern is a `rule:<n>` violation" in brief
    assert "Code the diff does not touch never blocks" in brief


def test_shape_dispatch_carries_the_shape_brief(repo, tmp_path, capsys):
    binary, _ = fake_codegraph(tmp_path)
    out = tmp_path / "s.md"
    assert cli.main(["pack", "7", "--role", "judge", "--posture", "shape",
                     "--cwd", str(repo), "--codegraph-bin", str(binary),
                     "--base-ref", "base", "--return", str(tmp_path / "s.json"),
                     "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "## Shape brief" in text and '"options"' in text and '"verdict"' in text
    for gone in ("## Judge brief", "## Diff", "## Standing orders"):
        assert gone not in text
