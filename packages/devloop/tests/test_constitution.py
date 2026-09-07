"""The constitution ships as a devloop default (issue #26, dec-1746aec3).

Seams under test — the two the issue names:

1. **Resolution** — ``cli.find_constitution``: a fresh repo with devloop
   installed resolves the packaged default with zero authoring; a repo's
   ``docs/agents/constitution.md`` overlay *extends* the default (appended
   after it), never replaces it. Same upward walk as loop.toml, opposite
   merge posture.
2. **Splice** — the persona is the single splice container (issue #41,
   dec-d79e8e7b): ``pack.splice`` inserts the resolved constitution at the
   persona's one marker line, and the assembled text — the REAL packaged
   persona, the REAL packaged rules, this repo's REAL overlay, in that
   order — is pinned to a golden. The command doc names exactly one splice
   point, and the reading-the-ladder clause lives in the persona alone.

Sources of truth are the issue's acceptance criteria (≤ 40 lines, seven
rules, no citation in any rule's text), dec-1746aec3's amendment convention
(watched_paths covers both layers), and the filesystem — never the code
under test.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from devloop import cli, pack, paths

DOCS = cli.REPO_ROOT / "docs" / "agents"
COMMAND_DOC = DOCS / "issue-loop.command.md"
FUNLOOPS_ROOT = cli.REPO_ROOT.parents[1]
GOLDEN_SPLICE = Path(__file__).resolve().parent / "fixtures" / "pack" / "splice.md"

# The former depth rider's most distinctive line — if it appears anywhere but
# the persona, the reading-the-ladder clause survived as a second mechanism.
LADDER_CLAUSE_PHRASE = "consolidate, not scatter"
# AC3 verbatim: an issue/PR number, a bare 7-hex sha, or "PR " in a rule.
CITATION = re.compile(r"#[0-9]{2,}|\b[0-9a-f]{7}\b|PR ")


# ---------------------------------------------------------------------------
# Resolution seam


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


def test_the_package_checkout_serves_its_own_copy_once():
    """Walking up from inside packages/devloop finds the packaged file itself;
    it must not be served twice (once as default, once as 'overlay')."""
    assert cli.find_constitution(cli.REPO_ROOT / "devloop") == [
        cli.PACKAGE_CONSTITUTION]


def test_an_identical_copy_in_another_checkout_is_served_once(tmp_path):
    """The loop's own operating mode: devloop imported from one checkout, cwd
    inside a git worktree of the same repo. The walk finds that worktree's
    tracked copy of the packaged file — same bytes, different path — and the
    rules must not be spliced twice."""
    repo = _repo(
        tmp_path,
        overlay=cli.PACKAGE_CONSTITUTION.read_text(encoding="utf-8"))
    assert cli.find_constitution(repo) == [cli.PACKAGE_CONSTITUTION]


def test_a_missing_packaged_default_is_loud(tmp_path, monkeypatch):
    """A wheel that shipped no docs/ must not resolve to a nonexistent path
    the orchestrator splices as silence — losing every rule unannounced is
    the fail-open rule 5 names. Resolution refuses instead."""
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
# The packaged file's own contract (the issue's criteria verbatim)


def test_constitution_is_one_screen():
    assert cli.PACKAGE_CONSTITUTION.exists(), \
        "the constitution must ship under docs/agents/"
    lines = cli.PACKAGE_CONSTITUTION.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 40, f"{len(lines)} lines — the criterion is ≤ 40"


def test_seven_rules_and_no_citation_in_any_rule():
    """dec-d79e8e7b's durability test: a packaged rule names the failure it
    prevents in general terms; provenance is a decision id in the PR that
    adds the rule, never in the text an installing repo cannot resolve."""
    rules = re.findall(r"^\d+\.\s.*$", pack.body(cli.PACKAGE_CONSTITUTION),
                       re.MULTILINE)
    assert len(rules) == 7, f"{len(rules)} rules — the settled set is seven"
    for rule in rules:
        assert not CITATION.search(rule), f"rule cites an incident: {rule}"


# ---------------------------------------------------------------------------
# Splice seam


def test_implementer_splice_is_golden():
    """AC1 — seam: ``pack.splice``, the pure function the pack verb calls,
    over the real packaged persona, the real packaged rules and this repo's
    real overlay (resolved from the funloops root, packaged first). The
    golden is authored from those files and compared byte-for-byte; the
    overlay's rules continue the packaged numbering (8, 9) so the implementer
    reads one list."""
    spliced = pack.splice(pack.body(cli.PACKAGE_PERSONA),
                          [pack.body(p) for p in cli.find_constitution(FUNLOOPS_ROOT)])
    assert spliced == GOLDEN_SPLICE.read_text(encoding="utf-8")


def test_exactly_one_splice_point_in_the_command_doc():
    """A single paragraph of the command doc names the file and defines the
    extend-not-replace resolution."""
    text = COMMAND_DOC.read_text(encoding="utf-8")
    naming = [p for p in text.split("\n\n") if "constitution.md" in p]
    assert len(naming) == 1, "constitution.md must be named at one splice point"
    assert "extends" in naming[0] and "never replaces" in naming[0]


def test_ladder_clause_lives_in_the_persona_alone():
    """The reading-the-ladder clause is dissolved into the persona
    (dec-d79e8e7b §3): no other packaged doc — the constitution included —
    carries it, so there is one home and no rider."""
    assert LADDER_CLAUSE_PHRASE in pack.body(cli.PACKAGE_PERSONA)
    for doc in DOCS.glob("*.md"):
        if doc.name == "ponytail-persona.md":
            continue
        assert LADDER_CLAUSE_PHRASE not in doc.read_text(encoding="utf-8"), \
            f"the ladder clause leaked into {doc.name}"


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
