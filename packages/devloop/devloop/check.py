"""The ticket check: one issue against the ticket standard, before any agent runs.

It reads declared shapes only (the ``##`` sections, the ``verify:`` and
``demo:`` lines, the ``Repo:`` line under Interfaces, the names in backticks)
and asks the machine which tools are installed. It never judges a premise.
"""

from __future__ import annotations

import re
import shlex
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypedDict

from devloop import gates

MAX_WORDS = 200

RULES = {
    "what-missing": "the ticket has no What section",
    "why-missing": "the ticket has no Why section",
    "what-over-length": f"the What is over {MAX_WORDS} words",
    "why-over-length": f"the Why is over {MAX_WORDS} words",
    "no-verify": "the ticket has no `verify:` line",
    "no-demo": "the ticket has no `demo:` line",
    "verify-unparsable": "the verify command does not parse as a shell command",
    "verify-tool-missing": "the first word of the verify command is not an installed tool",
    "demo-no-observable": "the demo names no `Observable:`",
    "demo-no-artifact": "the demo names no `Artifact:`",
    "harness-no-prompt": "the demo names a `Harness:` and no `Prompt:`",
    "harness-not-installed": "the demo names a harness that is not installed on this machine",
    "files-without-interfaces": "the line names a file and the ticket has no Interfaces section",
    "what-outside-repo": "the What names a path or repo outside the repo the ticket declares",
}


class Failure(TypedDict):
    rule: str
    line: str


@dataclass
class Report:
    """The check's result: every failure, as the rule it broke and the line quoted."""
    failures: list[Failure]

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        """``pass``, or ``fail`` and each failure in Markdown: the text of the needs-info comment."""
        return "\n".join(["fail" if self.failures else "pass",
                          *(f"\n- **{f['rule']}**: {RULES[f['rule']]}\n  > {f['line']}"
                            for f in self.failures)])


def check(issue: dict) -> Report:
    """Check one issue snapshot (its ``body`` and ``repo``) against every rule."""
    ticket = _Ticket(issue["body"], issue["repo"])
    return Report([{"rule": rule, "line": line} for rule, line in (
        *ticket.prose(), *ticket.verifies(), *ticket.demos(), *ticket.files(), *ticket.scope())])


class _Ticket:
    """An issue body, parsed; each method yields one rule group's ``(rule, line)`` failures."""

    def __init__(self, body: str, repo: str):
        self.body = body
        self.sections = _sections(body)
        self.repos = _REPO.findall("\n".join(self.sections.get("Interfaces", []))) or [repo]

    def prose(self) -> Iterator[tuple[str, str]]:
        for name in ("What", "Why"):
            lines = self.sections.get(name)
            if not lines:
                yield f"{name.lower()}-missing", f"## {name}"
            elif sum(len(line.split()) for line in lines) > MAX_WORDS:
                yield f"{name.lower()}-over-length", lines[0]

    def verifies(self) -> Iterator[tuple[str, str]]:
        found = gates.criteria(self.body)
        if not found:
            yield "no-verify", CRITERIA_HEADING
        for criterion in found:
            try:
                words = shlex.split(criterion.text)
            except ValueError:
                yield "verify-unparsable", criterion.line
                continue
            tool = next((w for w in words if not _PREFIX.fullmatch(w)), "")
            if not shutil.which(tool):
                yield "verify-tool-missing", criterion.line

    def demos(self) -> Iterator[tuple[str, str]]:
        found = gates.criteria(self.body, gates.DEMO)
        if not found:
            yield "no-demo", CRITERIA_HEADING
        for line, _ in found:
            items = _unquoted(line)
            for item in ("Observable", "Artifact"):
                if f"{item}:" not in items:
                    yield f"demo-no-{item.lower()}", line
            if harness := _HARNESS.search(items):
                if "Prompt:" not in items:
                    yield "harness-no-prompt", line
                if not any(shutil.which(w.strip(_PUNCTUATION)) for w in harness.group(1).split()):
                    yield "harness-not-installed", line

    def files(self) -> Iterator[tuple[str, str]]:
        if self.sections.get("Interfaces"):
            return
        for name in ("What", "Why"):
            for line in self.sections.get(name, []):
                if any(_is_file(w) for w in _named(line)):
                    yield "files-without-interfaces", line

    def scope(self) -> Iterator[tuple[str, str]]:
        for line in self.sections.get("What", []):
            if any(self._outside(w) for w in _named(line)):
                yield "what-outside-repo", line

    def _outside(self, word: str) -> bool:
        """An absolute or home path that passes through no declared repo, or a
        repo of a declared owner that is not declared."""
        if _ABSOLUTE.match(word):
            return not {r.split("/")[1] for r in self.repos} & set(word.split("/"))
        return (word.split("/")[0] in {r.split("/")[0] for r in self.repos} and "/" in word
                and not any(word == r or word.startswith(r + "/") for r in self.repos))


CRITERIA_HEADING = "## Acceptance criteria"
_REPO = re.compile(r"Repo:\s*`?([\w.-]+/[\w.-]+)")
_HARNESS = re.compile(r"Harness:(.*?)(?=Prompt:|Observable:|Artifact:|$)")
_PREFIX = re.compile(r"!|\w+=.*")
_ABSOLUTE = re.compile(r"~|\$HOME|\.\./|/[^/]+/")
_BASENAME = re.compile(r"[\w-]+\.[A-Za-z]\w{0,4}")
_CODE = re.compile(r"`([^`]+)`")
_PUNCTUATION = "`.,;:()'\""


def _sections(body: str) -> dict[str, list[str]]:
    """The non-empty lines under each ``## `` heading, by heading."""
    sections: dict[str, list[str]] = {}
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            lines = sections.setdefault(line[3:].strip(), [])
        elif line.strip():
            lines.append(line.strip())
    return sections


def _named(line: str) -> list[str]:
    """Every word the line carries in backticks."""
    return [word for span in _CODE.findall(line) for word in span.split()]


def _unquoted(line: str) -> str:
    """The line with no colon inside backticks, so a quoted item label is not an item."""
    return _CODE.sub(lambda span: span.group(0).replace(":", " "), line)


def _is_file(word: str) -> bool:
    """A path with a directory part, or a bare ``name.ext``; a slash command is neither."""
    return "/" in word[1:] or bool(_BASENAME.fullmatch(word))
