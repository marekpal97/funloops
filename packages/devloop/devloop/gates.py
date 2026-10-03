"""The Gate protocol: deterministic executors and judgment validators.

Every gate kind has exactly one verb. A DETERMINISTIC kind is executed
here (``execute(gate_cfg, cwd, base_ref) -> GateResult``). A JUDGMENT kind
is never executed by the rail; the orchestrator dispatches a subagent and
the rail validates its return (``validate(gate_cfg, raw) -> GateResult``),
rejecting a schema violation so the orchestrator re-asks. ``GateResult`` is
``{id, kind, passed, summary, detail}``; judgment results add ``reasons``,
empty on a real verdict.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from devloop import paths

# Exit codes a shell gives a command it cannot find: POSIX sh 127, cmd.exe 9009.
_NOT_FOUND = (127, 9009)


def run_command_gate(gate: dict, cwd: Path, base_ref: str | None = None) -> dict:
    """Run one command gate; exit 0 passes. ``base_ref`` is unused and keeps
    the executors' shared signature. A command the shell cannot find is named
    in the summary."""
    timeout_sec = gate.get("timeout_sec", 900)
    try:
        proc = subprocess.run(
            gate["cmd"],
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A timeout is a gate result; the orchestrator consumes JSON, never tracebacks.
        return {
            "id": gate["id"],
            "kind": "command",
            "passed": False,
            "summary": f"`{gate['cmd']}` timed out after {timeout_sec}s",
            "detail": "",
        }
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-30:])
    summary = f"`{gate['cmd']}` exited {proc.returncode}"
    if proc.returncode in _NOT_FOUND and proc.stderr.strip():
        summary += f" — {proc.stderr.strip().splitlines()[-1]}"
    return {
        "id": gate["id"],
        "kind": "command",
        "passed": proc.returncode == 0,
        "summary": summary,
        "detail": tail,
    }


# ---------------------------------------------------------------------------
# The verify rail: an issue's own `verify:` lines, run as command gates. The
# result is the rail's, so the orchestrator cannot soften a red line into prose.

_VERIFY_LINE = re.compile(r"^\s*(?:[-*]\s+(?:\[[ xX]\]\s+)?)?verify:\s*(.+?)\s*$")


def parse_verify_lines(body: str) -> list[str]:
    """The body's ``verify:`` commands in body order, backticks stripped. A
    line inside a ``` fence does not parse."""
    lines, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not (m := _VERIFY_LINE.match(line)):
            continue
        lines.append(m.group(1).removeprefix("`").removesuffix("`").strip())
    return lines


def verify_gates(body: str) -> list[dict]:
    """One command gate per verify line, ``verify:<k>`` counting from 1."""
    return [{"id": f"verify:{k}", "kind": "command", "cmd": cmd}
            for k, cmd in enumerate(parse_verify_lines(body), 1)]


def evaluate_diff_gate(gate: dict, numstat: str) -> dict:
    """Evaluate ``git diff --numstat`` output against ``forbidden_paths``
    (:func:`devloop.paths.match` forms). Size never blocks; the changed-line
    count travels as ``changed_lines`` for the PR body and the simplify
    gate's size threshold."""
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
        "changed_lines": total,
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
# Judgment kinds: validated here, executed by the orchestrator. Nothing is
# coerced, because a coerced value would put the judge's mistake in the
# trajectory as fact.

VERDICTS = ("met", "not-met")
SEVERITIES = ("problem", "note")
SIMPLIFY_OUTCOMES = ("applied", "reverted", "lean", "skipped-small")


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
    """Validate a judge return: ``{criteria: [{id, verdict: met|not-met,
    evidence}], findings: [{severity: problem|note, finding}]}``.

    The gate passes per ``threshold``: ``majority`` needs more than half met,
    anything else reads as ``all``. ``findings`` never decides the verdict;
    it may be empty but not missing, so silence never reads as a clean review.
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
    """Validate a simplify return: ``{outcome: applied|reverted|lean|
    skipped-small, lines_delta, cuts[], kept[]}``. A schema-valid return
    always passes; its failure mode is the revert."""
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


# `check` dispatches only through DETERMINISTIC; `validate` only through JUDGMENT.
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"judge": validate_judge, "simplify": validate_simplify}

# The keys each kind's verb reads beyond id/kind/required; the config loader
# refuses any other key by name, so a deleted knob is never silently inert.
GATE_KEYS = {
    "command": {"cmd", "timeout_sec"},
    "diff": {"forbidden_paths"},
    "judge": {"threshold"},
    "simplify": {"skill", "rerun", "revert_note", "min_diff_lines"},
}
COMMON_GATE_KEYS = {"id", "kind", "required"}


def validate(gate: dict, raw: object) -> dict:
    """Validate one judgment gate's subagent return. A non-object is rejected
    here; raises ``KeyError`` for a deterministic or unknown kind."""
    if not isinstance(raw, dict):
        return reject(gate, [f"payload: expected a JSON object, got {type(raw).__name__}"])
    return JUDGMENT[gate["kind"]](gate, raw)
