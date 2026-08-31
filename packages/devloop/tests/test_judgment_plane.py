"""The judgment plane, funloops-side (issue #149).

S1 moved the command docs verbatim; they still described thinkweave. These are
the seams for the rewrite's four acceptance criteria:

1. ``/issue-loop`` is invocable in a funloops checkout — the doc drives the
   ``devloop`` rail, and ``config``/``plan`` really run here.
2. The memory-feed sections are explicitly conditional on a host vault, and the
   doc minus those blocks is still a complete, coherent loop.
3. ``loop.toml.template`` ships commented defaults with nothing host-specific.
4. The boundary spec describes the workspace layout it actually ships in.

Sources of truth are independent of the docs throughout: the argparse parser
for the subcommand surface, ``DEFAULT_CONFIG`` for the template's knob list,
and the filesystem for the package map.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from devloop import cli

DOCS = cli.REPO_ROOT / "docs" / "agents"
COMMAND_DOC = DOCS / "issue-loop.command.md"
TEMPLATE = DOCS / "loop.toml.template"
BOUNDARIES = DOCS / "devloop-boundaries.md"
WORKSPACE_ROOT = cli.REPO_ROOT.parent.parent

# The marked-block convention the command doc uses for the optional half.
EXT_OPEN = "<!-- host-extension:"
EXT_CLOSE = "<!-- /host-extension -->"


def _doc(path: Path) -> str:
    assert path.exists(), f"{path.name} must ship under docs/agents/"
    return path.read_text(encoding="utf-8")


def _subcommands() -> set[str]:
    sub = next(a for a in cli.build_arg_parser()._actions
               if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def _without_host_extensions(text: str) -> str:
    """The command doc as a vault-less host reads it: marked blocks removed."""
    return re.sub(re.escape(EXT_OPEN) + ".*?" + re.escape(EXT_CLOSE), "", text, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# AC1 — invocable in a funloops checkout


def test_command_doc_drives_the_devloop_rail():
    """Every rail invocation goes through the packaged entry point. The old
    ``python scripts/issue_loop.py`` path does not exist in this repo, so a doc
    that still names it is a doc that cannot be followed."""
    text = _doc(COMMAND_DOC)
    assert "scripts/issue_loop.py" not in text
    # Source of truth: argparse. Every subcommand the rail exposes is shown
    # being invoked through `devloop`.
    for name in _subcommands():
        assert f"devloop {name}" in text, name


def test_command_doc_documents_the_symlink_wiring():
    """`/issue-loop` is installed by a machine-local symlink into
    .claude/commands/ — the same convention the vendored ponytail files
    describe, and the reason the command doc is invocable at all."""
    text = _doc(COMMAND_DOC)
    assert ".claude/commands/" in text and "ln -s" in text
    assert "issue-loop.command.md" in text


def test_symlink_is_not_committed():
    """The symlink itself is machine-local. Untracked is the observable half;
    the gitignore rule is what makes it stay that way, so pin both — otherwise
    this passes on a checkout that simply never installed the command."""
    out = subprocess.run(
        ["git", "ls-files", ".claude/"],
        cwd=WORKSPACE_ROOT, capture_output=True, text=True, check=False,
    ).stdout
    assert "issue-loop" not in out
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".claude/commands/issue-loop.md"],
        cwd=WORKSPACE_ROOT, capture_output=True, check=False,
    ).returncode
    assert ignored == 0, "the command symlink path must be gitignored"


def test_config_runs_in_this_checkout():
    """`devloop config` resolves this repo's loop.toml — the funloops one, not
    a copy of thinkweave's."""
    out = subprocess.run([sys.executable, "-m", "devloop", "config"],
                         cwd=WORKSPACE_ROOT, capture_output=True, text=True, check=True)
    cfg = json.loads(out.stdout)
    tests_gate = next(g for g in cfg["gates"] if g["id"] == "tests")
    # Every `--extra` the gate asks for must be an extra this package actually
    # declares — S1's inherited `--extra mcp` named a thinkweave-only one, so
    # the gate could not have run here. Source of truth: the package metadata.
    meta = tomllib.loads((cli.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(meta["project"].get("optional-dependencies", {}))
    assert set(re.findall(r"--extra\s+(\S+)", tests_gate["cmd"])) <= declared
    # No thinkweave paths survive in the resolved config or the file it came from.
    assert "thinkweave" not in json.dumps(cfg).lower()
    assert "thinkweave" not in _doc(DOCS / "loop.toml").lower()


@pytest.mark.skipif(
    shutil.which("gh") is None
    or subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0,
    reason="needs an authenticated gh (the tracker is the DAG)",
)
def test_plan_runs_against_the_funloops_tracker():
    """`plan` talks to *this* repo's tracker. An empty frontier is a valid
    result — the assertion is that the snapshot resolves and partitions."""
    out = subprocess.run([sys.executable, "-m", "devloop", "plan"],
                         cwd=WORKSPACE_ROOT, capture_output=True, text=True, check=True)
    plan = json.loads(out.stdout)
    assert set(plan) == {"frontier", "blocked", "claimed", "warnings"}


# ---------------------------------------------------------------------------
# AC2 — the memory feed is an optional host extension


def test_memory_feed_blocks_are_marked_and_balanced():
    """The prime splice, the trajectory feed, and the wrap-coverage note are
    the three vault-dependent stretches; each is a marked block."""
    text = _doc(COMMAND_DOC)
    assert text.count(EXT_OPEN) == text.count(EXT_CLOSE) >= 3
    # The marker states the condition and the vault-less behavior, so a reader
    # who skips the block knows what they are skipping. Non-greedy to the
    # closing `-->` (a marker may contain `>`).
    markers = re.findall(re.escape(EXT_OPEN) + r".*?-->", text, re.DOTALL)
    for marker in markers:
        assert "vault" in marker.lower(), marker
    assert "primed=false" in text or "primed: false" in text


def test_the_loop_is_complete_without_a_host_vault():
    """A vault-less reader gets the whole loop: the doc minus its marked blocks
    still carries every stage, and mentions no vault machinery at all."""
    spine = _without_host_extensions(_doc(COMMAND_DOC))
    for heading in ("## 0. ", "## 0.5 ", "### 1a.", "### 1b.", "### 1c.",
                    "### 1d.", "### 1e.", "## 2. "):
        assert heading in spine, heading
    # The load-bearing content of each stage survives the strip.
    for token in ("implementer subagent", "Do NOT push", "gh pr create",
                  "max_fix_rounds", "git worktree remove"):
        assert token in spine, token
    # The preamble is where the extension is explained — it names the host and
    # says what a reader without one does.
    preamble, _, body = spine.partition("## 0. ")
    assert "skip every marked block" in preamble
    # From the first step on, no procedural instruction needs a memory host.
    for token in ("weave_", "weave ", "vault", "thinkweave", "trajectory", "prime"):
        assert token not in body.lower(), token


def test_the_host_overlay_is_pointed_at_not_shipped():
    """issue-loop-memory.md is the host's overlay (it documents how finished
    issues feed a thinkweave vault) and stays host-side. funloops points at it
    rather than carrying a copy that would drift — and locates it in exactly
    one place, with the repo and path a reader needs to actually find it."""
    assert not (DOCS / "issue-loop-memory.md").exists()
    spec = _doc(BOUNDARIES)
    assert "docs/agents/issue-loop-memory.md" in spec
    assert "thinkweave" in spec[spec.index("**The host overlay.**"):]
    # One locating site, so a move updates one line.
    named = sum("issue-loop-memory.md" in _doc(p) for p in DOCS.glob("*.md"))
    assert named == 1, "the overlay is located in more than one doc"


# ---------------------------------------------------------------------------
# AC3 — the loop.toml template


def test_template_is_a_working_config_with_the_same_gate_contract():
    """The template parses through the real loader and carries the same gate
    pipeline as this repo's config — a host that copies it gets a loop that
    runs, not a fragment to assemble."""
    template = cli.load_config(TEMPLATE)
    shipped = cli.load_config()
    assert [(g["id"], g["kind"]) for g in template["gates"]] == \
           [(g["id"], g["kind"]) for g in shipped["gates"]]


def test_template_documents_every_knob():
    """'Commented defaults' means every overridable knob is present and
    explained. Source of truth: DEFAULT_CONFIG, not a hand-kept list."""
    text = _doc(TEMPLATE)
    assert "docs/agents/loop.toml" in text  # the header names where a copy is found
    for section, knobs in cli.DEFAULT_CONFIG.items():
        assert f"[{section}]" in text, section
        for knob in knobs:
            assert re.search(rf"^\s*#?\s*{re.escape(knob)}\s*=", text, re.MULTILINE), knob


def test_template_bakes_in_no_host_specifics():
    """No host paths, no host repo names — the two things that made S1's
    verbatim copy wrong for funloops in the first place."""
    text = _doc(TEMPLATE)
    low = text.lower()
    assert "thinkweave" not in low
    assert "packages/devloop" not in low
    cfg = cli.load_config(TEMPLATE)
    # sensitive_paths starts empty: a host adds its own, and an inherited list
    # would silently classify the wrong files as sensitive. watched_paths ships
    # exactly devloop's own convention — docs/agents/ is where every adopting
    # repo's loop.toml and constitution overlay live (issue #26: the amendment
    # convention relies on a human seeing that diff), and watched only caps a
    # PR at yellow, so a wrong inherited entry costs a skim, not a red lane.
    assert cfg["triage"]["sensitive_paths"] == []
    assert cfg["triage"]["watched_paths"] == ["docs/agents/"]


# --- the template's delivery mechanism (review round 1, major) --------------
# "Copy it to your repo's docs/agents/loop.toml" has to be true. It is only
# true if the rail looks there, so these pin the lookup, not the prose.


def _host_repo(tmp_path: Path, marker: str = "echo host") -> Path:
    """A repo that adopted the template, with one distinguishing edit."""
    (tmp_path / ".git").mkdir()
    cfg_dir = tmp_path / "docs" / "agents"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "loop.toml").write_text(
        _doc(TEMPLATE).replace('cmd = "pytest -q"', f'cmd = "{marker}"'), encoding="utf-8")
    return tmp_path


def test_config_is_found_from_a_subdirectory(tmp_path):
    """The loop runs from wherever the orchestrator sits, not only the repo
    root, so the search walks upward."""
    deep = _host_repo(tmp_path) / "src" / "pkg"
    deep.mkdir(parents=True)
    assert cli.find_config(deep) == tmp_path / "docs" / "agents" / "loop.toml"


def test_the_search_stops_at_the_repo_root(tmp_path):
    """A repo with no loop.toml of its own must not silently inherit one from
    an ancestor directory — that would be someone else's gate pipeline."""
    outer = _host_repo(tmp_path)
    inner = outer / "vendor" / "other-repo"
    (inner / ".git").mkdir(parents=True)
    assert cli.find_config(inner) == cli.PACKAGE_CONFIG


def test_config_falls_back_to_the_packaged_copy(tmp_path):
    """No host config anywhere → the package's own, which is how the funloops
    checkout (no docs/agents/ at its root) keeps resolving its loop.toml."""
    assert cli.find_config(tmp_path) == cli.PACKAGE_CONFIG


def test_an_adopting_repo_really_gets_its_own_gate_pipeline(tmp_path):
    """End to end, the way the reviewer reproduced the bug: copy the template
    into a repo, run the rail from that repo, and the emitted config must be
    that repo's — not this package's."""
    host = _host_repo(tmp_path)
    out = subprocess.run([sys.executable, "-m", "devloop", "config"],
                         cwd=host, capture_output=True, text=True, check=True)
    cfg = json.loads(out.stdout)
    assert next(g for g in cfg["gates"] if g["id"] == "tests")["cmd"] == "echo host"
    assert cfg["triage"]["sensitive_paths"] == []   # the template's, not funloops'


# ---------------------------------------------------------------------------
# AC4 — the boundary spec describes the layout it ships in


def test_boundary_package_map_matches_the_filesystem():
    """The §2 map is a contract, so pin it both ways against the real tree."""
    text = _doc(BOUNDARIES)
    block = text[text.index("## 2. Package map"):].split("```")[1]
    listed = set(re.findall(r"[\w_]+\.py", block))
    actual = {p.name for p in (cli.REPO_ROOT / "devloop").rglob("*.py")}
    assert listed == actual


def test_boundary_spec_names_the_workspace_layout_and_entry_points():
    """The external seam is the console script now; the thinkweave shim it was
    carved out of does not exist here."""
    text = _doc(BOUNDARIES)
    assert "scripts/issue_loop.py" not in text
    assert "packages/devloop/" in text
    assert "python -m devloop" in text
