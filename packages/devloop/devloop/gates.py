"""The Gate protocol: deterministic executors and judgment validators.

Every gate kind has exactly one verb, and which verb it has states which
plane runs it (boundary spec §3): a DETERMINISTIC kind is *executed* here
(``execute(gate_cfg, cwd, base_ref) -> GateResult``); a JUDGMENT kind is
never executed by the rail — the /issue-loop orchestrator dispatches a
subagent and the rail *validates* its return
(``validate(gate_cfg, raw) -> GateResult``), rejecting a schema-violating
return so the orchestrator re-asks. ``GateResult`` is a plain dict —
``{id, kind, passed, summary, detail}`` — shared by both verbs so downstream
consumers never care which produced it; judgment results add ``reasons``,
the rejection's per-field detail (empty on a real verdict).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from devloop import paths

# Exit codes a shell gives a command it cannot find: POSIX sh 127, cmd.exe 9009.
_NOT_FOUND = (127, 9009)


def run_command_gate(gate: dict, cwd: Path, base_ref: str | None = None) -> dict:
    """``base_ref`` is unused — it is in the signature so both deterministic
    executors share the registry's one calling convention.

    ``expect`` (set only by the verify rail, never a loop.toml key) is a
    substring stdout must carry for the gate to pass. A command the shell
    cannot find fails with the shell's own "not found" line in the summary, so
    a missing binary is named, never read as a plain non-zero exit.
    """
    timeout_sec = gate.get("timeout_sec", 900)
    try:
        proc = subprocess.run(
            gate["cmd"],
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,  # a failing command IS the gate result, not an exception
        )
    except subprocess.TimeoutExpired:
        # a timeout is a gate RESULT too — the orchestrator consumes JSON,
        # never tracebacks
        return {
            "id": gate["id"],
            "kind": "command",
            "passed": False,
            "summary": f"`{gate['cmd']}` timed out after {timeout_sec}s",
            "detail": "",
        }
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-30:])
    expect = gate.get("expect", "")
    found = expect in proc.stdout
    summary = f"`{gate['cmd']}` exited {proc.returncode}"
    if expect:
        summary += f"; stdout {'contains' if found else 'lacks'} {expect!r}"
    if proc.returncode in _NOT_FOUND and proc.stderr.strip():
        summary += f" — {proc.stderr.strip().splitlines()[-1]}"
    return {
        "id": gate["id"],
        "kind": "command",
        "passed": proc.returncode == 0 and found,
        "summary": summary,
        "detail": tail,
    }


# ---------------------------------------------------------------------------
# The verify rail (dec-2f5bf66a): an issue's own `verify:` lines, run as
# ad-hoc command gates. Strictness justification (dec-034ee0f7): the result
# is the rail's, so the orchestrator cannot soften a red line into prose.

# `- [ ] verify: `<cmd> [=> <text>]`` — the checklist prefix is optional, the
# backticks are stripped, and the LAST ` => ` splits off the expected stdout.
_VERIFY_LINE = re.compile(r"^\s*(?:[-*]\s+(?:\[[ xX]\]\s+)?)?verify:\s*(.+?)\s*$")
# The `--issue N` token a verify line may carry, however spelled (`=`, quotes,
# extra spaces); the number is anchored so `--issue 400` never reads as 40.
_ISSUE_TOKEN = re.compile(r"""--issue\s*=?\s*["']?(\d+)\b""")
# The chain of issues whose lines are running, outermost first (comma-joined
# in the environment so child processes see it): the fixed-point guard.
VERIFY_ENV = "DEVLOOP_VERIFY_ISSUE"


def parse_verify_lines(body: str) -> list[tuple[str, str]]:
    """``[(command, expected_stdout_substring)]`` in body order; ``""`` when
    the line carries no ``=>``. Prose that merely mentions ``verify:`` does
    not start a line with it, and a line inside a ``` fence is an example,
    so neither parses. The LAST `` => `` always splits: a command that needs
    a literal `` => `` must end with an expected-text clause of its own."""
    lines, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not (m := _VERIFY_LINE.match(line)):
            continue
        text = m.group(1).removeprefix("`").removesuffix("`")
        cmd, sep, expect = text.rpartition(" => ")
        lines.append((cmd.strip(), expect.strip()) if sep else (text.strip(), ""))
    return lines


def run_verify_lines(number: int, body: str, cwd: Path) -> dict:
    """``check --issue``'s result: ``{issue, results: [GateResult…], summary}``
    — one command GateResult per line (``id: verify:<k>``, k = the line's
    position in the body; the tests gate's runner and default timeout);
    ``summary`` reads ``no verify lines`` for a body without any (a pass).

    A line re-running ``check --issue`` on an issue already in the chain
    (``VERIFY_ENV``) is a fixed point: excluded, counted in the summary. The
    backstop for a spelling the token match cannot see is the chain itself:
    an issue entered a second time over is ONE red result naming the cycle.
    """
    chain = [int(n) for n in os.environ.get(VERIFY_ENV, "").split(",") if n.strip()]
    if chain.count(number) >= 2:
        cycle = " → ".join(str(n) for n in [*chain, number])
        return {"issue": number, "summary": "recursive verify: " + cycle,
                "results": [{"id": "verify:cycle", "kind": "command", "passed": False,
                             "summary": f"recursive verify: {cycle}", "detail": ""}]}
    parsed = parse_verify_lines(body)
    lines = [(k, c, e) for k, (c, e) in enumerate(parsed, 1)
             if not any(int(n) in chain for n in _ISSUE_TOKEN.findall(c))]
    excluded = len(parsed) - len(lines)
    prev = os.environ.get(VERIFY_ENV)
    os.environ[VERIFY_ENV] = ",".join(str(n) for n in [*chain, number])
    try:
        results = [run_command_gate({"id": f"verify:{k}", "kind": "command",
                                     "cmd": cmd, "expect": expect}, cwd)
                   for k, cmd, expect in lines]
    finally:
        if prev is None:
            del os.environ[VERIFY_ENV]
        else:
            os.environ[VERIFY_ENV] = prev
    passed = sum(r["passed"] for r in results)
    summary = (f"{passed}/{len(results)} verify lines passed" if results
               else "no verify lines")
    if excluded:
        summary += f"; {excluded} self-referential line(s) excluded (fixed point)"
    return {"issue": number, "results": results, "summary": summary}


def evaluate_diff_gate(gate: dict, numstat: str) -> dict:
    """Pure evaluation of `git diff --numstat` output: a forbidden-paths check
    only (dec-cf8f0d33 dropped the line cap — size is a triage signal, never a
    block). The changed-line count is reported for the PR body.

    ``forbidden_paths`` patterns use :func:`devloop.paths.match`'s three forms;
    every shipped entry is the trailing-``/`` prefix case (the old ``startswith``).
    """
    forbidden = gate.get("forbidden_paths", [])
    touched_forbidden, total = [], 0
    for line in numstat.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        total += (0 if added == "-" else int(added)) + (0 if deleted == "-" else int(deleted))
        if any(paths.match(path, p) for p in forbidden):
            touched_forbidden.append(path)
    return {
        "id": gate["id"],
        "kind": "diff",
        "passed": not touched_forbidden,
        "summary": (f"touches forbidden paths: {', '.join(touched_forbidden)}"
                    if touched_forbidden else f"{total} changed lines, no forbidden paths"),
        "detail": "",
    }


def run_diff_gate(gate: dict, cwd: Path, base_ref: str) -> dict:
    numstat = subprocess.run(
        ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return evaluate_diff_gate(gate, numstat)


# ---------------------------------------------------------------------------
# Judgment kinds — validated here, executed by the orchestrator. Nothing is
# coerced: an invented enum or a blank evidence line is rejected naming its
# field path, because coercing it would put the judge's mistake in the
# trajectory as fact.

VERDICTS = ("met", "not-met")
SEVERITIES = ("critical", "major", "minor", "nit")
SIMPLIFY_OUTCOMES = ("applied", "reverted", "lean")


def reject(gate: dict, reasons: list[str]) -> dict:
    """A GateResult for a return that never became a verdict."""
    return {
        "id": gate["id"],
        "kind": gate["kind"],
        "passed": False,
        "summary": f"schema-rejected: {'; '.join(reasons)}",
        "detail": "\n".join(reasons),
        "reasons": reasons,
    }


def _verdict(gate: dict, reasons: list[str], *, passed: bool, summary: str) -> dict:
    return (reject(gate, reasons) if reasons else
            {"id": gate["id"], "kind": gate["kind"], "passed": passed,
             "summary": summary, "detail": "", "reasons": []})


def _entries(raw: dict, key: str, reasons: list[str],
             *, allow_empty: bool = False) -> list[tuple[int, dict]]:
    """The list-of-objects unwrap every judgment schema starts with."""
    value = raw.get(key)
    if not isinstance(value, list):
        reasons.append(f"{key}: expected a list, got {type(value).__name__}")
        return []
    if not value and not allow_empty:
        reasons.append(f"{key}: expected at least one entry, got none")
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            reasons.append(f"{key}[{i}]: expected an object, got {type(entry).__name__}")
    return [(i, e) for i, e in enumerate(value) if isinstance(e, dict)]


def _text(entry: dict, where: str, key: str, reasons: list[str]) -> None:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        reasons.append(f"{where}.{key}: expected a non-empty string, got {value!r}")


def _enum(entry: dict, where: str, key: str, allowed: tuple[str, ...],
          reasons: list[str]) -> str:
    value = entry.get(key)
    if value not in allowed:
        reasons.append(f"{where}.{key}: {value!r} is not one of {' | '.join(allowed)}")
        return ""
    return value


def validate_judge(gate: dict, raw: dict) -> dict:
    """The one judgment stage (dec-611cbd8a, dec-2d4bc03d):
    ``{criteria: [{id, verdict: met|not-met, evidence}],
    findings: [{severity: critical|major|minor|nit, finding}]}``.

    ``criteria`` is one entry per acceptance criterion and carries the whole
    authority: the gate passes per the gate's ``threshold`` — ``majority``
    needs strictly more than half met, anything else is read as ``all`` (gates
    are file-only config, a trusted input — unlike the subagent return here).
    ``findings`` is everything the judge saw outside the contract: schema-
    checked so it can travel to the PR body and the triage lane as data, but
    never a verdict — a critical finding blocks nothing. The list may be empty;
    it may not be missing, so silence never reads as a clean review.
    """
    reasons: list[str] = []
    verdicts = []
    for i, entry in _entries(raw, "criteria", reasons):
        where = f"criteria[{i}]"
        _text(entry, where, "id", reasons)
        _text(entry, where, "evidence", reasons)
        verdicts.append(_enum(entry, where, "verdict", VERDICTS, reasons))
    findings = _entries(raw, "findings", reasons, allow_empty=True)
    for i, entry in findings:
        where = f"findings[{i}]"
        _text(entry, where, "finding", reasons)
        _enum(entry, where, "severity", SEVERITIES, reasons)
    threshold = gate.get("threshold", "all")
    met = sum(v == "met" for v in verdicts)
    passed = (met * 2 > len(verdicts) if threshold == "majority"
              else met == len(verdicts))
    return _verdict(gate, reasons, passed=passed,
                    summary=(f"{met}/{len(verdicts)} criteria met (threshold: "
                             f"{threshold}); {len(findings)} findings"))


def validate_simplify(gate: dict, raw: dict) -> dict:
    """``{outcome: applied|reverted|lean, lines_delta, cuts[], kept[]}`` — the
    same envelope the trajectory trace stores, so a validated return carries
    into the note unchanged. Never fails the pipeline (``required = false``):
    a schema-valid return always passes, its "failure" mode being the revert.
    """
    reasons: list[str] = []
    outcome = _enum(raw, "payload", "outcome", SIMPLIFY_OUTCOMES, reasons)
    delta = raw.get("lines_delta")
    if isinstance(delta, bool) or not isinstance(delta, int):
        reasons.append(f"payload.lines_delta: expected an int, got {delta!r}")
    for key in ("cuts", "kept"):
        for i, entry in _entries(raw, key, reasons, allow_empty=True):
            _text(entry, f"{key}[{i}]", "what", reasons)
            _text(entry, f"{key}[{i}]", "why", reasons)
    return _verdict(gate, reasons, passed=True, summary=f"{outcome}: {delta} lines")


# The two registries the protocol's structural claim reduces to: every kind has
# exactly one verb, and which verb it has states which plane runs it. `check`
# dispatches ONLY through DETERMINISTIC — any other kind, judgment-side or typo,
# gets the LLM-judged error; `validate` dispatches ONLY through JUDGMENT.
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"judge": validate_judge, "simplify": validate_simplify}

# The keys each kind's verb reads (beyond the shared id/kind/required). The
# config loader rejects a gate entry carrying any other key, naming it — so a
# knob the subtractive pass deleted (block_on, max_changed_lines,
# smells_baseline) is refused rather than silently inert, the same posture
# `--set` has always taken on an unknown scalar knob.
GATE_KEYS = {
    "command": {"cmd", "timeout_sec"},
    "diff": {"forbidden_paths"},
    "judge": {"threshold"},
    "simplify": {"skill", "rerun", "revert_note"},
}
COMMON_GATE_KEYS = {"id", "kind", "required"}


def validate(gate: dict, raw: object) -> dict:
    """Validate one judgment gate's subagent return (the JUDGMENT dispatch).

    Every schema starts from a JSON object, so that guard lives here once;
    per-kind validators take it from there. Raises ``KeyError`` for a
    deterministic or unknown kind — callers check membership first, exactly
    as ``check`` does against ``DETERMINISTIC``.
    """
    if not isinstance(raw, dict):
        return reject(gate, [f"payload: expected a JSON object, got {type(raw).__name__}"])
    return JUDGMENT[gate["kind"]](gate, raw)
