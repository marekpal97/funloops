"""Tests for the deterministic rail of the issue-to-PR loop.

Everything here is pure: parsing and frontier computation take plain dicts
and strings — no gh, no git, no network.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys

import pytest

from devloop import cli, dag, gates, github, index_client, pack, triage
from devloop.trajectory import mint, prime

# ---------------------------------------------------------------------------
# blockers — native dependency edges only (#95: the body grammar is gone)


def test_blockers_are_the_native_edges():
    assert dag.blockers({"native_blockers": [17, 16]}) == [16, 17]
    assert dag.blockers({"native_blocked_count": 1}) == []
    assert dag.blockers({}) == []


# ---------------------------------------------------------------------------
# compute_frontier


CFG = {
    "labels": {"runnable": "ready-for-agent", "claimed": "agent-claimed"},
    "loop": {},
}


def _issue(number, state="OPEN", labels=("ready-for-agent",), body="", **extra):
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "labels": [{"name": l} for l in labels],
        "body": body,
        **extra,
    }


def test_frontier_requires_closed_blockers():
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, native_blockers=[1], native_blocked_count=0),
        _issue(3, native_blockers=[2], native_blocked_count=1),
    ]
    result = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2]
    assert [e["number"] for e in result["blocked"]] == [3]
    assert result["blocked"][0]["open_blockers"] == [2]


def test_frontier_excludes_unlabeled_and_claimed():
    issues = [
        _issue(1, labels=("bug",)),  # not ready-for-agent
        _issue(2, labels=("ready-for-agent", "agent-claimed")),
        _issue(3),
    ]
    result = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [3]
    assert [e["number"] for e in result["claimed"]] == [2]


def test_assignee_is_a_claim():
    issues = [
        _issue(1, assignees=[{"login": "marekpal97"}]),
        _issue(2),
    ]
    result = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2]
    assert result["claimed"][0]["assignees"] == ["marekpal97"]


def test_native_dependencies_gate_frontier():
    # native count gates even without the edge list
    issues = [_issue(2, native_blocked_count=1)]
    result = dag.compute_frontier(issues, CFG)
    assert result["frontier"] == []
    assert "native" in result["blocked"][0]["open_blockers_note"]
    # with the edge list, blockers are named and closure unblocks
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, native_blocked_count=0, native_blockers=[1]),
        _issue(3, native_blocked_count=1, native_blockers=[4]),
        _issue(4),
    ]
    result = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2, 4]
    assert result["blocked"][0]["open_blockers"] == [4]


def test_body_blocked_by_text_does_not_gate_the_frontier():
    """A body `Blocked-by:` line in an old issue is inert."""
    issues = [_issue(1), _issue(2, body="Blocked-by: #1")]
    result = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [1, 2]
    assert result["blocked"] == []
    assert result["frontier"][1]["blockers"] == []
    comp = dag.compute_components(issues)
    assert comp[1] != comp[2]


def test_components_split_unrelated_dags():
    issues = [
        _issue(1),
        _issue(2, native_blockers=[1], native_blocked_count=1),
        _issue(10),
        _issue(11, native_blockers=[10], native_blocked_count=1),
        _issue(20),  # isolated
    ]
    comp = dag.compute_components(issues)
    assert comp[1] == comp[2] == 1
    assert comp[10] == comp[11] == 10
    assert comp[20] == 20
    result = dag.compute_frontier(issues, CFG)
    by_num = {e["number"]: e for e in result["frontier"]}
    assert by_num[1]["component"] == 1 and by_num[10]["component"] == 10
    assert by_num[20]["component"] == 20


def test_components_ignore_closed_issues():
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, native_blockers=[1], native_blocked_count=0),
        _issue(3, native_blockers=[1], native_blocked_count=0),
    ]
    comp = dag.compute_components(issues)
    # 2 and 3 only share a CLOSED blocker — no open edge between them
    assert comp[2] != comp[3]


def test_frontier_is_number_ordered_and_body_metadata_is_not_read():
    """The DAG is the parallel set: an entry carries no wave or parallel-safe
    hint, and `Wave:` in a body no longer reorders the frontier."""
    issues = [_issue(6, body="Wave: 2 | Parallel-safe: no"), _issue(5), _issue(7, body="Wave: 1")]
    result = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [5, 6, 7]
    assert set(result["frontier"][0]) == {"number", "title", "blockers", "component"}
    limited = dag.compute_frontier(issues, CFG, limit=2)
    assert [e["number"] for e in limited["frontier"]] == [5, 6]
    assert limited["deferred"] == [7]


def test_plan_cuts_the_frontier_to_the_run_cap_and_mints_a_run_id(monkeypatch, capsys):
    """The frontier is this run's: at most ``max_issues_per_run``, in number
    order; the rest are named under ``deferred``, never hidden. ``--limit``
    overrides the cap, and every plan carries a ``loop-<date>-<hex>`` run id."""
    monkeypatch.setattr(github, "fetch_issues", lambda: [_issue(n) for n in range(1, 6)])
    assert cli.main(["plan", "--set", "max_issues_per_run=3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["number"] for e in out["frontier"]] == [1, 2, 3]
    assert out["deferred"] == [4, 5]
    assert re.fullmatch(r"loop-\d{8}-[0-9a-f]{4}", out["run_id"])
    assert cli.main(["plan", "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["number"] for e in out["frontier"]] == [1, 2] and out["deferred"] == [3, 4, 5]


def test_frontier_native_blocker_missing_from_snapshot_blocks_and_warns():
    """A native edge can point outside this repo's snapshot (cross-repo, or a
    deleted issue). GitHub counted it as blocking, so the rail must too —
    and say why, rather than silently holding the issue back."""
    issues = [_issue(2, native_blockers=[999], native_blocked_count=1)]
    result = dag.compute_frontier(issues, CFG)
    assert result["frontier"] == []
    assert result["blocked"][0]["open_blockers"] == [999]
    assert any("#999" in w for w in result["warnings"])


def test_closed_issues_never_in_frontier():
    issues = [_issue(1, state="CLOSED")]
    result = dag.compute_frontier(issues, CFG)
    assert result["frontier"] == [] and result["blocked"] == []


# ---------------------------------------------------------------------------
# command gate — a timeout is a gate RESULT, not a traceback (the orchestrator
# consumes JSON; a raw TimeoutExpired breaks the gate-result contract)


def test_command_gate_timeout_is_a_result_not_a_traceback(tmp_path):
    gate = {"id": "slow", "kind": "command", "cmd": "sleep 5", "timeout_sec": 0.2}
    result = gates.run_command_gate(gate, tmp_path)
    assert result["passed"] is False
    assert "timed out" in result["summary"]


# ---------------------------------------------------------------------------
# verify rail (dec-e267d040) — `check --issue N` runs the configured command
# gates, then the issue body's `verify:` lines, as one result list. A verify
# line is a shell command; exit 0 passes. Two seams: the pure parse (body →
# commands) and `check --issue`'s JSON stdout + exit code with the gh fetch
# replaced by a fixture body and the host config by a tmp repo.

# The grammar in the wild: a checklist item, the command backticked. The
# prose lines mention `verify:` too and must NOT parse.
VERIFY_BODY = """\
## What to build
A criterion written as `verify: <command>` is executed by the rail.
- New rail verb reads the `verify:` lines from its acceptance criteria.

A documented example is not a criterion:
```markdown
- [ ] verify: `rm -rf /`
```

## Acceptance criteria
- [ ] AC1: prose only — stays with the judge
- [x] verify: `! test -f packages/devloop/devloop/codemap.py`
- [ ] verify: `echo a => b`
* verify: `grep -qE 'map(\\.notes)?\\.json$' x`
"""


def test_parse_verify_lines_takes_the_checklist_grammar():
    """One command per line; ` => ` is part of the command, never a stdout clause."""
    assert gates.parse_verify_lines(VERIFY_BODY) == [
        "! test -f packages/devloop/devloop/codemap.py",
        "echo a => b",
        "grep -qE 'map(\\.notes)?\\.json$' x",
    ]
    assert gates.parse_verify_lines("no runnable criteria here") == []


PY = f'"{sys.executable}" -c'

# A host whose pipeline is one command gate and one diff gate.
CHECK_HOST = (f'[[gates]]\nid = "diff-guard"\nkind = "diff"\n'
              f'[[gates]]\nid = "tests"\nkind = "command"\ncmd = \'{PY} "print(1)"\'\n')


def _host_config(tmp_path, monkeypatch, text):
    """A host repo whose docs/agents/loop.toml is ``text``, as the cwd."""
    (tmp_path / ".git").mkdir()
    d = tmp_path / "docs" / "agents"
    d.mkdir(parents=True)
    (d / "loop.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def _issue_body(monkeypatch, body: str) -> None:
    """The gh seam: `check --issue` reads the body exactly as `pack` does."""
    monkeypatch.setattr(github, "run", lambda args, cwd=None: json.dumps({"body": body}))


def test_check_issue_runs_the_command_gates_then_the_verify_lines(tmp_path, monkeypatch, capsys):
    """One call, one list: the tests gate, then one result per verify line;
    the diff gate is not a command gate and stays out. A red line exits 1."""
    _host_config(tmp_path, monkeypatch, CHECK_HOST)
    _issue_body(monkeypatch, "- [ ] verify: `true`\n- [ ] verify: `false`\n")
    rc = cli.main(["check", "--issue", "7", "--cwd", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["issue"] == 7
    assert [(r["id"], r["kind"], r["passed"]) for r in out["results"]] == [
        ("tests", "command", True), ("verify:1", "command", True), ("verify:2", "command", False)]
    assert "`false`" in out["results"][2]["summary"]
    assert set(out["results"][2]) >= {"id", "kind", "passed", "summary", "detail"}
    assert out["summary"] == "2/3 passed"


def test_verify_line_passes_on_exit_zero_alone(tmp_path, monkeypatch, capsys):
    """No stdout clause: a line that prints `x => y` and exits 0 is green."""
    _host_config(tmp_path, monkeypatch, "")
    _issue_body(monkeypatch, "- [ ] verify: `echo hello => nope`\n")
    assert cli.main(["check", "--issue", "7", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["passed"] is True


def test_verify_rail_missing_binary_is_named_never_skipped(tmp_path, monkeypatch, capsys):
    """A command absent from PATH is a FAILED result whose summary names the
    binary (the shell's 127 / cmd.exe's 9009 'not found' line)."""
    _host_config(tmp_path, monkeypatch, "")
    _issue_body(monkeypatch, "- [ ] verify: `devloop-no-such-binary-xq --flag`\n")
    rc = cli.main(["check", "--issue", "7", "--cwd", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["results"][0]["passed"] is False
    assert "devloop-no-such-binary-xq" in out["results"][0]["summary"]


def test_check_issue_with_no_gates_and_no_lines_exits_zero(tmp_path, monkeypatch, capsys):
    _host_config(tmp_path, monkeypatch, "")
    _issue_body(monkeypatch, "- [ ] AC1: prose only\n")
    rc = cli.main(["check", "--issue", "25", "--cwd", str(tmp_path)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"issue": 25, "results": [], "summary": "0/0 passed"}


@pytest.mark.parametrize("cmd, mode, baseline", [
    ("print(1)", "auto", "green"),
    ("raise SystemExit(1)", "auto", "red"),
    ("raise SystemExit(1)", "always", "green"),
    ("print(1)", "never", "red"),
])
def test_check_baseline_runs_the_tests_gate_under_tdd_mode(tmp_path, monkeypatch, capsys,
                                                          cmd, mode, baseline):
    """The baseline line is the tests gate's colour under ``auto``; ``always``
    writes green and ``never`` red, whatever the gate said. No issue is read."""
    _host_config(tmp_path, monkeypatch,
                 f'[tdd]\nmode = "{mode}"\n[[gates]]\nid = "diff-guard"\nkind = "diff"\n'
                 f'[[gates]]\nid = "tests"\nkind = "command"\ncmd = \'{PY} "{cmd}"\'\n')
    monkeypatch.setattr(github, "run", lambda *a, **k: pytest.fail("baseline reads no issue"))
    assert cli.main(["check", "--baseline", "--cwd", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["baseline"] == baseline
    assert [r["id"] for r in out["results"]] == ["tests"]


def test_check_baseline_without_a_command_gate_is_an_error_under_auto(tmp_path, monkeypatch,
                                                                     capsys):
    """Under ``auto`` no command gate means no tests ran: exit 2 naming it,
    never a green baseline over an empty result list."""
    _host_config(tmp_path, monkeypatch, '[tdd]\nmode = "auto"\n')
    assert cli.main(["check", "--baseline", "--cwd", str(tmp_path)]) == 2
    out = json.loads(capsys.readouterr().out)
    assert "baseline" not in out and "command gate" in out["error"]


def test_verify_rail_gh_failure_is_the_error_rung(tmp_path, monkeypatch, capsys):
    """A body that cannot be read is exit 2 with {"error"}, like every other
    `check` that never starts — exit 1 would read as 'a verify line is red'."""
    def boom(args, cwd=None):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="no issue 999999")
    monkeypatch.setattr(github, "run", boom)
    rc = cli.main(["check", "--issue", "999999", "--cwd", str(tmp_path)])
    assert rc == 2
    assert "no issue 999999" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# diff gate — pure evaluation over numstat text


def test_diff_gate_forbidden_path():
    gate = {"id": "g", "forbidden_paths": [".github/workflows/"]}
    numstat = "3\t1\tsrc/thinkweave/core/config.py\n2\t0\t.github/workflows/ci.yml\n"
    result = gates.evaluate_diff_gate(gate, numstat)
    assert result["passed"] is False
    assert ".github/workflows/ci.yml" in result["summary"]


def test_diff_gate_forbidden_paths_use_the_three_form_convention():
    """Issue #94's one unification: forbidden_paths goes through paths.match,
    so it ADOPTS the convention triage already used. Trailing-slash entries (all
    the shipped ones) are byte-identical startswith; a bare name now matches
    that basename at any depth instead of only at the repo root."""
    gate = {"id": "g", "forbidden_paths": ["dist/", "secrets.env"]}
    # dir prefix: unchanged semantics.
    assert gates.evaluate_diff_gate(gate, "1\t0\tdist/bundle.js\n")["passed"] is False
    # bare basename: matches at depth, and a prefix-sharing sibling does not.
    assert gates.evaluate_diff_gate(gate, "1\t0\tops/secrets.env\n")["passed"] is False
    assert gates.evaluate_diff_gate(gate, "1\t0\tops/secrets.env.example\n")["passed"] is True


def test_diff_gate_is_a_forbidden_paths_check_only():
    """dec-cf8f0d33: the line cap is gone — diff-guard never fails on size.
    A 5,000-line diff touching nothing forbidden passes; the summary still
    reports the count as information for the PR body."""
    gate = {"id": "g", "forbidden_paths": [".github/workflows/"]}
    result = gates.evaluate_diff_gate(gate, "3000\t2000\tsrc/big.py\n")
    assert result["passed"] is True
    assert "5000 changed lines" in result["summary"]


def test_diff_gate_passes_and_handles_binary():
    gate = {"id": "g", "forbidden_paths": ["vault/"]}
    numstat = "4\t3\tsrc/a.py\n-\t-\tassets/logo.png\n"
    result = gates.evaluate_diff_gate(gate, numstat)
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# config


def test_load_config_defaults_when_missing(tmp_path):
    cfg = cli.load_config(tmp_path / "nope.toml")
    assert cfg["loop"]["max_issues_per_run"] == 3
    assert cfg["loop"]["require_green_baseline"] is True
    assert cfg["loop"]["run_mode"] == "pass"
    assert cfg["labels"]["runnable"] == "ready-for-agent"
    assert cfg["tdd"]["mode"] == "auto"
    assert cfg["gates"] == []


def test_load_config_tdd_override(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[tdd]\nmode = "never"\n', encoding="utf-8")
    assert cli.load_config(p)["tdd"]["mode"] == "never"


def test_load_config_merges_file(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text(
        '[loop]\nmax_issues_per_run = 5\n\n[[gates]]\nid = "tests"\nkind = "command"\ncmd = "pytest"\n',
        encoding="utf-8",
    )
    cfg = cli.load_config(p)
    assert cfg["loop"]["max_issues_per_run"] == 5
    assert cfg["loop"]["max_fix_rounds"] == 2  # default survives partial override
    assert cfg["gates"][0]["id"] == "tests"


def test_gate_pipeline_order_is_pinned():
    """The full pipeline order is a contract: diff-guard → tests → judge.
    The cheap deterministic gates run first; the one judge stage follows and
    is the last. No map gate, no simplify gate."""
    cfg = cli.load_config()
    ids = [g["id"] for g in cfg["gates"]]
    assert ids == ["diff-guard", "tests", "judge"]


# ---------------------------------------------------------------------------
# trajectory payload (memory-feed proposal) — pure assembly


def test_build_trajectory_payload():
    issue = {
        "number": 26,
        "title": "D1: Queue.items_since() — close the archive leak",
        "html_url": "https://github.com/x/y/issues/26",
        "labels": [{"name": "ready-for-agent"}, {"name": "track:D-acquisition"}],
    }
    payload = mint.build_trajectory(
        issue,
        branch="loop/issue-26",
        commits=["a" * 40, "b" * 40],
        numstat="10\t2\tsrc/thinkweave/acquisition/queue.py\n5\t0\ttests/test_queue.py\n",
        gates=[{"id": "tests", "kind": "command", "passed": True, "summary": "exit 0"}],
        fix_rounds=1,
        outcome="shipped",
        pr_url="https://github.com/x/y/pull/99",
        run_id="loop-20260713-abcd",
    )
    fm = payload["frontmatter"]
    assert payload["type"] == "note" and payload["tags"] == ["loop-run"]
    assert fm["issue"] == 26 and fm["outcome"] == "shipped" and fm["fix_rounds"] == 1
    assert fm["commits"] == 2
    assert fm["commit_shas"] == ["a" * 40, "b" * 40]
    assert fm["epic_title"] == ""
    assert fm["files_touched"] == ["src/thinkweave/acquisition/queue.py", "tests/test_queue.py"]
    assert fm["gates"] == [{"id": "tests", "passed": True, "summary": "exit 0"}]
    assert "track:D-acquisition" in payload["concept_hints"]
    # Issue #85: the Lessons section is retired from the body skeleton — the
    # run-causal register is What / How it went only.
    assert "## How it went" in payload["body_skeleton"]
    assert "## Lessons" not in payload["body_skeleton"]


def test_build_trajectory_defaults_to_empty_skills():
    """Existing callers pass no skills data (backward compat): frontmatter
    carries an empty skills[] and the record stays a plain [loop-run] note —
    no [skill-invocation] tag."""
    payload = mint.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
    )
    assert payload["frontmatter"]["skills"] == []
    assert payload["tags"] == ["loop-run"]


def test_build_trajectory_skills_shape():
    """skills[] normalizes each dispatched stage into {id, role, skill, outcome,
    fix_rounds_attributed}, preserving dispatch order, dropping extra keys,
    and defaulting a missing skill name to "" and a missing attribution count
    to 0. The skill-centric flag adds the [skill-invocation] tag. Expected
    values are hand-written from the issue's frontmatter schema, not
    recomputed by the code under test."""
    issue = {"number": 56, "title": "Generalize the trajectory note", "labels": []}
    skills_log = [
        {"id": "implementer", "role": "implementer", "outcome": "shipped",
         "fix_rounds_attributed": 0, "worktree": "/tmp/wt"},  # extra key dropped, no skill
        {"id": "acceptance-judge", "role": "acceptance", "skill": "code-review",
         "outcome": "not-met", "fix_rounds_attributed": 2},
        {"id": "code-reviewer", "role": "reviewer", "skill": "ponytail-review",
         "outcome": "passed"},  # no count → 0
    ]
    payload = mint.build_trajectory(
        issue, branch="loop/dag-54", commits=["a"], numstat="1\t0\tx.py\n",
        gates=[{"id": "acceptance", "kind": "acceptance", "passed": True, "summary": ""}],
        fix_rounds=2, outcome="shipped", skills=skills_log, skill_centric=True,
    )
    assert payload["frontmatter"]["skills"] == [
        {"id": "implementer", "role": "implementer", "skill": "",
         "outcome": "shipped", "fix_rounds_attributed": 0},
        {"id": "acceptance-judge", "role": "acceptance", "skill": "code-review",
         "outcome": "not-met", "fix_rounds_attributed": 2},
        {"id": "code-reviewer", "role": "reviewer", "skill": "ponytail-review",
         "outcome": "passed", "fix_rounds_attributed": 0},
    ]
    assert payload["tags"] == ["loop-run", "skill-invocation"]


def test_skill_row_names_the_skill_that_ran_the_stage():
    """Every skills-json row carries the skill that ran the stage: the name
    is passed through verbatim, and a dispatch that ran no skill defaults to
    "". Expected values are hand-written from the issue's criterion."""
    payload = _trajectory_with_skills([
        {"id": "judge", "role": "judge", "skill": "code-review"},
        {"id": "implementer", "role": "implementer"},
    ])
    assert [(s["id"], s["skill"]) for s in payload["frontmatter"]["skills"]] == [
        ("judge", "code-review"),
        ("implementer", ""),
    ]


DISPATCH_KEYS = {
    "transport": "agent-tool", "harness": "claude-code", "model": "claude-opus-5",
    "effort": "high", "session_ref": "sess-01ABC", "duration_sec": 412, "tokens": 183_000,
}


def _trajectory_with_skills(skills):
    return mint.build_trajectory(
        {"number": 14, "title": "join keys", "labels": []},
        branch="loop/issue-14", commits=["a"], numstat="",
        gates=[], fix_rounds=0, outcome="shipped", skills=skills,
    )


def test_skill_record_carries_the_dispatch_join_keys_verbatim():
    """A stage record with all seven dispatch join keys lands in frontmatter
    with every value unchanged; the five contracted fields stay beside them.
    A `tier` key is no join key any more and is dropped."""
    payload = _trajectory_with_skills([
        {"id": "implementer", "role": "implementer", "skill": "code-review",
         "outcome": "shipped", "fix_rounds_attributed": 1, **DISPATCH_KEYS, "tier": "small"},
    ])
    assert payload["frontmatter"]["skills"] == [
        {"id": "implementer", "role": "implementer", "skill": "code-review",
         "outcome": "shipped", "fix_rounds_attributed": 1, **DISPATCH_KEYS},
    ]


@pytest.mark.parametrize("bad, path, shown", [
    ({"tokens": "many"}, "skills[0].tokens", "'many'"),
    ({"duration_sec": True}, "skills[0].duration_sec", "True"),
    ({"transport": "carrier-pigeon"}, "skills[0].transport", "'carrier-pigeon'"),
    ({"model": 5}, "skills[0].model", "5"),
])
def test_wrong_typed_join_key_is_rejected_with_its_field_path(bad, path, shown):
    """A wrong-typed join key raises, and each reason names the offending field
    path and the value it saw, in the validate verb's style."""
    with pytest.raises(ValueError) as exc:
        _trajectory_with_skills([
            {"id": "implementer", "role": "implementer", "outcome": "shipped", **bad},
        ])
    reasons = list(exc.value.args)
    assert len(reasons) == 1
    assert reasons[0].startswith(path + ":")
    assert shown in reasons[0]


def test_trajectory_verb_prints_join_key_reasons(tmp_path, monkeypatch, capsys):
    """The rail surfaces a rejected skills-json as `{error, reasons}` and exits
    2, so the orchestrator sees each field path, not one joined string."""
    monkeypatch.setattr(github, "fetch_parent", lambda repo, number: None)
    monkeypatch.setattr(github, "run", lambda args, cwd=None: json.dumps(
        {"number": 14, "title": "join keys", "labels": []}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="", stderr=""))
    (tmp_path / "g.json").write_text("[]", encoding="utf-8")
    (tmp_path / "s.json").write_text(json.dumps([
        {"id": "implementer", "role": "implementer", "outcome": "shipped",
         "tokens": "many", "duration_sec": True},
    ]), encoding="utf-8")
    rc = cli.main(["trajectory", "14", "--cwd", str(tmp_path),
                   "--gates-json", str(tmp_path / "g.json"),
                   "--skills-json", str(tmp_path / "s.json"), "--outcome", "shipped"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert [r.split(":")[0] for r in out["reasons"]] == [
        "skills[0].duration_sec", "skills[0].tokens"]


def test_trajectory_reads_a_branch_after_the_worktree_is_removed(tmp_path, monkeypatch, capsys):
    """Acceptance (#52): §1d removes the implementer worktree once the PR is
    open, then §3 records the trajectory. The verb takes --branch and reads
    that ref from the main checkout, so the payload still carries the loop
    branch and its non-zero commit count, not the main checkout's HEAD.
    Expected values are hand-written from the two commits made below."""
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()

    def git(*argv: str, cwd=repo) -> str:
        return subprocess.run(["git", *argv], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    git("worktree", "add", "-q", "-b", "loop/issue-52", str(worktree))
    for name, text in (("a.txt", "a\n"), ("b.txt", "b\n")):
        (worktree / name).write_text(text, encoding="utf-8")
        git("add", "-A", cwd=worktree)
        git("commit", "-qm", f"slice {name}", cwd=worktree)
    git("worktree", "remove", str(worktree))  # §1d teardown, before §3
    assert not worktree.exists() and git("rev-parse", "loop/issue-52").strip()

    monkeypatch.setattr(github, "fetch_parent", lambda repo, number: None)
    monkeypatch.setattr(github, "run", lambda args, cwd=None: json.dumps(
        {"number": 52, "title": "branch after teardown", "labels": []}))
    (tmp_path / "g.json").write_text("[]", encoding="utf-8")
    rc = cli.main(["trajectory", "52", "--cwd", str(repo),
                   "--branch", "loop/issue-52", "--base-ref", base,
                   "--gates-json", str(tmp_path / "g.json"),
                   "--outcome", "shipped"])
    assert rc == 0
    fm = json.loads(capsys.readouterr().out)["frontmatter"]
    assert fm["branch"] == "loop/issue-52"
    assert fm["commits"] == 2
    assert fm["commit_shas"] == git("rev-list", "--reverse", f"{base}..loop/issue-52").split()
    assert fm["files_touched"] == ["a.txt", "b.txt"]


def _trajectory_cli(tmp_path, monkeypatch, parent):
    """Run the trajectory verb with gh faked: the issue item for the issue
    endpoint, ``parent`` (a REST item, or ``None`` for gh's 404) for the
    parent endpoint."""
    def fake_run(args, cwd=None):
        if args[-1].endswith("/parent"):
            if parent is None:
                raise subprocess.CalledProcessError(
                    1, args, stderr="gh: Not Found (HTTP 404)")
            return json.dumps(parent)
        return json.dumps({"number": 88, "title": "one home per fact", "labels": [],
                           "html_url": "https://github.com/o/r/issues/88"})
    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="", stderr=""))
    (tmp_path / "g.json").write_text("[]", encoding="utf-8")
    return cli.main(["trajectory", "88", "--cwd", str(tmp_path), "--branch", "loop/dag-89",
                     "--gates-json", str(tmp_path / "g.json"), "--outcome", "shipped"])


def test_trajectory_payload_carries_the_sub_issue_parent_as_epic_url(tmp_path, monkeypatch, capsys):
    """A run lands on its epic's task: the payload's `epic_url` and
    `epic_title` are the native sub-issue parent's, and empty when the issue
    has no parent; `commit_shas` is emitted either way."""
    assert _trajectory_cli(tmp_path, monkeypatch, {
        "number": 89, "title": "devloop subtraction",
        "html_url": "https://github.com/o/r/issues/89"}) == 0
    fm = json.loads(capsys.readouterr().out)["frontmatter"]
    assert fm["epic_url"] == "https://github.com/o/r/issues/89"
    assert fm["epic_title"] == "devloop subtraction"
    assert _trajectory_cli(tmp_path, monkeypatch, None) == 0
    fm = json.loads(capsys.readouterr().out)["frontmatter"]
    assert fm["epic_url"] == "" and fm["epic_title"] == ""
    assert fm["commit_shas"] == []


def test_fetch_parent_raises_on_a_failure_that_is_not_a_missing_parent(monkeypatch):
    """Only gh's 404 means "no parent"; any other failure raises, so a
    network error never records a run as epic-less."""
    def boom(args, cwd=None):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 502: Bad Gateway")
    monkeypatch.setattr(github, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        github.fetch_parent("o/r", 88)


def test_stage_row_carries_its_posture():
    """The shape pass is role `judge`, posture `shape`: a stage row keeps a
    posture the pack knows, and a posture it does not know is rejected."""
    payload = _trajectory_with_skills([
        {"id": "judge:shape", "role": "judge", "outcome": "met", "posture": "shape"}])
    assert payload["frontmatter"]["skills"][0]["posture"] == "shape"
    with pytest.raises(ValueError) as exc:
        _trajectory_with_skills([{"id": "judge", "role": "judge", "posture": "simplify"}])
    assert exc.value.args[0].startswith("skills[0].posture:")


def test_trace_rounds_key_is_now_reviews():
    """The judge/fix rounds travel as `reviews`; the old `rounds` key is
    dropped, so a task note never says "rounds" at two levels."""
    payload = mint.build_trajectory(
        {"number": 3, "title": "x", "labels": []}, branch="b", commits=[], numstat="",
        gates=[], fix_rounds=0, outcome="shipped",
        trace={"rounds": [{"gate": "judge"}], "reviews": [{"gate": "judge"}]})
    assert set(payload["frontmatter"]["trace"]) == {"reviews"}


def test_trajectory_argparse_contract():
    """The trajectory subcommand exposes --skills-json (optional, default
    None) and --skill-centric (store_true, default False), so the
    orchestrator can pass its dispatch log and mark skill-centric records."""
    ns = cli.build_arg_parser().parse_args([
        "trajectory", "56", "--gates-json", "g.json",
        "--skills-json", "s.json", "--skill-centric",
        "--outcome", "shipped",
    ])
    assert ns.skills_json == "s.json"
    assert ns.skill_centric is True
    ns2 = cli.build_arg_parser().parse_args([
        "trajectory", "56", "--gates-json", "g.json", "--outcome", "shipped",
    ])
    assert ns2.skills_json is None
    assert ns2.skill_centric is False


def test_build_trajectory_mirrors_primed_and_served():
    """The frontmatter mirrors the prime verdict: primed:true + the served ids
    when the run was primed; primed:false + empty served when held out."""
    issue = {"number": 57, "title": "prime the implementer", "labels": []}
    primed = mint.build_trajectory(
        issue, branch="loop/dag-54", commits=["a"], numstat="1\t0\tx.py\n",
        gates=[], fix_rounds=0, outcome="shipped",
        primed=True, served=["n-prior1", "dec-abc222"],
    )
    assert primed["frontmatter"]["primed"] is True
    assert primed["frontmatter"]["served"] == ["n-prior1", "dec-abc222"]

    held = mint.build_trajectory(
        issue, branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped", primed=False,
    )
    assert held["frontmatter"]["primed"] is False
    assert held["frontmatter"]["served"] == []


def test_build_trajectory_omits_prime_keys_when_unknown():
    """Backward compat: callers that pass no prime data (primed=None) get a
    note with no primed/served keys — the pre-#57 shape is unchanged."""
    payload = mint.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
    )
    assert "primed" not in payload["frontmatter"]
    assert "served" not in payload["frontmatter"]


def test_trajectory_prime_argparse_contract():
    """--primed/--no-primed (default None) + --served-json (default None) let
    the orchestrator mirror the prime verdict into the trajectory note."""
    ns = cli.build_arg_parser().parse_args([
        "trajectory", "57", "--gates-json", "g.json", "--outcome", "shipped",
        "--primed", "--served-json", "served.json",
    ])
    assert ns.primed is True and ns.served_json == "served.json"
    ns_held = cli.build_arg_parser().parse_args([
        "trajectory", "57", "--gates-json", "g.json", "--outcome", "routed-to-human",
        "--no-primed",
    ])
    assert ns_held.primed is False
    ns_default = cli.build_arg_parser().parse_args([
        "trajectory", "57", "--gates-json", "g.json", "--outcome", "shipped",
    ])
    assert ns_default.primed is None and ns_default.served_json is None


# ---------------------------------------------------------------------------
# --dag scoping and --assume-done (stacked delivery)


def test_scope_to_dag_keeps_component_and_closed_issues():
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, native_blockers=[1], native_blocked_count=0),  # comp 2 (edge to 1 is closed)
        _issue(3, native_blockers=[2], native_blocked_count=1),  # comp 2
        _issue(10),                          # unrelated component
    ]
    scoped = dag.scope_to_dag(issues, 3)
    nums = sorted(i["number"] for i in scoped)
    assert nums == [1, 2, 3]  # closed #1 kept for blocker checks, #10 dropped


def test_scope_to_dag_rejects_closed_or_missing_root():
    with pytest.raises(ValueError):
        dag.scope_to_dag([_issue(1, state="CLOSED")], 1)
    with pytest.raises(ValueError):
        dag.scope_to_dag([_issue(1)], 99)


def test_assume_done_unblocks_dependents():
    issues = [
        _issue(16),
        _issue(21, native_blockers=[16], native_blocked_count=1),
        _issue(22, native_blockers=[16], native_blocked_count=1),
    ]
    before = dag.compute_frontier(issues, CFG)
    assert [e["number"] for e in before["frontier"]] == [16]
    after = dag.compute_frontier(
        dag.apply_assume_done(issues, {16}), CFG)
    assert [e["number"] for e in after["frontier"]] == [21, 22]


def test_assume_done_does_not_mutate_input():
    issues = [_issue(16)]
    dag.apply_assume_done(issues, {16})
    assert issues[0]["state"] == "OPEN"


# ---------------------------------------------------------------------------
# --set overrides — per-run posture on top of loop.toml defaults


def test_parse_override_section_defaults_to_loop():
    assert cli.parse_override("delivery=stacked") == ("loop", "delivery", "stacked")


def test_parse_override_toml_scalars():
    assert cli.parse_override("max_issues_per_run=6") == ("loop", "max_issues_per_run", 6)
    assert cli.parse_override("training_mode=false") == ("loop", "training_mode", False)
    assert cli.parse_override('tdd.mode="never"') == ("tdd", "mode", "never")
    assert cli.parse_override("labels.runnable=agent-go") == ("labels", "runnable", "agent-go")


def test_parse_override_malformed():
    for bad in ("delivery", "=stacked", "delivery=", ""):
        with pytest.raises(ValueError):
            cli.parse_override(bad)


def test_apply_overrides_wins_over_file(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[loop]\ndelivery = "pr-per-issue"\nmax_issues_per_run = 3\n', encoding="utf-8")
    cfg = cli.apply_overrides(
        cli.load_config(p), ["delivery=stacked", "max_issues_per_run=6"]
    )
    assert cfg["loop"]["delivery"] == "stacked"
    assert cfg["loop"]["max_issues_per_run"] == 6


def test_apply_overrides_rejects_unknown_key_and_section(tmp_path):
    cfg = cli.load_config(tmp_path / "nope.toml")
    with pytest.raises(ValueError, match="unknown key"):
        cli.apply_overrides(cfg, ["deliverey=stacked"])  # typo protection
    with pytest.raises(ValueError, match="not overridable"):
        cli.apply_overrides(cfg, ["gates.tests=off"])  # gates are file-only


def test_apply_overrides_noop_without_specs(tmp_path):
    cfg = cli.load_config(tmp_path / "nope.toml")
    assert cli.apply_overrides(cfg, []) is cfg


# --- [dispatch] — which agent runs a role is run posture, per role -----------


def test_load_config_without_dispatch_resolves_every_role_to_the_agent_tool(tmp_path):
    """No `[dispatch]` table: every configured role is the Agent tool with no
    harness, model, effort, args or posture — today's behaviour, spelled out."""
    cfg = cli.load_config(tmp_path / "nope.toml")
    assert cfg["dispatch"] == {
        "implementer": {"transport": "agent-tool"},
        "judge": {"transport": "agent-tool"},
    }


def test_load_config_merges_a_dispatch_role_over_the_default(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[dispatch.judge]\ntransport = "herdr"\nharness = "codex"\n'
                 'args = ["-m", "o3"]\n', encoding="utf-8")
    cfg = cli.load_config(p)
    assert cfg["dispatch"]["judge"] == {
        "transport": "herdr", "harness": "codex", "args": ["-m", "o3"]}
    assert cfg["dispatch"]["implementer"] == {"transport": "agent-tool"}


def test_dispatch_override_via_set_sets_one_key_of_one_role(tmp_path):
    cfg = cli.apply_overrides(cli.load_config(tmp_path / "nope.toml"),
                              ["dispatch.judge.harness=codex",
                               'dispatch.judge.args=["--model", "opus"]'])
    assert cfg["dispatch"]["judge"] == {
        "transport": "agent-tool", "harness": "codex", "args": ["--model", "opus"]}
    assert cfg["dispatch"]["implementer"] == {"transport": "agent-tool"}


def test_dispatch_refuses_an_unknown_key_by_name_on_both_paths(tmp_path):
    cfg = cli.load_config(tmp_path / "nope.toml")
    with pytest.raises(ValueError, match="unknown key 'dispatch.judge.bogus'"):
        cli.apply_overrides(cfg, ["dispatch.judge.bogus=x"])
    p = tmp_path / "loop.toml"
    p.write_text('[dispatch.judge]\nbogus = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key 'dispatch.judge.bogus'"):
        cli.load_config(p)


def test_a_dispatch_entry_declares_a_role_on_both_paths(tmp_path):
    """A role is a `[dispatch]` entry (funloops#65): an entry the defaults do
    not carry declares a new role over the default transport, whether the
    file or `--set` declares it; the default roles stay as they were."""
    declared = {"transport": "agent-tool", "posture": "reader"}
    p = tmp_path / "loop.toml"
    p.write_text('[dispatch.reviewer]\nposture = "reader"\n', encoding="utf-8")
    cfg = cli.load_config(p)
    assert cfg["dispatch"]["reviewer"] == declared
    assert cfg["dispatch"]["judge"] == {"transport": "agent-tool"}
    cfg = cli.apply_overrides(cli.load_config(tmp_path / "nope.toml"),
                              ["dispatch.reviewer.posture=reader"])
    assert cfg["dispatch"]["reviewer"] == declared


@pytest.mark.parametrize("entry", [
    'transport = "ssh"',        # not agent-tool | herdr
    'posture = "editor"',       # not writer | reader | shape
    'posture = ["reader", "editor"]',  # a list of postures holds only postures
    "posture = []",             # an entry that names a posture names one at least
    'args = "--model opus"',    # the argv tail is a list, not one string
    "model = 4",                # a name, not a number
])
def test_dispatch_refuses_a_value_of_the_wrong_shape_naming_the_key(tmp_path, entry):
    """Shape only: transport and posture are closed sets, args is a list of
    strings, the rest are strings. Model and harness names are never checked
    against a list."""
    p = tmp_path / "loop.toml"
    p.write_text(f"[dispatch.implementer]\n{entry}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dispatch.implementer." + entry.split(" ")[0]):
        cli.load_config(p)


def test_template_dispatch_table_names_no_host_value():
    """What resolves from the template's dispatch table names no harness,
    model, effort or argv tail."""
    template_path = cli.REPO_ROOT / "docs" / "agents" / "loop.toml.template"
    dispatch = cli.load_config(template_path)["dispatch"]
    assert set(dispatch) == {"implementer", "judge"}
    for role, entry in dispatch.items():
        assert entry["transport"] == "agent-tool", role
        assert not {"harness", "model", "effort", "args"} & set(entry), role
    assert dispatch["implementer"]["posture"] == "writer"
    assert dispatch["judge"]["posture"] == ["reader", "shape"]  # the per-slice and stack-tip passes


# --- deleted keys are rejected, named (issue #39 AC2, dec-cf8f0d33) ---------
# The same unknown-key check `--set` always had now runs on the file too: a
# knob the subtractive pass deleted is neither silently honored nor silently
# ignored — the rail refuses the config and names the key.

DELETED_SCALARS = [
    ("loop", "prime_holdout", "5"),
    ("loop", "draft_pr", "true"),
    ("loop", "claim_mode", '"assign"'),
    ("triage", "green_enabled", "false"),
    ("triage", "green_max_diff_lines", "150"),
    ("triage", "green_requires_first_try", "true"),
    ("loop", "max_parallel", "1"),
    ("triage", "red_min_diff_lines", "800"),
]


@pytest.mark.parametrize("section,key,value", DELETED_SCALARS)
def test_load_config_rejects_deleted_scalar_keys_naming_them(tmp_path, section, key, value):
    p = tmp_path / "loop.toml"
    p.write_text(f"[{section}]\n{key} = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"unknown key '{section}\.{key}'"):
        cli.load_config(p)


def test_load_config_rejects_the_deleted_dispatch_persona_key(tmp_path):
    """`[dispatch] persona` is gone (dec-d79e8e7b addendum): the persona is
    spliced by posture. The section now holds per-role tables, so the old
    scalar is refused by name as a role entry that is not a table."""
    p = tmp_path / "loop.toml"
    p.write_text("[dispatch]\npersona = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dispatch.persona: expected a table"):
        cli.load_config(p)


@pytest.mark.parametrize("kind,key,value", [
    ("diff", "max_changed_lines", "2000"),
    ("judge", "block_on", '["critical", "major"]'),
    ("judge", "smells_baseline", "true"),
    ("judge", "threshold", '"all"'),
])
def test_load_config_rejects_deleted_gate_keys_naming_them(tmp_path, kind, key, value):
    """Gate entries are checked against what their kind's verb reads; a deleted
    key is named by its position and name so the fix is one line."""
    p = tmp_path / "loop.toml"
    p.write_text(f'[[gates]]\nid = "g"\nkind = "{kind}"\n{key} = {value}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=rf"gates\[0\]\.{key}"):
        cli.load_config(p)


@pytest.mark.parametrize("entry,kind", [
    ('kind = "review"\nblock_on = ["major"]', "'review'"),  # pre-#39 gate, stale keys
    ('kind = "simplify"\nmin_diff_lines = 50', "'simplify'"),  # the deleted stage
    ('cmd = "true"', "None"),                               # no kind: config error, not KeyError
])
def test_load_config_rejects_stale_or_missing_gate_kind(tmp_path, entry, kind):
    p = tmp_path / "loop.toml"
    p.write_text(f'[[gates]]\nid = "g"\n{entry}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=rf"unknown kind {kind} at gates\[0\]"):
        cli.load_config(p)


def test_the_deleted_small_tier_is_refused_by_name(tmp_path, capsys):
    """`[dispatch.small]` is no tier any more: its threshold is an unknown
    dispatch key, and `config --diff-lines` is gone."""
    p = tmp_path / "loop.toml"
    p.write_text("[dispatch.small]\nmax_diff_lines = 200\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key 'dispatch.small.max_diff_lines'"):
        cli.load_config(p)
    with pytest.raises(SystemExit):
        cli.build_arg_parser().parse_args(["config", "--diff-lines", "10"])


def test_config_verb_names_the_deleted_key_and_exits_2(tmp_path, capsys, monkeypatch):
    """End to end: a host whose loop.toml still carries a deleted knob gets an
    error JSON naming it from every verb, not a config that quietly dropped it."""
    (tmp_path / ".git").mkdir()
    d = tmp_path / "docs" / "agents"
    d.mkdir(parents=True)
    (d / "loop.toml").write_text("[loop]\nprime_holdout = 5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config"]) == 2
    assert "loop.prime_holdout" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# prime — claim-time priming from prior trajectories


def test_render_prime_block_splices_insight_bodies_and_lists_served():
    trajectories = [
        {"id": "n-aaa111", "title": "prime rail", "issue": 57, "outcome": "shipped",
         "insights": [{"id": "n-ins1", "body": "Widen the CHECK first."}]},
        {"id": "n-bbb222", "title": "trajectory judge", "issue": 60, "outcome": "shipped",
         "insights": [{"id": "n-ins2", "body": "Judge from the PR timeline."}]},
    ]
    block, served = prime.render_prime_block(
        trajectories,
        decisions=[{"id": "dec-ccc333", "title": "Widen before you split",
                    "summary": "The CHECK is the seam."}],
    )
    assert "Widen the CHECK first." in block
    assert "Judge from the PR timeline." in block
    assert "dec-ccc333" in block
    assert served == ["n-ins1", "n-ins2", "dec-ccc333"]
    # Nothing to serve → clean skip.
    assert prime.render_prime_block([], decisions=[]) == ("", [])


def test_render_prime_block_honors_char_budget():
    trajectories = [
        {"id": f"n-{i}", "title": f"t{i}", "issue": i, "outcome": "shipped",
         "insights": [{"id": f"n-ins{i}", "body": c * 400}]}
        for i, c in ((1, "L"), (2, "M"), (3, "N"))
    ]
    _block, served = prime.render_prime_block(trajectories, budget_chars=600)
    # First piece always lands; the budget stops further pieces before all three.
    assert served == ["n-ins1"]
    assert "n-ins2" not in served and "n-ins3" not in served


def test_build_prime_payload_no_index_is_a_clean_noop():
    # No conn (index absent) → empty, no crash, loop unchanged.
    payload = prime.build_prime_payload(
        57, "loop-run-0", ["self-improvement"], conn=None,
    )
    assert payload["primed"] is False
    assert payload["served"] == [] and payload["block"] == ""


def _seed_index_db(path, *, note_id, title, concepts, body, tags=("loop-run",),
                   date="2026-07-18", frontmatter=None):
    """Build a minimal read-side index db (notes + note_tags + note_concepts)
    with one trajectory note — the exact tables prime's query joins."""
    import json as _json
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, type TEXT, title TEXT, path TEXT,"
        " date TEXT, frontmatter TEXT, body_text TEXT, tags TEXT);"
        "CREATE TABLE note_tags (note_id TEXT, tag TEXT);"
        "CREATE TABLE note_concepts (note_id TEXT, concept TEXT);"
    )
    conn.execute(
        "INSERT INTO notes (id, type, title, path, date, frontmatter, body_text)"
        " VALUES (?, 'note', ?, ?, ?, ?, ?)",
        (note_id, title, f"{note_id}.md", date,
         _json.dumps(frontmatter or {"issue": 57, "outcome": "shipped"}), body),
    )
    for tag in tags:
        conn.execute("INSERT INTO note_tags VALUES (?, ?)", (note_id, tag))
    for c in concepts:
        conn.execute("INSERT INTO note_concepts VALUES (?, ?)", (note_id, c))
    conn.commit()
    conn.close()


def test_query_trajectories_filters_by_concept_and_linked_insight(tmp_path):
    db = tmp_path / "index.db"
    # Note A matches the concept and links an insight; note B matches the
    # concept but links nothing — no reusable color, so it is not a candidate.
    _seed_index_db(db, note_id="n-hasl", title="A", concepts=["retrieval"],
                   body="## What\nx\n\n## How it went\ny\n",
                   frontmatter={"issue": 57, "outcome": "shipped",
                                "builds_on": ["n-ins1"]})
    _add_note(db, note_id="n-ins1", title="portable lesson", body="reuse me")
    wconn = sqlite3.connect(str(db))
    wconn.execute("INSERT INTO notes (id, type, title, path, date, frontmatter, body_text)"
                  " VALUES ('n-nol', 'note', 'B', 'n-nol.md', '2026-07-18', '{}', '## What\nno links\n')")
    wconn.execute("INSERT INTO note_tags VALUES ('n-nol', 'loop-run')")
    wconn.execute("INSERT INTO note_concepts VALUES ('n-nol', 'retrieval')")
    wconn.commit()
    wconn.close()
    conn = index_client.open_ro(str(db))
    try:
        # Concept miss → nothing.
        assert prime.query_trajectories(conn, ["unrelated-concept"], 3) == []
        # Concept hit → only the note that actually links an insight.
        hits = prime.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-hasl"]
    assert hits[0]["insights"] == [{"id": "n-ins1", "body": "reuse me"}]


# ---------------------------------------------------------------------------
# Prime v2 (issue #85) — serve insight-note bodies by following builds_on links
# from concept-matched trajectories; prefer merged-clean-labeled trajectories
# over reworked when labels exist.


def _add_note(path, *, note_id, title, body, concepts=(), tags=(),
              date="2026-07-18", frontmatter=None, note_type="note"):
    """Insert one more note (+ its tags/concepts) into an existing seeded db —
    the tables prime's queries join. Insight notes carry no loop-run tag.
    ``note_type`` lets a test seed a non-note (decision/session) to prove prime
    only serves ``type='note'`` bodies as color."""
    import json as _json
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(path)
    conn.execute(
        "INSERT INTO notes (id, type, title, path, date, frontmatter, body_text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (note_id, note_type, title, f"{note_id}.md", date,
         _json.dumps(frontmatter or {}), body),
    )
    for t in tags:
        conn.execute("INSERT INTO note_tags VALUES (?, ?)", (note_id, t))
    for c in concepts:
        conn.execute("INSERT INTO note_concepts VALUES (?, ?)", (note_id, c))
    conn.commit()
    conn.close()


def test_query_trajectories_follows_builds_on_to_insight_bodies(tmp_path):
    """A trajectory carrying a builds_on link resolves the linked insight note's
    BODY — the portable lesson's only home since #85."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-traj", title="loop trajectory #85",
        concepts=["retrieval"],
        body="## What\nprime v2.\n\n## How it went\none fix round.\n",
        frontmatter={"issue": 85, "outcome": "shipped",
                     "builds_on": ["n-ins1"]},
    )
    _add_note(db, note_id="n-ins1", title="portable lesson",
              body="Portable lessons live in linked insight notes.")
    conn = index_client.open_ro(str(db))
    try:
        hits = prime.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-traj"]
    assert hits[0]["insights"] == [
        {"id": "n-ins1", "body": "Portable lessons live in linked insight notes."},
    ]


def test_query_trajectories_prefers_merged_clean_over_reworked(tmp_path):
    """When labels exist, merged-clean sorts before reworked regardless of
    recency; the sort is a deterministic stable tweak."""
    db = tmp_path / "index.db"
    # The reworked note is NEWER (would win on recency); merged-clean is older.
    _seed_index_db(
        db, note_id="n-rew", title="reworked", concepts=["retrieval"],
        body="## What\nx\n\n## How it went\ny\n", date="2026-07-18",
        frontmatter={"issue": 1, "outcome": "shipped", "outcome_label": "reworked",
                     "builds_on": ["n-ins-rew"]},
    )
    _add_note(db, note_id="n-ins-rew", title="lesson", body="rework lesson")
    _add_note(
        db, note_id="n-clean", title="clean", concepts=["retrieval"],
        tags=["loop-run"], date="2026-07-10",
        body="## What\nx\n\n## How it went\ny\n",
        frontmatter={"issue": 2, "outcome": "shipped", "outcome_label": "merged-clean",
                     "builds_on": ["n-ins-clean"]},
    )
    _add_note(db, note_id="n-ins-clean", title="lesson", body="clean lesson")
    conn = index_client.open_ro(str(db))
    try:
        hits = prime.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-clean", "n-rew"]


def test_query_trajectories_unlabeled_keeps_recency(tmp_path):
    """No outcome_label anywhere → pure recency order (byte-stable v1 behavior);
    the weighting only fires when labels exist."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-older", title="older", concepts=["retrieval"],
        body="## What\nx\n\n## How it went\ny\n", date="2026-07-10",
        frontmatter={"issue": 1, "outcome": "shipped", "builds_on": ["n-ins-old"]},
    )
    _add_note(db, note_id="n-ins-old", title="lesson", body="old")
    _add_note(
        db, note_id="n-newer", title="newer", concepts=["retrieval"],
        tags=["loop-run"], date="2026-07-18",
        body="## What\nx\n\n## How it went\ny\n",
        frontmatter={"issue": 2, "outcome": "shipped", "builds_on": ["n-ins-new"]},
    )
    _add_note(db, note_id="n-ins-new", title="lesson", body="new")
    conn = index_client.open_ro(str(db))
    try:
        hits = prime.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-newer", "n-older"]


def test_render_prime_block_v2_rendering_is_byte_stable():
    """AC2 pin: the served block for a v2 trajectory is byte-identical to the
    pre-#98 rendering. The expected string is hand-built from the documented
    format (heading, then ``### #<issue> — <title> (<outcome>)`` + the insight
    bodies, then the decisions section: one bullet per decision with its id,
    title and summary line — funloops#49 replaced the bare id line; an id the
    index does not hold says so), never recomputed by the renderer."""
    block, served = prime.render_prime_block(
        [{"id": "n-traj", "title": "prime rail", "issue": 57, "outcome": "shipped",
          "insights": [{"id": "n-ins1", "body": "Portable lesson one."},
                       {"id": "n-ins2", "body": "Portable lesson two."}]}],
        decisions=[{"id": "dec-ccc333", "title": "Widen before you split",
                    "summary": "The CHECK is the seam."},
                   {"id": "dec-ddd444", "title": "", "summary": ""}],
    )
    assert block == (
        "## Prior trajectories — reusable lessons from similar prior runs\n"
        "\n"
        "### #57 — prime rail (shipped)\n"
        "Portable lesson one.\n"
        "Portable lesson two.\n"
        "\n"
        "### Prior decisions\n"
        "- **dec-ccc333** — Widen before you split\n"
        "  The CHECK is the seam.\n"
        "- **dec-ddd444** — (not in the index)\n"
    )
    assert served == ["n-ins1", "n-ins2", "dec-ccc333", "dec-ddd444"]


def test_build_prime_payload_renders_decision_title_and_summary(tmp_path):
    """funloops#49 (dec-f5bdf9ea): the decisions leg resolves each id against
    the index and renders the decision's title and the first prose line of its
    body (the ``## Context`` rationale weave_extract writes first); an id the
    index does not hold still lands, marked, so a dead pointer announces
    itself. ``served`` records every id that arrived."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-seed", title="seed", concepts=["x"], body="y")
    _add_note(db, note_id="dec-1", title="Serve decisions with titles",
              body="# Serve decisions with titles\n\n## Context\n\n"
                   "Bare ids are dead pointers.\nSecond line.\n\n"
                   "## Decision\n\nRender the title.",
              note_type="decision")
    conn = index_client.open_ro(str(db))
    try:
        payload = prime.build_prime_payload(
            49, "loop-run-0", ["unrelated"], conn=conn,
            decisions=["dec-1", "dec-missing"],
        )
    finally:
        conn.close()
    assert payload["primed"] is True
    assert payload["served"] == ["dec-1", "dec-missing"]
    assert "- **dec-1** — Serve decisions with titles\n  Bare ids are dead pointers.\n" in payload["block"]
    assert "Second line." not in payload["block"]
    assert "- **dec-missing** — (not in the index)" in payload["block"]


def test_build_prime_payload_serves_insight_bodies_end_to_end(tmp_path):
    """Acceptance: a concept-matched trajectory with a builds_on insight serves
    the insight body in the block and records the insight id as served."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-traj", title="loop trajectory",
        concepts=["self-improvement"],
        body="## What\nx\n\n## How it went\ny\n",  # no Lessons — v2
        frontmatter={"issue": 85, "outcome": "shipped", "builds_on": ["n-ins1"]},
    )
    _add_note(db, note_id="n-ins1", title="portable lesson",
              body="Serve insight bodies via builds_on links.")
    conn = index_client.open_ro(str(db))
    try:
        payload = prime.build_prime_payload(
            85, "loop-run-0", ["self-improvement"], conn=conn,
        )
    finally:
        conn.close()
    assert payload["primed"] is True
    assert payload["served"] == ["n-ins1"]
    assert "Serve insight bodies via builds_on links." in payload["block"]


# ---------------------------------------------------------------------------
# Prime v3 (issue #100) — FTS+concept fusion through the index_client seam.
# The write side speaks ontology, the orchestrator held GitHub labels, so the
# concept-only join was dead by construction; an FTS leg over the issue's own
# text is the second retriever, fused by RRF (k=60, the retrieval doctrine's
# constant).


def _build_fts(path):
    """Mirror the real index's ``notes_fts`` (src/thinkweave/core/indexer.py:215
    — fts5, external content over ``notes``) onto a hand-seeded db, then fill it
    from the rows already inserted. The FTS leg has nothing to MATCH without it."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(path)
    conn.executescript(
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
        " id UNINDEXED, title, body_text, tags, content='notes',"
        " content_rowid='rowid', tokenize=\"unicode61 remove_diacritics 2"
        " tokenchars '-_'\");"
    )
    conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


def _fusion_db(tmp_path):
    """Two concept-matched trajectories (A newer than B, so concept-only order is
    [A, B]) plus C, which carries neither the concept nor a linked insight of its
    own — only the FTS query reaches it."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-a", title="alpha rail", concepts=["retrieval"],
                   body="## What\nunrelated wording.\n", date="2026-07-18",
                   frontmatter={"issue": 1, "outcome": "shipped",
                                "builds_on": ["n-ins-a"]})
    _add_note(db, note_id="n-ins-a", title="lesson", body="lesson A")
    _add_note(db, note_id="n-b", title="beta rail", concepts=["retrieval"],
              tags=["loop-run"], date="2026-07-10",
              body="## What\nfuse the retrieval legs.\n",
              frontmatter={"issue": 2, "outcome": "shipped",
                           "builds_on": ["n-ins-b"]})
    _add_note(db, note_id="n-ins-b", title="lesson", body="lesson B")
    _add_note(db, note_id="n-c", title="gamma rail", tags=["loop-run"],
              date="2026-07-09", body="## What\nfuse the retrieval legs too.\n",
              frontmatter={"issue": 3, "outcome": "shipped",
                           "builds_on": ["n-ins-c"]})
    _add_note(db, note_id="n-ins-c", title="lesson", body="lesson C")
    _build_fts(db)
    return db


def test_query_trajectories_rrf_fusion_reorders_the_concept_leg(tmp_path):
    """A note both legs return outranks a note only the concept leg returns,
    even when the latter tops the concept leg on recency.

    Hand-computed from RRF (k=60, 1-indexed ranks): A is concept rank 1 and
    absent from the FTS leg → 1/61 = 0.016393. B is concept rank 2 and present
    in the FTS leg at some rank r → 1/62 + 1/(60+r) ≥ 0.016129 + 0.000264 for
    any r a 3-row index can produce, so B > A. Concept-only order is [A, B]."""
    db = _fusion_db(tmp_path)
    conn = index_client.open_ro(str(db))
    try:
        assert [h["id"] for h in prime.query_trajectories(conn, ["retrieval"], 3)] \
            == ["n-a", "n-b"]
        fused = prime.query_trajectories(conn, ["retrieval"], 3,
                                         query="fuse the retrieval legs")
        assert [h["id"] for h in fused] == ["n-b", "n-a", "n-c"]
    finally:
        conn.close()


def test_query_trajectories_degrades_each_leg_independently(tmp_path):
    """Empty query → concept-only. Empty concepts + query → FTS-only (the
    GH-label-only path the orchestrator hits). Both empty → nothing at all."""
    db = _fusion_db(tmp_path)
    conn = index_client.open_ro(str(db))
    try:
        assert [h["id"] for h in prime.query_trajectories(conn, ["retrieval"], 3,
                                                          query="")] == ["n-a", "n-b"]
        fts_only = prime.query_trajectories(conn, [], 3,
                                            query="fuse the retrieval legs")
        assert {h["id"] for h in fts_only} == {"n-b", "n-c"}  # n-a has no match
        assert prime.query_trajectories(conn, [], 3, query="") == []
    finally:
        conn.close()


def test_query_trajectories_fts_leg_never_crashes_the_concept_leg(tmp_path):
    """The query is user-shaped (an issue title/body). fts5 operator soup and a
    query with no indexable terms must degrade to concept-only, and so must an
    index whose notes_fts is missing entirely (older/partial vault)."""
    db = _fusion_db(tmp_path)
    conn = index_client.open_ro(str(db))
    try:
        for hostile in ('"" AND (NEAR', "*", "^ ~ !", "   "):
            assert [h["id"] for h in prime.query_trajectories(
                conn, ["retrieval"], 3, query=hostile)] == ["n-a", "n-b"]
    finally:
        conn.close()
    no_fts = tmp_path / "no-fts.db"
    _seed_index_db(no_fts, note_id="n-a", title="alpha", concepts=["retrieval"],
                   body="## What\nfuse the retrieval legs.\n",
                   frontmatter={"issue": 1, "outcome": "shipped",
                                "builds_on": ["n-ins-a"]})
    _add_note(no_fts, note_id="n-ins-a", title="lesson", body="lesson A")
    conn = index_client.open_ro(str(no_fts))
    try:
        hits = prime.query_trajectories(conn, ["retrieval"], 3,
                                        query="fuse the retrieval legs")
        assert [h["id"] for h in hits] == ["n-a"]
    finally:
        conn.close()


def test_query_trajectories_outcome_rank_wins_over_the_fused_order(tmp_path):
    """AC3: fusion decides candidate order, the outcome weighting still decides
    final order — a merged-clean trajectory sorts above a reworked one that the
    fused ranking put first."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-rew", title="reworked", concepts=["retrieval"],
                   body="## What\nfuse the retrieval legs.\n", date="2026-07-18",
                   frontmatter={"issue": 1, "outcome": "shipped",
                                "outcome_label": "reworked",
                                "builds_on": ["n-ins-rew"]})
    _add_note(db, note_id="n-ins-rew", title="lesson", body="rework lesson")
    _add_note(db, note_id="n-clean", title="clean", concepts=["retrieval"],
              tags=["loop-run"], date="2026-07-10", body="## What\nplain.\n",
              frontmatter={"issue": 2, "outcome": "shipped",
                           "outcome_label": "merged-clean",
                           "builds_on": ["n-ins-clean"]})
    _add_note(db, note_id="n-ins-clean", title="lesson", body="clean lesson")
    _build_fts(db)
    conn = index_client.open_ro(str(db))
    try:
        hits = prime.query_trajectories(conn, ["retrieval"], 3,
                                        query="fuse the retrieval legs")
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-clean", "n-rew"]


def test_prime_dry_run_prints_the_block_and_writes_no_buffer(tmp_path, capsys):
    """AC6: --dry-run is the mechanic's proof mode — the full payload (block
    included) prints, and the buffer write is suppressed regardless of
    --buffer, so a dry run against a live index leaves zero side effects."""
    db = _fusion_db(tmp_path)
    buf = tmp_path / "buffer" / "ses-loop123.jsonl"
    rc = cli.main([
        "prime", "57", "--run-id", "loop-run-0", "--concepts", "retrieval",
        "--query", "fuse the retrieval legs", "--db", str(db),
        "--buffer", str(buf), "--dry-run",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primed"] is True
    assert payload["query"] == "fuse the retrieval legs"
    assert "lesson B" in payload["block"]
    assert not buf.exists()  # suppressed despite --buffer


def test_prime_flags_the_pre_v3_labels_only_invocation(tmp_path, capsys):
    """The pre-#100 call shape (`--labels`, no `--query`) reaches exactly the
    dead concept-only join #100 fixed — GitHub labels are never written as
    concepts, so it matches nothing. It must not report as a benign empty
    match: the note names the calling convention, so an inert rail on the live
    loop is visible rather than plausible. The v3 shape is not flagged."""
    absent = str(tmp_path / "absent.db")
    assert cli.main(["prime", "100", "--run-id", "loop-run-0", "--db", absent,
                     "--labels", "enhancement,ready-for-agent"]) == 0
    flagged = json.loads(capsys.readouterr().out)
    assert flagged["primed"] is False
    assert "--query" in flagged["note"] and "ontology" in flagged["note"]
    # The degradation reason survives alongside the convention warning.
    assert "no matching prior trajectories" in flagged["note"]

    assert cli.main(["prime", "100", "--run-id", "loop-run-0", "--db", absent,
                     "--concepts", "agent-harness"]) == 0
    assert "--query" not in json.loads(capsys.readouterr().out)["note"]
    # A labels call that DOES pass the text leg is the fixed shape, not the
    # dead one — the FTS leg carries retrieval, so no warning.
    assert cli.main(["prime", "100", "--run-id", "loop-run-0", "--db", absent,
                     "--labels", "enhancement", "--query", "some issue text"]) == 0
    assert "--query" not in json.loads(capsys.readouterr().out)["note"]


def test_prime_erroring_fts_does_not_read_as_a_clean_empty_match(tmp_path):
    """FTS is load-bearing now, so a *broken* notes_fts must not hide behind
    "no matching prior trajectories". An absent table stays best-effort (an old
    index still primes on concepts), but a table that errors with nothing else
    retrieved degrades with the index-error note. Never a crash either way."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-x", title="x", concepts=["retrieval"],
                   body="## What\nfuse the retrieval legs.\n",
                   frontmatter={"issue": 1, "outcome": "shipped",
                                "builds_on": ["n-ins-x"]})
    _add_note(db, note_id="n-ins-x", title="lesson", body="lesson X")
    # A plain table where fts5 is expected: MATCH raises OperationalError.
    wconn = sqlite3.connect(str(db))
    wconn.execute("CREATE TABLE notes_fts (id TEXT, title TEXT, body_text TEXT)")
    wconn.commit()
    wconn.close()
    conn = index_client.open_ro(str(db))
    try:
        # Concept leg retrieved something → the broken FTS leg stays silent.
        served = prime.build_prime_payload(
            1, "loop-run-0", ["retrieval"], conn=conn,
            query="fuse the retrieval legs")
        # Nothing retrieved at all → the FTS failure reaches the note.
        empty = prime.build_prime_payload(
            1, "loop-run-0", ["no-such-concept"], conn=conn,
            query="fuse the retrieval legs")
    finally:
        conn.close()
    assert served["primed"] is True and served["served"] == ["n-ins-x"]
    assert empty["primed"] is False
    assert "unread" in empty["note"].lower()


def test_prime_serves_file_anchored_decisions_without_any_trajectory(tmp_path, capsys):
    """AC7: the orchestrator-resolved --decisions ids (the file-anchored rung of
    the granularity ladder) still land in `served` and in the block, and they
    prime a run on their own — the retrieval legs finding nothing does not
    discard them."""
    rc = cli.main([
        "prime", "100", "--run-id", "loop-run-0", "--concepts", "retrieval",
        "--query", "some issue text", "--decisions", "dec-1,dec-2",
        "--db", str(tmp_path / "absent.db"),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primed"] is True
    assert payload["served"] == ["dec-1", "dec-2"]
    # No index to resolve against: each id still lands, marked as unresolved.
    assert "- **dec-1** — (not in the index)\n- **dec-2** — (not in the index)\n" in payload["block"]


# --- Review round 1 (issue #85) — hardening the prime v2 seams --------------


def test_resolve_insights_only_serves_note_type(tmp_path):
    """builds_on could name a decision or session id; prime must NOT serve a
    non-note body as color — only `type='note'` insight notes are served, and a
    decision id resolves to nothing."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-seed", title="seed", concepts=["x"],
                   body="y")
    _add_note(db, note_id="dec-1", title="a decision", body="decision body",
              note_type="decision")
    _add_note(db, note_id="ses-1", title="a session", body="session body",
              note_type="session")
    _add_note(db, note_id="n-ins", title="insight", body="insight body")
    conn = index_client.open_ro(str(db))
    try:
        # A decision/session id in builds_on resolves to nothing; only the note
        # is served, preserving builds_on order.
        assert prime.resolve_insights(conn, ["dec-1", "n-ins", "ses-1"]) == [
            {"id": "n-ins", "body": "insight body"},
        ]
    finally:
        conn.close()


def test_coerce_builds_on_forms():
    """Regression pin for the builds_on coercion: plain ids pass through,
    path-based wikilinks (`[[path|id]]`) strip to the trailing id, whitespace is
    trimmed, non-string elements are dropped, and a non-list is empty."""
    f = prime._coerce_builds_on
    assert f(["n-plain"]) == ["n-plain"]
    assert f(["[[projects/x/note.md|n-wiki]]"]) == ["n-wiki"]
    assert f(["  n-space  "]) == ["n-space"]
    assert f(["n-a", 123, None, {"x": 1}, ""]) == ["n-a"]
    assert f("n-notalist") == []
    assert f(None) == []


def test_query_trajectories_dangling_builds_on_filtered(tmp_path):
    """A builds_on link that resolves to nothing (missing / non-note id) leaves
    the trajectory with no reusable color → filtered out, never a crash."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-traj", title="t", concepts=["retrieval"],
        body="## What\nx\n\n## How it went\ny\n",
        frontmatter={"issue": 1, "outcome": "shipped", "builds_on": ["n-missing"]},
    )
    conn = index_client.open_ro(str(db))
    try:
        hits = prime.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert hits == []


def test_prime_writes_loop_prime_served_event_to_buffer(tmp_path):
    """When --buffer is given and the run is primed, the rail appends one
    loop_prime retrieval event (the context_served source seed) with the served
    ids — mirroring the prompt-time serving surface."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-prior1", title="t", concepts=["retrieval"],
                   body="## What\nx\n\n## How it went\ny\n",
                   frontmatter={"issue": 57, "outcome": "shipped",
                                "builds_on": ["n-ins1"]})
    _add_note(db, note_id="n-ins1", title="portable lesson", body="reuse")
    buf = tmp_path / "buffer" / "ses-loop123.jsonl"
    rc = cli.main([
        "prime", "57", "--run-id", "loop-run-0",
        "--concepts", "retrieval", "--db", str(db),
        "--buffer", str(buf), "--session-id", "ses-loop123",
    ])
    assert rc == 0
    lines = [l for l in buf.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["type"] == "retrieval" and ev["tool"] == "loop_prime"
    assert ev["returned_ids"] == ["n-ins1"]  # the insight served, not the trajectory
    assert ev["args"]["run_id"] == "loop-run-0" and ev["args"]["issue"] == 57


def test_prime_unprimed_run_writes_no_buffer_event(tmp_path):
    """An unprimed run (nothing found — here, no index at all) has no served
    ids, so no buffer event even if --buffer is supplied."""
    buf = tmp_path / "buffer" / "ses-loop123.jsonl"
    rc = cli.main([
        "prime", "57", "--run-id", "loop-run-10",
        "--concepts", "retrieval", "--buffer", str(buf),
        "--db", str(tmp_path / "absent.db"),
    ])
    assert rc == 0
    assert not buf.exists()


def test_prime_degrades_on_corrupt_index(tmp_path, capsys):
    """A foreign/corrupt file at the resolved --db path must NOT crash the loop
    (sqlite3.connect is lazy — the DatabaseError surfaces on the first query,
    past main's connect guard). Regression: rc 0 + unprimed payload."""
    db = tmp_path / "index.db"
    db.write_bytes(b"GIF89a this is definitely not a sqlite database\n" * 8)
    rc = cli.main([
        "prime", "57", "--run-id", "loop-run-0",
        "--concepts", "retrieval", "--db", str(db),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primed"] is False
    assert payload["served"] == [] and payload["block"] == ""
    assert "unread" in payload["note"].lower()


def test_prime_degrades_on_schema_drift_index(tmp_path, capsys):
    """A valid but older index missing the tables prime joins (note_tags /
    note_concepts) raises OperationalError on the query — must also degrade to
    an unprimed payload, rc 0."""
    db = tmp_path / "index.db"
    wconn = sqlite3.connect(str(db))
    wconn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT)")  # no join tables
    wconn.commit()
    wconn.close()
    rc = cli.main([
        "prime", "57", "--run-id", "loop-run-0",
        "--concepts", "retrieval", "--db", str(db),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primed"] is False and payload["served"] == []
    assert "unread" in payload["note"].lower()


def test_build_prime_payload_index_error_notes_degradation(tmp_path):
    """At the seam: a query that raises sqlite3.Error degrades to primed:false
    with a note distinct from the no-match note."""
    db = tmp_path / "index.db"
    wconn = sqlite3.connect(str(db))
    wconn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT)")
    wconn.commit()
    wconn.close()
    conn = index_client.open_ro(str(db))
    try:
        payload = prime.build_prime_payload(
            57, "loop-run-0", ["retrieval"], conn=conn,
        )
    finally:
        conn.close()
    assert payload["primed"] is False and payload["served"] == []
    assert "unread" in payload["note"].lower()


def test_build_trajectory_rejects_non_list_or_non_string_served():
    """--served-json shape guard: a dict (e.g. the whole prime payload pasted by
    mistake) or a bare string must not silently become frontmatter — it would
    corrupt the served-context regression's raw material."""
    issue = {"number": 1, "title": "x", "labels": []}
    for bad in ({"issue": 57, "served": ["n-a"]}, "n-abc", 42):
        with pytest.raises((ValueError, TypeError)):
            mint.build_trajectory(
                issue, branch="b", commits=[], numstat="", gates=[],
                fix_rounds=0, outcome="shipped", primed=True, served=bad,
            )
    # A list with a non-string element is rejected too.
    with pytest.raises((ValueError, TypeError)):
        mint.build_trajectory(
            issue, branch="b", commits=[], numstat="", gates=[],
            fix_rounds=0, outcome="shipped", primed=True, served=["n-ok", 123],
        )


# ---------------------------------------------------------------------------
# Semantic execution trace (issue #85) — the gate agents' own reports,
# condensed by the orchestrator into structured envelopes on the trajectory
# note frontmatter. No new model calls: build_trajectory only accepts + shapes.
# Every expected value below is hand-written from the #85 schema, not
# recomputed by the code under test.


def _sample_trace() -> dict:
    """A hand-written semantic trace with prose-valued fields, carrying one
    extra orchestrator-bookkeeping key per level to prove projection drops it."""
    return {
        "reviews": [
            {"gate": "review", "finding": "standalone existence test duplicates "
             "the eight sibling guards", "severity": "note",
             "disposition": "accepted", "fixed_by": "dropped the redundant test",
             "reviewer_note": "orchestrator bookkeeping — dropped"},
        ],
        "criteria": [
            {"id": "AC1", "verdict": "met", "flipped_by_round": 1, "extra": "x"},
            {"id": "AC2", "verdict": "met", "flipped_by_round": None},
        ],
        "simplify": {
            "outcome": "applied", "lines_delta": -12,
            "cuts": [{"what": "existence test", "why": "eight siblings already "
                      "guard existence", "note": "dropped"}],
            "kept": [{"what": "budget-cap test", "why": "the only novel invariant"}],
            "bookkeeping": "dropped",
        },
        "edge_cases": ["empty concepts → no prime", "corrupt index → unprimed"],
        "tdd": {"red_confirmed": True, "note": "dropped"},
        "orchestrator_scratch": {"anything": "dropped"},  # unknown top-level key
    }


def test_build_trajectory_round_trips_semantic_trace():
    """--trace-json carries the gate agents' condensed reports into
    frontmatter['trace'] as prose-valued structured envelopes; prose survives
    verbatim, counts stay ints, flipped_by_round is int-or-null."""
    issue = {"number": 85, "title": "Trajectory v2", "labels": []}
    payload = mint.build_trajectory(
        issue, branch="loop/dag-54", commits=["a"], numstat="1\t0\tx.py\n",
        gates=[], fix_rounds=1, outcome="shipped", trace=_sample_trace(),
    )
    trace = payload["frontmatter"]["trace"]
    assert trace["reviews"] == [
        {"gate": "review",
         "finding": "standalone existence test duplicates the eight sibling guards",
         "severity": "note", "disposition": "accepted",
         "fixed_by": "dropped the redundant test"},
    ]
    assert trace["criteria"] == [
        {"id": "AC1", "verdict": "met", "flipped_by_round": 1},
        {"id": "AC2", "verdict": "met", "flipped_by_round": None},
    ]
    assert "simplify" not in trace  # the deleted stage's envelope is dropped
    assert trace["edge_cases"] == ["empty concepts → no prime",
                                   "corrupt index → unprimed"]
    assert trace["tdd"] == {"red_confirmed": True}
    # Unknown top-level key dropped (skills-style projection).
    assert "orchestrator_scratch" not in trace


def test_build_trajectory_omits_trace_when_absent():
    """Backward compat / byte-stability: a caller that passes no trace gets a
    payload with NO trace key — the pre-#85 frontmatter is unchanged."""
    payload = mint.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
    )
    assert "trace" not in payload["frontmatter"]


def test_build_trajectory_trace_only_includes_provided_top_level_keys():
    """A partial trace (only the fields a run actually had) yields only those
    envelopes — absent sections are omitted, not emitted empty."""
    payload = mint.build_trajectory(
        {"number": 2, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
        trace={"tdd": {"red_confirmed": False}},
    )
    assert payload["frontmatter"]["trace"] == {"tdd": {"red_confirmed": False}}


def test_build_trajectory_rejects_non_dict_trace():
    """Shape guard (mirrors the served list-guard, #57 posture): a non-dict
    trace — a list, a bare string, a number — must not silently land in
    frontmatter; the trace envelope is a JSON object."""
    issue = {"number": 3, "title": "x", "labels": []}
    for bad in ([{"gate": "review"}], "review: minor", 7):
        with pytest.raises((ValueError, TypeError)):
            mint.build_trajectory(
                issue, branch="b", commits=[], numstat="", gates=[],
                fix_rounds=0, outcome="shipped", trace=bad,
            )


def test_trajectory_trace_argparse_contract():
    """The trajectory subcommand exposes --trace-json (optional, default None),
    a sibling of --skills-json / --served-json."""
    ns = cli.build_arg_parser().parse_args([
        "trajectory", "85", "--gates-json", "g.json",
        "--trace-json", "trace.json", "--outcome", "shipped",
    ])
    assert ns.trace_json == "trace.json"
    ns2 = cli.build_arg_parser().parse_args([
        "trajectory", "85", "--gates-json", "g.json", "--outcome", "shipped",
    ])
    assert ns2.trace_json is None


def test_resolve_index_db_honors_weave_dir_override(tmp_path):
    """PR #10 deployment class: <vault>/config/config.toml sets weave_dir off
    the vault (derived SQLite on native fs). --vault must resolve the index
    under weave_dir, not the stale <vault>/.weave/index.db."""
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    weave = tmp_path / "native" / "weave"
    weave.mkdir(parents=True)
    # Forward slashes: a raw Windows path in a TOML *basic* string would make
    # `C:\Users\…` an invalid \U escape and the config would fail to parse,
    # silently exercising the malformed-config fallback instead of the override.
    (vault / "config" / "config.toml").write_text(
        f'weave_dir = "{weave.as_posix()}"\n', encoding="utf-8")
    assert index_client.resolve_db_path(None, str(vault)) == str(weave / "index.db")


def test_resolve_index_db_relative_weave_dir_anchors_at_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        'weave_dir = "derived/weave"\n', encoding="utf-8")
    assert index_client.resolve_db_path(None, str(vault)) == str(
        vault / "derived" / "weave" / "index.db")


def test_resolve_index_db_falls_back_to_legacy_weave_layout(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    # No config.toml at all → legacy <vault>/.weave/index.db.
    assert index_client.resolve_db_path(None, str(vault)) == str(
        vault / ".weave" / "index.db")


def test_resolve_index_db_malformed_config_falls_back(tmp_path):
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        "this is = not valid toml = at all\n", encoding="utf-8")
    # Malformed config must not crash — degrade to the legacy layout.
    assert index_client.resolve_db_path(None, str(vault)) == str(
        vault / ".weave" / "index.db")


def test_resolve_index_db_explicit_db_wins_over_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        f'weave_dir = "{tmp_path / "elsewhere"}"\n', encoding="utf-8")
    assert index_client.resolve_db_path("/explicit/index.db", str(vault)) == "/explicit/index.db"


def test_prime_argparse_contract():
    ns = cli.build_arg_parser().parse_args([
        "prime", "57", "--run-id", "loop-x", "--labels", "a,b",
        "--concepts", "c1,c2", "--db", "i.db", "--limit", "2",
        "--budget-chars", "800", "--decisions", "dec-1", "--buffer", "b.jsonl",
    ])
    assert ns.cmd == "prime" and ns.number == 57 and ns.run_id == "loop-x"
    assert ns.labels == "a,b" and ns.concepts == "c1,c2"
    assert ns.limit == 2 and ns.budget_chars == 800
    # Sensible defaults when omitted.
    ns2 = cli.build_arg_parser().parse_args(["prime", "1", "--run-id", "r"])
    assert ns2.labels is None and ns2.concepts is None and ns2.db is None
    assert ns2.limit == 3 and ns2.budget_chars == 1200 and ns2.buffer is None


# ---------------------------------------------------------------------------
# Risk-lane PR triage — classify_pr over synthetic PR-signal sets
#
# Pure function: (signals, triage-cfg) -> {lane, label, reasons}. Precedence
# red > yellow > green, every triggered rule listed (short-circuit reasons).

# Two lanes only (dec-cf8f0d33 deleted the green lane): red routes to a human,
# everything else is yellow (review-light) with its reasons listed.
TRIAGE_CFG = {
    "sensitive_paths": [
        "hooks/", "src/thinkweave/surfaces/", "ontology.yaml",
        "sources.yaml", "*schema*",
    ],
    "watched_paths": ["docs/agents/"],
    "red_diff_lines": 800,
}


def _signals(**kw):
    """A first-try, small, test-covered, note-finding, green-baseline PR —
    the clean archetype. Override one field per test to trip one rule."""
    base = {
        "fix_rounds": 0,
        "diff_lines": 20,
        "files_touched": ["src/thinkweave/core/foo.py", "tests/test_foo.py"],
        "tests_touched": True,
        "review_severity": "note",
        "baseline_green": True,
    }
    base.update(kw)
    return base


# --- no green lane ----------------------------------------------------------


def test_clean_archetype_is_review_light_with_no_reasons():
    """There is no auto-merge lane: the cleanest PR is still a human skim.
    An empty reasons list is what tells the skimmer nothing tripped."""
    r = triage.classify_pr(_signals(), TRIAGE_CFG)
    assert r["lane"] == "yellow" and r["label"] == "review-light"
    assert r["reasons"] == []
    assert set(triage.TRIAGE_LABELS) == {"yellow"}


def test_note_and_none_findings_stay_yellow():
    assert triage.classify_pr(_signals(review_severity="none"), TRIAGE_CFG)["lane"] == "yellow"


# --- red lane ---------------------------------------------------------------


def test_classify_hooks_path_always_red():
    # Acceptance criterion 1: hooks/ is red regardless of size / first-try /
    # coverage / review — the whole green archetype except the touched file.
    r = triage.classify_pr(_signals(files_touched=["hooks/hooks.json"]), TRIAGE_CFG)
    assert r["lane"] == "red" and r["label"] == "ready-for-human"
    assert any("hooks/hooks.json" in x for x in r["reasons"])


def test_classify_dir_prefix_matches_mcp_surface():
    r = triage.classify_pr(
        _signals(files_touched=["src/thinkweave/surfaces/mcp/server.py"]), TRIAGE_CFG)
    assert r["lane"] == "red"


def test_classify_glob_pattern_matches_schema_basename():
    r = triage.classify_pr(
        _signals(files_touched=["src/thinkweave/core/schemas.py"]), TRIAGE_CFG)
    assert r["lane"] == "red"


def test_classify_bare_filename_matches_basename_only():
    # ontology.yaml at any depth is sensitive; a file that merely shares the
    # stem as a prefix (different basename) is not.
    hit = triage.classify_pr(
        _signals(files_touched=["src/thinkweave/vault_templates/config/ontology.yaml"]),
        TRIAGE_CFG)
    assert hit["lane"] == "red"
    miss = triage.classify_pr(
        _signals(files_touched=["src/thinkweave/core/ontology_helpers.py"]), TRIAGE_CFG)
    assert miss["lane"] != "red"


def test_classify_big_diff_red():
    r = triage.classify_pr(_signals(diff_lines=900), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("900" in x for x in r["reasons"])


def test_classify_degraded_baseline_red():
    assert triage.classify_pr(_signals(baseline_green=False), TRIAGE_CFG)["lane"] == "red"


def test_classify_problem_review_red():
    """dec-39140113: two severities. `problem` is the red lane."""
    r = triage.classify_pr(_signals(review_severity="problem"), TRIAGE_CFG)
    assert r["lane"] == "red" and any("problem" in x for x in r["reasons"])


def test_acceptance_is_not_a_triage_signal():
    """The judge's verdict already blocks; triage neither requires nor reads it."""
    assert "acceptance" not in _signals()
    assert triage.classify_pr(_signals(), TRIAGE_CFG)["lane"] == "yellow"
    assert triage.classify_pr(_signals(acceptance="uncertain"), TRIAGE_CFG)["reasons"] == []


def test_red_lists_every_triggered_rule():
    # Short-circuit reasons: not just the first — every red rule that fired.
    r = triage.classify_pr(
        _signals(files_touched=["hooks/x.json"], diff_lines=900,
                 baseline_green=False, review_severity="problem"),
        TRIAGE_CFG)
    assert r["lane"] == "red"
    joined = " | ".join(r["reasons"])
    assert "hooks/x.json" in joined and "900" in joined
    assert "baseline" in joined.lower() and "problem" in joined
    assert len(r["reasons"]) >= 4


# --- yellow lane ------------------------------------------------------------


def test_classify_fix_rounds_yellow():
    r = triage.classify_pr(_signals(fix_rounds=1), TRIAGE_CFG)
    assert r["lane"] == "yellow" and r["label"] == "review-light"
    assert any("fix round" in x for x in r["reasons"])


def test_classify_watched_path_yellow():
    r = triage.classify_pr(_signals(files_touched=["docs/agents/loop.toml"]), TRIAGE_CFG)
    assert r["lane"] == "yellow"
    assert any("watched" in x for x in r["reasons"])


def test_classify_no_test_coverage_yellow():
    r = triage.classify_pr(_signals(tests_touched=False), TRIAGE_CFG)
    assert r["lane"] == "yellow"


# --- thresholds are config, not hardcoded (acceptance criterion 3) ----------


def test_thresholds_read_from_config():
    sig = _signals(diff_lines=200)
    lenient = {**TRIAGE_CFG, "red_diff_lines": 800}
    strict = {**TRIAGE_CFG, "red_diff_lines": 100}
    assert triage.classify_pr(sig, lenient)["lane"] == "yellow"
    assert triage.classify_pr(sig, strict)["lane"] == "red"


# --- config plumbing --------------------------------------------------------


def test_load_config_triage_defaults(tmp_path):
    cfg = cli.load_config(tmp_path / "nope.toml")
    t = cfg["triage"]
    # No path is sensitive until a host says so: the packaged rail cannot know
    # another repo's layout, and an inherited guess classifies the wrong files.
    assert t["sensitive_paths"] == []
    assert isinstance(t["red_diff_lines"], int)


def test_repo_loop_toml_has_triage_section():
    cfg = cli.load_config()
    # Sensitive paths translated to THIS repo's layout: the CLI surface and the
    # gate pipeline, as bare basenames so they hold for every workspace member.
    sp = cfg["triage"]["sensitive_paths"]
    assert "cli.py" in sp and "loop.toml" in sp


def test_triage_override_via_set(tmp_path):
    cfg = cli.apply_overrides(
        cli.load_config(tmp_path / "nope.toml"),
        ["triage.red_diff_lines=200"],
    )
    assert cfg["triage"]["red_diff_lines"] == 200


def test_triage_override_rejects_unknown_key(tmp_path):
    cfg = cli.load_config(tmp_path / "nope.toml")
    with pytest.raises(ValueError, match="unknown key"):
        cli.apply_overrides(cfg, ["triage.red_min_diff=200"])


# --- CLI contract -----------------------------------------------------------


def _shipped(tmp_path, files):
    """A git repo whose ``base...HEAD`` touches ``files``, as the triage cwd."""
    root = tmp_path / "shipped"
    root.mkdir()
    def git(*args):
        subprocess.run(["git", "-c", "user.name=fx", "-c", "user.email=fx@example.com",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=root, check=True, capture_output=True)
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("tag", "base")
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "slice")
    return root


def _triage(tmp_path, files, *extra, changed=20, findings=(), baseline="green", rounds=0):
    """Run ``devloop triage 59`` over a shipped slice: the gate results with
    a diff gate counting ``changed`` lines, a judge return carrying
    ``findings``, the baseline line and the fix rounds."""
    root = _shipped(tmp_path, files)
    gates_file, judge_file = tmp_path / "gates.json", tmp_path / "judge.json"
    gates_file.write_text(json.dumps([
        {"id": "diff-guard", "kind": "diff", "passed": True, "summary": "",
         "detail": "", "changed_lines": changed},
        {"id": "tests", "kind": "command", "passed": True, "summary": "", "detail": ""}]),
        encoding="utf-8")
    judge_file.write_text(json.dumps({
        "criteria": [{"id": "AC1", "verdict": "met", "evidence": "ran it"}],
        "findings": [{"severity": s, "finding": "f"} for s in findings]}), encoding="utf-8")
    return cli.main(["triage", "59", "--gates-json", str(gates_file),
                     "--judge-json", str(judge_file), "--baseline", baseline,
                     "--fix-rounds", str(rounds), "--cwd", str(root),
                     "--base-ref", "base", *extra])


def test_read_signals_returns_the_declared_record(tmp_path):
    """The signal record crosses into classification as ``triage.Signals``."""
    root = _shipped(tmp_path, ["src/foo.py", "tests/test_foo.py"])
    signals = triage.read_signals(
        [{"id": "diff-guard", "kind": "diff", "changed_lines": 12}],
        {"criteria": [], "findings": [{"severity": "note", "finding": "f"}]},
        "green", 1, root, "base")
    assert signals == triage.Signals(
        fix_rounds=1, diff_lines=12, files_touched=["src/foo.py", "tests/test_foo.py"],
        tests_touched=True, review_severity="note", baseline_green=True)
    assert signals.classify(TRIAGE_CFG, "ready-for-human") == {
        "lane": "yellow", "label": "review-light", "reasons": ["1 fix round(s)"]}


def test_triage_argparse_contract():
    """The rail gathers its own signals: no hand-assembled signal file."""
    ns = cli.build_arg_parser().parse_args(
        ["triage", "59", "--gates-json", "g.json", "--judge-json", "j.json",
         "--baseline", "red", "--fix-rounds", "2"])
    assert (ns.number, ns.gates_json, ns.judge_json, ns.baseline, ns.fix_rounds) == (
        59, "g.json", "j.json", "red", 2)
    with pytest.raises(SystemExit):
        cli.build_arg_parser().parse_args(["triage", "59", "--signals-json", "s.json"])
    with pytest.raises(SystemExit):  # the baseline line is green | red, nothing else
        cli.build_arg_parser().parse_args(
            ["triage", "--gates-json", "g", "--judge-json", "j", "--baseline", "true"])


def test_triage_cli_red_via_default_config(tmp_path, capsys):
    """A big diff (the diff gate's count) and a sensitive path (git diff) go red."""
    assert _triage(tmp_path, ["hooks/hooks.json"], changed=900) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lane"] == "red" and out["label"] == "ready-for-human"
    assert out["issue"] == 59
    assert any("900" in r for r in out["reasons"])


def test_triage_cli_clean_pr_is_review_light(tmp_path, capsys):
    """A test file in the diff is the coverage signal; a note stays yellow."""
    assert _triage(tmp_path, ["src/foo.py", "tests/test_foo.py"], findings=["note"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["label"] == "review-light" and out["reasons"] == []


@pytest.mark.parametrize("kw, needle", [
    ({"findings": ["note", "problem"]}, "review severity problem"),
    ({"baseline": "red"}, "degraded baseline"),
])
def test_triage_cli_reads_severity_and_baseline(tmp_path, capsys, kw, needle):
    assert _triage(tmp_path, ["tests/test_foo.py"], **kw) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lane"] == "red" and any(needle in r for r in out["reasons"])


def test_triage_cli_reads_rounds_and_missing_tests(tmp_path, capsys):
    assert _triage(tmp_path, ["src/foo.py"], rounds=2) == 0
    reasons = " | ".join(json.loads(capsys.readouterr().out)["reasons"])
    assert "2 fix round(s)" in reasons and "tests_touched=false" in reasons


# ---------------------------------------------------------------------------
# Fail-closed: LLM-assembled signals make enum drift / missing keys realistic.
# Unrecognized enum values and absent safety-critical signals must never
# classify green-eligible — they go RED, naming the offending value/key.


def _signals_no(*drop, **kw):
    """The green archetype with the named safety keys REMOVED (fail-closed
    coverage) plus any overrides."""
    s = _signals(**kw)
    for k in drop:
        s.pop(k, None)
    return s


def test_unrecognized_review_severity_is_red():
    r = triage.classify_pr(_signals(review_severity="high"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("high" in x for x in r["reasons"])


def test_missing_review_severity_is_red():
    r = triage.classify_pr(_signals_no("review_severity"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("review_severity" in x for x in r["reasons"])


def test_missing_baseline_green_is_red():
    r = triage.classify_pr(_signals_no("baseline_green"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("baseline_green" in x for x in r["reasons"])


def test_non_bool_baseline_green_is_red():
    # baseline_green: "false" (string) is truthy — it must NOT pass green.
    # Same enum-drift class: a non-bool value fails closed to red.
    r = triage.classify_pr(_signals(baseline_green="false"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("baseline_green" in x for x in r["reasons"])


def test_empty_signals_is_red_on_both_safety_keys():
    r = triage.classify_pr({}, TRIAGE_CFG)
    assert r["lane"] == "red"
    joined = " | ".join(r["reasons"])
    assert "baseline_green" in joined and "review_severity" in joined


def test_benign_absence_does_not_trip_red():
    # diff_lines / fix_rounds / files_touched absent is NOT a safety hole:
    # with the two safety keys present and clean, the PR is a clean skim.
    sig = {"tests_touched": True, "review_severity": "note", "baseline_green": True}
    r = triage.classify_pr(sig, TRIAGE_CFG)
    assert r["lane"] == "yellow" and r["reasons"] == []


def test_red_label_sourced_from_on_gate_failure(tmp_path, capsys):
    # classify_pr's red label is overridable (default 'ready-for-human'); the
    # CLI feeds it from labels.on_gate_failure so remapping that label moves the
    # triage-red label with it — no duplicate source of truth.
    assert triage.classify_pr(
        _signals(baseline_green=False), TRIAGE_CFG,
        red_label="needs-a-human")["label"] == "needs-a-human"
    # A path this repo's loop.toml declares sensitive, so the run classifies red.
    _triage(tmp_path, ["packages/devloop/devloop/cli.py"],
            "--set", "labels.on_gate_failure=escalate-me")
    assert json.loads(capsys.readouterr().out)["label"] == "escalate-me"


def test_schema_glob_is_case_insensitive():
    # docs/SCHEMA.md (uppercase) must be caught by the *schema* pattern.
    r = triage.classify_pr(_signals(files_touched=["docs/SCHEMA.md"]), TRIAGE_CFG)
    assert r["lane"] == "red"


@pytest.mark.parametrize("gates_text, judge_text, needle", [
    ("[]", '{"criteria": [], "findings": []}', "diff gate"),
    ('[{"id": "d", "kind": "diff", "changed_lines": 3}]', '["not", "an", "object"]', "judge"),
])
def test_triage_cli_unreadable_inputs_are_errors(tmp_path, capsys, gates_text, judge_text, needle):
    """No diff gate count, or a judge return that is not an object, is exit 2
    naming the input, never a lane read from a guess."""
    root = _shipped(tmp_path, ["tests/test_foo.py"])
    (tmp_path / "g.json").write_text(gates_text, encoding="utf-8")
    (tmp_path / "j.json").write_text(judge_text, encoding="utf-8")
    rc = cli.main(["triage", "--gates-json", str(tmp_path / "g.json"),
                   "--judge-json", str(tmp_path / "j.json"), "--baseline", "green",
                   "--cwd", str(root), "--base-ref", "base"])
    assert rc == 2
    assert needle in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# Judgment-gate validators (issue #99, fused by #39 / dec-611cbd8a) — the rail
# never EXECUTES the judge; it validates what the orchestrator's
# subagent returned, rejecting a schema-violating return with per-field
# reasons so the orchestrator re-asks instead of str()-coercing garbage
# downstream.


def _gate(gate_id):
    return next(g for g in cli.load_config()["gates"] if g["id"] == gate_id)


def _met(*ids):
    return [{"id": i, "verdict": "met", "evidence": f"{i} observed"} for i in ids]


def test_gate_registries_are_disjoint_and_cover_the_pipeline():
    """Every kind has exactly one verb (boundary spec §3): a kind is either
    executed by the rail or validated by it, never both, and the shipped gate
    pipeline names no kind outside the two registries. One judgment kind."""
    assert not (set(gates.DETERMINISTIC) & set(gates.JUDGMENT))
    kinds = {g["kind"] for g in cli.load_config()["gates"]}
    assert kinds <= set(gates.DETERMINISTIC) | set(gates.JUDGMENT)
    assert set(gates.JUDGMENT) == {"judge"}


def test_validate_judge_all_met_passes_whatever_the_findings_say():
    """AC1: the fused envelope carries criteria verdicts AND findings; the
    gate passes iff no criterion is not-met. A problem finding outside the
    contract is advisory (findings comment + triage lane) — it never blocks."""
    result = gates.validate(_gate("judge"), {
        "criteria": _met("AC1", "AC2"),
        "findings": [{"severity": "problem", "finding": "out-of-contract concern"}],
    })
    assert set(result) == {"id", "kind", "passed", "summary", "detail", "reasons"}
    assert result["id"] == "judge" and result["kind"] == "judge"
    assert result["passed"] is True
    assert result["reasons"] == []
    assert "2/2" in result["summary"]


def test_validate_judge_one_not_met_fails_the_gate_without_rejecting():
    """A `not-met` is a FIX ROUND, not a schema rejection — passed=False with
    empty reasons (rc 1, not rc 2). No findings at all is still a verdict.
    #56: the reserved `intent` id is one more criterion to the rail."""
    result = gates.validate(_gate("judge"), {"criteria": [
        {"id": "AC1", "verdict": "met", "evidence": "e"},
        {"id": "intent", "verdict": "not-met",
         "evidence": "ran uv run devloop check --issue 40 with a two-issue cycle; forked until killed"},
    ], "findings": []})
    assert result["passed"] is False
    assert result["reasons"] == []


def test_validate_judge_rejects_unknown_verdict_naming_field_and_value():
    """An enum the judge invented is REJECTED (not coerced), and the reason
    names the offending field path and the value — that is what makes the
    re-ask actionable."""
    result = gates.validate(_gate("judge"), {"criteria": [
        {"id": "AC1", "verdict": "met", "evidence": "e"},
        {"id": "AC2", "verdict": "probably", "evidence": "e"},
    ], "findings": []})
    assert result["passed"] is False
    assert len(result["reasons"]) == 1
    reason = result["reasons"][0]
    assert "criteria[1].verdict" in reason
    assert "probably" in reason and "met" in reason and "not-met" in reason


def test_validate_judge_rejects_empty_evidence_and_empty_criteria():
    """Evidence is the judge's whole contribution: a blank string is a schema
    violation, and so is a return with no criteria at all."""
    result = gates.validate(_gate("judge"), {"criteria": [
        {"id": "AC1", "verdict": "met", "evidence": "   "},
    ], "findings": []})
    assert result["passed"] is False
    assert any("criteria[0].evidence" in r for r in result["reasons"])

    empty = gates.validate(_gate("judge"), {"criteria": [], "findings": []})
    assert empty["passed"] is False
    assert any(r.startswith("criteria:") for r in empty["reasons"])


def test_validate_judge_rejects_bad_or_missing_findings():
    """findings[] is half the envelope: a retired four-level severity or a
    blank finding is rejected naming the field, and a return that omits the
    list altogether is a re-ask — silence is not "no findings"."""
    bad = gates.validate(_gate("judge"), {"criteria": _met("AC1"), "findings": [
        {"severity": "major", "finding": "x"},
        {"severity": "note", "finding": ""},
    ]})
    assert bad["passed"] is False
    assert any("findings[0].severity" in r and "major" in r for r in bad["reasons"])
    assert any("findings[1].finding" in r for r in bad["reasons"])
    missing = gates.validate(_gate("judge"), {"criteria": _met("AC1")})
    assert missing["passed"] is False
    assert any(r.startswith("findings:") for r in missing["reasons"])


def test_validate_rejects_non_object_payload_naming_the_payload():
    """A bare string / list pasted by mistake is rejected at the top level for
    every judgment kind — nothing downstream ever sees it."""
    result = gates.validate(_gate("judge"), ["not", "an", "object"])
    assert result["passed"] is False
    assert result["reasons"] == [
        "payload: expected a JSON object, got list"]  # reported once, not per section


def test_diff_gate_reports_the_changed_line_count_as_a_field():
    """The changed-line count travels as a declared field, never as
    prose the orchestrator re-parses out of the summary. Binary rows count 0."""
    result = gates.evaluate_diff_gate({"id": "g", "forbidden_paths": []},
                                      "3\t1\ta.py\n2\t0\tb.py\n-\t-\timg.png\n")
    assert result["changed_lines"] == 6


# --- the CLI seam -----------------------------------------------------------


def _write_json(tmp_path, payload):
    p = tmp_path / "return.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_validate_cli_exit_codes_mirror_check(tmp_path, capsys):
    """rc 0 = gate passed, rc 1 = gate failed (fix round), rc 2 = schema
    rejection (re-ask) — the same three-way convention `check` already uses."""
    ok = _write_json(tmp_path, {"criteria": [
        {"id": "AC1", "verdict": "met", "evidence": "e"}], "findings": []})
    assert cli.main(["validate", "--gate", "judge", "--return-json", ok]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True

    failed = _write_json(tmp_path, {"criteria": [
        {"id": "AC1", "verdict": "not-met", "evidence": "e"}], "findings": []})
    assert cli.main(["validate", "--gate", "judge", "--return-json", failed]) == 1
    capsys.readouterr()

    rejected = _write_json(tmp_path, {"criteria": [
        {"id": "AC1", "verdict": "nope", "evidence": "e"}], "findings": []})
    assert cli.main(["validate", "--gate", "judge", "--return-json", rejected]) == 2
    out = json.loads(capsys.readouterr().out)
    assert any("criteria[0].verdict" in r for r in out["reasons"])


def test_validate_cli_rejects_unparseable_json_as_a_re_ask(tmp_path, capsys):
    """A return that is not even JSON is the first thing worth re-asking for —
    rc 2 with a reason, not a traceback."""
    p = tmp_path / "return.json"
    p.write_text("All criteria met.", encoding="utf-8")
    rc = cli.main(["validate", "--gate", "judge", "--return-json", str(p)])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["reasons"] and "JSON" in out["reasons"][0]


def test_validate_cli_refuses_deterministic_kinds(tmp_path, capsys):
    """The mirror of `check`'s refusal: validate is for judgment kinds only,
    and points the caller at the other verb."""
    ok = _write_json(tmp_path, {"criteria": []})
    rc = cli.main(["validate", "--gate", "tests", "--return-json", ok])
    assert rc == 2
    assert "check" in json.loads(capsys.readouterr().out)["error"]
    rc = cli.main(["validate", "--gate", "nope", "--return-json", ok])
    assert rc == 2
    assert "no gate" in json.loads(capsys.readouterr().out)["error"]


def test_normalize_trace_is_documented_as_a_backstop_not_the_validation_seam():
    """#99 moved gate-return enforcement to the rail's `validate` verb. mint's
    normalizer survives only to backstop legacy / degraded input — and says so,
    so the next reader adds enforcement at the seam instead of here."""
    doc = " ".join(mint._normalize_trace.__doc__.split())
    assert "backstop" in doc
    assert "validate" in doc  # names the verb that owns enforcement


def test_skills_projection_is_the_stage_dispatch_shape():
    """`skills[]` is the loop's stage-dispatch log: the projection says so and
    keeps exactly the five contracted fields."""
    assert "stage" in mint._normalize_skill.__doc__
    # And it still projects exactly the five contracted fields — the parking
    # decision changes the prose, never the shipped shape.
    assert set(mint._normalize_skill({"id": "x", "extra": 1}, "skills[0]", [])) == {
        "id", "role", "skill", "outcome", "fix_rounds_attributed"}


# ---------------------------------------------------------------------------
# Demo criteria: the implementer runs the issue's demo, the judge scores it
# from recorded evidence; deviations reach the findings comment and the trace.


def test_build_trajectory_trace_carries_deviations_beside_edge_cases():
    """The trace keeps a `deviations` list of strings beside `edge_cases`;
    a non-string entry is dropped like an edge case's."""
    payload = mint.build_trajectory(
        {"number": 77, "title": "demo criteria", "labels": []},
        branch="loop/issue-77", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
        trace={"edge_cases": ["e"], "deviations": ["d", 3]},
    )
    trace = payload["frontmatter"]["trace"]
    assert trace["edge_cases"] == ["e"]
    assert trace["deviations"] == ["d"]


def test_parse_verify_lines_ignores_demo_lines():
    """A `demo:` criterion is the implementer's to run; the verify rail never
    executes it."""
    body = ("- [ ] demo: run `uv run devloop config`; stdout names the judge gate\n"
            "- [ ] verify: `uv run pytest packages/devloop -q`\n")
    assert gates.parse_verify_lines(body) == ["uv run pytest packages/devloop -q"]


def test_standing_orders_require_running_every_demo_on_the_final_commit():
    orders = " ".join(pack.STANDING_ORDERS.split())
    assert "`demo:`" in orders
    assert "final commit" in orders
    assert "`<return file>.demo/demo.md`" in orders
    assert "`sha: <full commit sha>`" in orders
    assert "missing capability" in orders


def test_standing_orders_run_the_one_check_call_before_returning():
    """One rail call replaces the separate tests and verify-line runs."""
    orders = " ".join(pack.STANDING_ORDERS.split())
    assert "Before returning, run `devloop check --issue <N>`" in orders
    assert "every `verify:` line from the worktree root" not in orders


def test_validate_judge_has_no_third_verdict():
    """The judge's verdicts are `met` and `not-met`: an `uncertain` is a
    schema rejection (rc 2, a re-ask), never a verdict."""
    assert gates.VERDICTS == ("met", "not-met")
    result = gates.validate(_gate("judge"), {"criteria": [
        {"id": "AC1", "verdict": "uncertain", "evidence": "e"}], "findings": []})
    assert result["reasons"] == [
        "criteria[0].verdict: 'uncertain' is not one of met | not-met"]


def test_standing_orders_rerun_every_demo_in_a_fix_round():
    orders = " ".join(pack.STANDING_ORDERS.split())
    assert "A fix round re-runs every `demo:` criterion on the new tip" in orders


# --- the judge's constitution contract and its shape posture -----------------


def _rule(rid, evidence="fx/core.py:12 threads the same three arguments"):
    return {"id": rid, "verdict": "not-met", "evidence": evidence}


def test_validate_judge_accepts_a_cited_rule_criterion_as_blocking():
    """A `rule:<n>` criterion for rules 3, 6, 7 and 8, cited by file and
    line, is a real verdict: it fails the gate without a rejection."""
    for n in (3, 6, 7, 8):
        result = gates.validate(_gate("judge"), {
            "criteria": [*_met("AC1"), _rule(f"rule:{n}")], "findings": []})
        assert result["reasons"] == [], n
        assert result["passed"] is False


@pytest.mark.parametrize("rid", ["rule:1", "rule:4", "rule:2", "rule:9", "rule:x", "rule:"])
def test_validate_judge_rejects_a_rule_id_outside_the_contract(rid):
    result = gates.validate(_gate("judge"), {
        "criteria": [*_met("AC1"), _rule(rid)], "findings": []})
    assert result["reasons"] and f"criteria[1].id: {rid!r}" in result["reasons"][0]


@pytest.mark.parametrize("evidence", ["builds a bare tuple in fx/core.py", "line 12 of core"])
def test_validate_judge_rejects_a_rule_criterion_without_a_file_line_citation(evidence):
    result = gates.validate(_gate("judge"), {
        "criteria": [_rule("rule:3", evidence)], "findings": []})
    assert result["reasons"] and "criteria[0].evidence" in result["reasons"][0]


CASE = {"verdict": "not-met",
        "flow": "rule 1: tasks.py has one consumer\nrule 4: the store reads bottom-up",
        "owns": [{"module": "operations/tasks.py", "owns": "the Task object and its store"}],
        "options": ["fold task_seam into tasks.py", "a Task object over the store"]}


def test_validate_shape_passes_met_and_fails_a_restructure_case():
    gate = _gate("judge")
    met = gates.validate(gate, {"verdict": "met", "flow": "", "owns": [], "options": []},
                         posture="shape")
    assert met["passed"] is True and met["reasons"] == []
    case = gates.validate(gate, CASE, posture="shape")
    assert case["passed"] is False and case["reasons"] == []


@pytest.mark.parametrize("change, field", [
    ({"verdict": "maybe"}, "verdict"),
    ({"flow": "1\n2\n3\n4\n5\n6"}, "flow"),
    ({"flow": ""}, "flow"),
    ({"owns": []}, "owns"),
    ({"owns": [{"module": "a.py"}]}, "owns[0].owns"),
    ({"options": []}, "options"),
    ({"options": ["a", "b", "c", "d"]}, "options"),
    ({"options": ["a", ""]}, "options[1]"),
])
def test_validate_shape_rejects_a_case_off_its_schema(change, field):
    result = gates.validate(_gate("judge"), {**CASE, **change}, posture="shape")
    assert result["reasons"] and any(r.startswith(field) for r in result["reasons"])


def test_validate_cli_takes_the_shape_posture(tmp_path, capsys):
    case = tmp_path / "shape.json"
    case.write_text(json.dumps(CASE), encoding="utf-8")
    assert cli.main(["validate", "--gate", "judge", "--posture", "shape",
                     "--return-json", str(case)]) == 1
    assert json.loads(capsys.readouterr().out)["reasons"] == []
    assert cli.main(["validate", "--gate", "judge", "--return-json", str(case)]) == 2




# ---------------------------------------------------------------------------
# The command doc: the three tests that read its text.

COMMAND_DOC = cli.REPO_ROOT / "docs" / "agents" / "issue-loop.command.md"


def _command_doc() -> str:
    return COMMAND_DOC.read_text(encoding="utf-8")


def test_command_doc_is_at_most_2500_words():
    words = len(_command_doc().split())
    assert words <= 2500, f"{words} words; the cap is 2500"


def test_every_devloop_verb_the_command_doc_names_is_a_subcommand():
    sub = next(a for a in cli.build_arg_parser()._actions
               if isinstance(a, argparse._SubParsersAction))
    named = set(re.findall(r"\bdevloop ([a-z][a-z-]*)", _command_doc()))
    assert named, "the command doc names no devloop verb"
    assert named <= set(sub.choices), named - set(sub.choices)


def test_command_doc_outside_host_extension_blocks_never_needs_a_vault():
    spine = re.sub(r"<!-- host-extension:.*?<!-- /host-extension -->", "",
                   _command_doc(), flags=re.DOTALL)
    assert "host-extension" not in spine, "an unbalanced host-extension block"
    for token in ("vault", "prime", "priming", "trajector"):
        assert token not in spine.lower(), token
