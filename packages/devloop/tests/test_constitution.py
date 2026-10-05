"""The constitution ships as a devloop default.

Seams under test: resolution (``cli.find_constitution``: a fresh repo gets the
packaged default; a repo's ``docs/agents/constitution.md`` overlay extends it,
never replaces it), the packaged file's one-screen cap, and the amendment
convention (``watched_paths`` covers both layers).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from devloop import cli, paths

FUNLOOPS_ROOT = cli.REPO_ROOT.parents[1]


def _repo(tmp_path: Path, overlay: str | None = None) -> Path:
    (tmp_path / ".git").mkdir()
    if overlay is not None:
        d = tmp_path / "docs" / "agents"
        d.mkdir(parents=True)
        (d / "constitution.md").write_text(overlay, encoding="utf-8")
    return tmp_path


def test_fresh_repo_resolves_the_packaged_default(tmp_path):
    """Zero authoring: a repo that never wrote a constitution still gets one."""
    assert cli.find_constitution(_repo(tmp_path)) == [cli.PACKAGE_CONSTITUTION]


def test_overlay_extends_without_replacing(tmp_path):
    """The packaged rules always apply and come first; the overlay is appended."""
    repo = _repo(tmp_path, overlay="13. local rule (some-incident)\n")
    overlay = repo / "docs" / "agents" / "constitution.md"
    assert cli.find_constitution(repo) == [cli.PACKAGE_CONSTITUTION, overlay]


def test_overlay_is_found_from_a_subdirectory(tmp_path):
    """The loop runs from wherever the orchestrator sits — the walk goes up,
    exactly as it does for loop.toml."""
    repo = _repo(tmp_path, overlay="13. local rule\n")
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    assert cli.find_constitution(deep) == [
        cli.PACKAGE_CONSTITUTION, repo / "docs" / "agents" / "constitution.md"]


def test_nested_repo_does_not_inherit_an_ancestor_overlay(tmp_path):
    """Stopping at the first .git: someone else's amendments must not leak in."""
    outer = _repo(tmp_path, overlay="13. outer rule\n")
    inner = outer / "vendor" / "other-repo"
    (inner / ".git").mkdir(parents=True)
    assert cli.find_constitution(inner) == [cli.PACKAGE_CONSTITUTION]


def test_the_package_checkout_serves_the_default_once_then_the_root_overlay():
    """The issue's pin (#50): started inside packages/devloop, the walk meets
    the packaged file first. It is the default, already served, not an
    overlay; the walk goes on to the repo root and serves funloops' own
    rules after it."""
    assert cli.find_constitution(cli.REPO_ROOT / "devloop") == [
        cli.PACKAGE_CONSTITUTION, FUNLOOPS_ROOT / "docs" / "agents" / "constitution.md"]


def _checkout(tmp_path: Path, overlay: str | None = None) -> Path:
    """Another checkout (a worktree) of a repo that vendors devloop the way
    funloops does: the package beside its docs/, a DIVERGED copy of the
    packaged constitution there, and optionally a root overlay."""
    repo = _repo(tmp_path, overlay)
    pkg = repo / "packages" / "devloop"
    (pkg / "devloop").mkdir(parents=True)
    (pkg / "devloop" / "__init__.py").touch()
    (pkg / "docs" / "agents").mkdir(parents=True)
    (pkg / "docs" / "agents" / "constitution.md").write_text(
        "1. a diverged packaged copy\n", encoding="utf-8")
    return repo


def test_the_packaged_copy_in_another_checkout_is_known_by_its_place(tmp_path):
    """The loop's own operating mode: devloop imported from one checkout, cwd
    inside a worktree of the repo. That worktree's tracked copy of the packaged
    file is recognised by where it sits (beside the devloop package), not by
    its bytes: even diverged it is never an overlay, and the walk goes on to
    the worktree's root overlay."""
    repo = _checkout(tmp_path, overlay="13. local rule\n")
    assert cli.find_constitution(repo / "packages" / "devloop") == [
        cli.PACKAGE_CONSTITUTION, repo / "docs" / "agents" / "constitution.md"]


def test_a_checkout_without_a_root_overlay_serves_the_default_alone(tmp_path):
    repo = _checkout(tmp_path)
    assert cli.find_constitution(repo / "packages" / "devloop") == [
        cli.PACKAGE_CONSTITUTION]


def test_a_missing_packaged_default_is_loud(tmp_path, monkeypatch):
    """A wheel that shipped no docs/ must not resolve to a nonexistent path
    the orchestrator splices as silence — losing every rule unannounced is
    the fail-open rule 6 names. Resolution refuses instead."""
    monkeypatch.setattr(cli, "PACKAGE_CONSTITUTION",
                        tmp_path / "absent" / "constitution.md")
    with pytest.raises(FileNotFoundError):
        cli.find_constitution(_repo(tmp_path))


def test_config_verb_emits_the_resolved_constitution(tmp_path):
    """The orchestrator's runnable half of the resolution contract: `devloop
    config` carries a `constitution` array — packaged default first, host
    overlay appended — so the doc can say 'run this', not 'walk like this'."""
    repo = _repo(tmp_path, overlay="13. local rule (#1)\n")
    out = subprocess.run([sys.executable, "-m", "devloop", "config"],
                         cwd=repo, capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["constitution"] == [
        str(cli.PACKAGE_CONSTITUTION),
        str(repo / "docs" / "agents" / "constitution.md")]


# ---------------------------------------------------------------------------
# The packaged file's own contract: one screen


def test_constitution_is_one_screen():
    assert cli.PACKAGE_CONSTITUTION.exists(), \
        "the constitution must ship under docs/agents/"
    lines = cli.PACKAGE_CONSTITUTION.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 40, f"{len(lines)} lines — the criterion is ≤ 40"


# ---------------------------------------------------------------------------
# Amendment convention: watched_paths already covers the file


def test_watched_paths_cover_the_constitution():
    """dec-1746aec3's predicted outcome: constitution.md stays under
    watched_paths, so an amendment PR is at most skim-lane, never invisible.
    Real matcher, real shipped configs — not a recomputed pattern. Both
    audiences: funloops keeps the packaged copy under packages/devloop/ AND
    its own overlay at the repo root (#41), an adopting repo's overlay lives
    at the repo root — each shipped config must cover its own arrangement."""
    funloops = cli.load_config(cli.PACKAGE_CONFIG)
    for rel in ("packages/devloop/docs/agents/constitution.md",
                "docs/agents/constitution.md"):
        assert any(paths.match(rel, p) for p in funloops["triage"]["watched_paths"]), rel
    template = cli.load_config(cli.REPO_ROOT / "docs" / "agents"
                               / "loop.toml.template")
    assert any(paths.match("docs/agents/constitution.md", p)
               for p in template["triage"]["watched_paths"])
