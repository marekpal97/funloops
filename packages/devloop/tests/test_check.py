"""The ticket check, at its three seams: ``check.check(issue)``, the
``devloop check <N>`` verb, and the ``needs_info`` list of ``plan``.

The machine's PATH is stubbed, so the same tools are "installed" everywhere.
"""

import json
import subprocess
from pathlib import Path

import pytest

from devloop import check, cli, github

FIXTURES = Path(__file__).parent / "fixtures" / "check"
INSTALLED = {"uv", "grep", "git", "test", "readlink", "claude"}

VERIFY = "- [ ] verify: `uv run pytest tests/test_widget.py -q`"
DEMO = "- [ ] demo: run `widget build` on the fixture. Observable: the plan prints. Artifact: the output."
WHAT = "`widget build` prints the plan."
WHY = "Nobody can see the plan today."
PASSING = f"""## What

{WHAT}

## Why

{WHY}

## Acceptance criteria

{VERIFY}
{DEMO}
"""
INTERFACES = "\n## Interfaces\n\n- `src/widget.py`: owns the plan.\n"


@pytest.fixture(autouse=True)
def _path(monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda name: name if name in INSTALLED else None)


def _failures(body: str, repo: str = "o/home") -> list[tuple[str, str]]:
    return [(f["rule"], f["line"]) for f in check.check({"body": body, "repo": repo}).failures]


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_a_ticket_that_meets_the_standard_passes():
    report = check.check({"body": PASSING, "repo": "o/home"})
    assert report.ok and report.failures == [] and report.render() == "pass"


LONG = " ".join(["word"] * 201)
NO_PROMPT = "- [ ] demo: drive a session. Harness: claude. Observable: a row. Artifact: the ids."
NO_HARNESS = ("- [ ] demo: drive a session. Harness: nosuchharness. Prompt: `p.md`. "
              "Observable: a row. Artifact: the ids.")


@pytest.mark.parametrize("body, expected", [
    (PASSING.replace(f"## What\n\n{WHAT}\n\n", ""), [("what-missing", "## What")]),
    (PASSING.replace(f"## Why\n\n{WHY}\n\n", ""), [("why-missing", "## Why")]),
    (PASSING.replace(WHAT, LONG), [("what-over-length", LONG)]),
    (PASSING.replace(WHY, LONG), [("why-over-length", LONG)]),
    (PASSING.replace(VERIFY + "\n", ""), [("no-verify", "## Acceptance criteria")]),
    (PASSING.replace(DEMO + "\n", ""), [("no-demo", "## Acceptance criteria")]),
    (PASSING.replace(VERIFY, '- [ ] verify: `uv run "unclosed`'),
     [("verify-unparsable", '- [ ] verify: `uv run "unclosed`')]),
    (PASSING.replace(VERIFY, "- [ ] verify: `nosuchtool --flag`"),
     [("verify-tool-missing", "- [ ] verify: `nosuchtool --flag`")]),
    (PASSING.replace(VERIFY, "- [ ] verify: `MODE=fast uv run pytest`"), []),
    (PASSING.replace(VERIFY, "- [ ] verify: `! grep -q gone notes.txt`"), []),
    (PASSING.replace(" Observable: the plan prints.", ""),
     [("demo-no-observable", DEMO.replace(" Observable: the plan prints.", ""))]),
    (PASSING.replace(" Artifact: the output.", ""),
     [("demo-no-artifact", DEMO.replace(" Artifact: the output.", ""))]),
    (PASSING.replace(DEMO, "- [ ] demo (human): look at it."),
     [("demo-no-observable", "- [ ] demo (human): look at it."),
      ("demo-no-artifact", "- [ ] demo (human): look at it.")]),
    (PASSING.replace(DEMO, NO_PROMPT), [("harness-no-prompt", NO_PROMPT)]),
    (PASSING.replace(DEMO, NO_HARNESS), [("harness-not-installed", NO_HARNESS)]),
    (PASSING.replace(DEMO, "- [ ] demo: check a fixture whose demo reads `Harness: nosuchharness`. "
                           "Observable: it fails. Artifact: the output."), []),
    (PASSING.replace(DEMO, NO_HARNESS.replace("nosuchharness", "the script's own `claude -p` call")), []),
    (PASSING.replace(WHAT, "`src/widget.py` prints the plan."),
     [("files-without-interfaces", "`src/widget.py` prints the plan.")]),
    (PASSING.replace(WHY, "`widget.toml` hides the plan."),
     [("files-without-interfaces", "`widget.toml` hides the plan.")]),
    (PASSING.replace(WHAT, "`src/widget.py` prints the plan.") + INTERFACES, []),
    (PASSING.replace(WHAT, "`/widget` runs in a session."), []),
    (PASSING.replace(WHAT, "A clone at `~/projects/elsewhere` holds the plan.") + INTERFACES,
     [("what-outside-repo", "A clone at `~/projects/elsewhere` holds the plan.")]),
    (PASSING.replace(WHAT, "A clone at `~/projects/elsewhere` holds the plan.") + INTERFACES
     + "- Repo: `o/elsewhere`\n", []),
    (PASSING.replace(WHAT, "The repo `o/other` holds the plan.") + INTERFACES,
     [("what-outside-repo", "The repo `o/other` holds the plan.")]),
    (PASSING.replace(WHAT, "The repo `o/other` holds the plan.") + INTERFACES + "- Repo: `o/other`\n", []),
])
def test_each_rule_quotes_the_line_that_broke_it(body, expected):
    assert _failures(body) == expected


def test_the_two_tickets_as_filed():
    """thinkweave#358 promised a second repo and declared none; thinkweave#344
    meets the standard."""
    promise = ("A private GitHub repo `marekpal97/rsi-rail`, cloned beside the other projects, "
               "holds everything the user tunes at harness level.")
    failures = _failures(_fixture("thinkweave-358.md"), "marekpal97/thinkweave")
    outside = [line for rule, line in failures if rule == "what-outside-repo"]
    assert len(outside) == 1 and outside[0].startswith(promise)
    assert _failures(_fixture("thinkweave-344.md"), "marekpal97/thinkweave") == []


def test_a_failing_report_renders_each_rule_with_its_quoted_line():
    out = check.check({"body": PASSING.replace(DEMO, NO_HARNESS), "repo": "o/home"}).render()
    assert out.splitlines()[0] == "fail"
    assert "harness-not-installed" in out and f"> {NO_HARNESS}" in out


def _gh_issue(monkeypatch, body: str) -> None:
    monkeypatch.setattr(github, "run", lambda args, cwd=None: json.dumps(
        {"body": body, "url": "https://github.com/o/home/issues/7"}))


def test_check_verb_prints_pass_and_exits_zero(monkeypatch, capsys):
    _gh_issue(monkeypatch, PASSING)
    assert cli.main(["check", "7"]) == 0
    assert capsys.readouterr().out.strip() == "pass"


def test_check_verb_prints_the_failures_and_exits_one(monkeypatch, capsys):
    _gh_issue(monkeypatch, PASSING.replace(WHAT, "The repo `o/other` holds the plan."))
    assert cli.main(["check", "7"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("fail") and "what-outside-repo" in out
    assert "> The repo `o/other` holds the plan." in out


def test_check_verb_reports_an_unreadable_issue_as_an_error(monkeypatch, capsys):
    def boom(args, cwd=None):
        raise subprocess.CalledProcessError(1, "gh", stderr="no such issue")
    monkeypatch.setattr(github, "run", boom)
    assert cli.main(["check", "7"]) == 2
    assert "no such issue" in json.loads(capsys.readouterr().out)["error"]


def test_plan_moves_a_failing_frontier_issue_to_needs_info(monkeypatch, capsys):
    def issue(number, body):
        return {"number": number, "title": f"Issue {number}", "state": "OPEN", "repo": "o/home",
                "labels": [{"name": "ready-for-agent"}], "body": body}
    failing = PASSING.replace(VERIFY, "- [ ] verify: `nosuchtool --flag`")
    monkeypatch.setattr(github, "fetch_issues", lambda: [issue(1, PASSING), issue(2, failing)])
    assert cli.main(["plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["number"] for e in out["frontier"]] == [1]
    assert [(e["number"], e["failures"]) for e in out["needs_info"]] == [
        (2, [{"rule": "verify-tool-missing", "line": "- [ ] verify: `nosuchtool --flag`"}])]
