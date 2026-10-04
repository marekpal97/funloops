"""The judgment plane's config side: the rail runs in a funloops checkout,
the template is a working host-neutral config, and an adopting repo's
loop.toml is the one the rail resolves."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from devloop import cli

TEMPLATE = cli.REPO_ROOT / "docs" / "agents" / "loop.toml.template"
WORKSPACE_ROOT = cli.REPO_ROOT.parent.parent

# ---------------------------------------------------------------------------
# invocable in a funloops checkout


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
    # No thinkweave paths survive in the resolved config.
    assert "thinkweave" not in json.dumps(cfg).lower()


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
    assert set(plan) == {"run_id", "frontier", "deferred", "blocked", "claimed", "warnings"}


# ---------------------------------------------------------------------------
# the loop.toml template


def test_template_is_a_working_config_with_the_same_gate_contract():
    """The template parses through the real loader and carries the same gate
    pipeline as this repo's config — a host that copies it gets a loop that
    runs, not a fragment to assemble."""
    template = cli.load_config(TEMPLATE)
    shipped = cli.load_config()
    assert [(g["id"], g["kind"]) for g in template["gates"]] == \
           [(g["id"], g["kind"]) for g in shipped["gates"]]


def test_template_bakes_in_no_host_specifics():
    """No host paths, no host repo names — the two things that made S1's
    verbatim copy wrong for funloops in the first place."""
    cfg = cli.load_config(TEMPLATE)
    assert "thinkweave" not in json.dumps(cfg).lower()
    assert "packages/devloop" not in json.dumps(cfg)
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
        f'[[gates]]\nid = "tests"\nkind = "command"\ncmd = "{marker}"\n', encoding="utf-8")
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
    assert cfg["triage"]["sensitive_paths"] == []   # the default, not funloops'

