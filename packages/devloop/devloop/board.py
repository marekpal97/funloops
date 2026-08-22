"""Board hygiene: the conventions ``dag.py`` depends on, as checks.

The loop reads one board grammar — sub-issue = epic membership, native
blocked-by = ordering, ``[labels]`` = triage rungs (funloops#9). Nothing
enforced that grammar, so every mint route invented its own (three epic
conventions, four title-ordering prefixes, per-repo label drift). This module
is the enforcing seam: pure checks over a board snapshot (shape owned by
``devloop.github.fetch_board``), each producing a finding and — when the fix
is mechanical, not a judgment — an op the sweep can replay through ``gh``.

Findings carry ``severity``: ``error`` (breaks ``plan`` or the grammar),
``warn`` (a human should look), ``info`` (worth knowing, nothing to do).
Ops are the whole vocabulary ``sweep --apply`` knows; anything needing a
human verdict (which rung, which track, is this epic really done) is a
finding with no op, by design.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

EPIC_LABEL = "epic"
RUNG_ROLES = ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human",
              "wontfix", "arch-proposal")
# GitHub seeds every repo with these; none carries meaning on a loop board.
BOILERPLATE_LABELS = ("good first issue", "help wanted", "invalid", "question", "duplicate")
# W1a: / A3: / S2-pre: / QW: — the title-ordering grammars the boards grew.
ORDER_PREFIX_RE = re.compile(r"^\s*\[?([A-Z]{1,2}\d*[a-z]?(-\w+)?)\]?\s*:\s+")
# EPIC: / PRD: — the third and fourth epic conventions; the `epic` label is the one.
EPIC_PREFIX_RE = re.compile(r"^\s*\[?(EPIC|PRD)\]?\s*:\s+", re.IGNORECASE)
# `Blocked-by: #12, #13` body headers (pre-#95 grammar) — only native edges gate.
TEXT_BLOCKER_RE = re.compile(r"Blocked-by:\s*([^\n|]*)", re.IGNORECASE)
IDLE_DAYS = 14

REQUIRED_LABELS = {
    EPIC_LABEL: ("Grouping surface: has sub-issues, closes last, never runnable", "5319e7"),
    "needs-triage": ("Maintainer needs to evaluate this issue", "e4e669"),
    "needs-info": ("Waiting on reporter for more information", "d876e3"),
    "ready-for-agent": ("Fully specified, ready for an AFK agent", "0e8a16"),
    "ready-for-human": ("Requires human implementation", "d93f0b"),
    "wontfix": ("Will not be actioned", "ffffff"),
    "arch-proposal": ("Draft architectural proposal from the slow loop", "c5def5"),
    "agent-claimed": ("Claimed by an /issue-loop run", "1d76db"),
}


def _labels(issue: dict) -> set[str]:
    return {l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])}


def _is_open(issue: dict) -> bool:
    return issue["state"].upper() == "OPEN"


def _finding(check: str, severity: str, repo: str, number: int | None, msg: str,
             op: dict | None = None) -> dict:
    f = {"check": check, "severity": severity, "repo": repo, "number": number, "message": msg}
    if op is not None:
        f["op"] = {"repo": repo, **op}
    return f


def _text_blockers(body: str) -> set[int]:
    m = TEXT_BLOCKER_RE.search(body or "")
    return {int(n) for n in re.findall(r"#(\d+)", m.group(1))} if m else set()


def _epics(issues: list[dict]) -> set[int]:
    """An epic is anything with sub-issues OR already labelled as one."""
    return {i["number"] for i in issues
            if (i.get("sub_issues") or {}).get("total", 0) > 0 or EPIC_LABEL in _labels(i)}


# ---------------------------------------------------------------------------
# Checks — each takes the board and returns findings


def _check_labels(board: dict, cfg: dict, now: datetime) -> list[dict]:
    """The repo's label set must carry the whole triage table + epic + claimed,
    and none of GitHub's boilerplate five (unused noise on a loop board)."""
    repo, have = board["repo"], set(board["labels"])
    used = {l for i in board["issues"] for l in _labels(i)}
    want = dict(REQUIRED_LABELS)
    # Honour the host's own strings for the rungs the loop reads.
    for role in ("runnable", "claimed", "on_gate_failure"):
        name = cfg["labels"].get(role)
        if name and name not in want:
            want[name] = (f"loop role: {role}", "0e8a16")
    out = []
    for name, (desc, color) in want.items():
        if name not in have:
            out.append(_finding("label-missing", "error", repo, None,
                                f"label '{name}' missing from repo",
                                {"op": "create_label", "name": name, "description": desc, "color": color}))
    for name in BOILERPLATE_LABELS:
        if name in have and name not in used:
            out.append(_finding("label-boilerplate", "warn", repo, None,
                                f"unused GitHub boilerplate label '{name}'",
                                {"op": "delete_label", "name": name}))
    return out


def _check_epics(board: dict, cfg: dict, now: datetime) -> list[dict]:
    """Epic grammar: has sub-issues ⇒ labelled ``epic``; never runnable; anchored
    (blocked-by every open child so ``plan --dag <epic>`` scopes to the whole
    tree and the epic closes last); flagged when every child is closed."""
    repo, runnable = board["repo"], cfg["labels"]["runnable"]
    epics = _epics(board["issues"])
    out = []
    for i in board["issues"]:
        if not _is_open(i) or i["number"] not in epics:
            continue
        n, labels = i["number"], _labels(i)
        summary = i.get("sub_issues") or {}
        if EPIC_LABEL not in labels:
            out.append(_finding("epic-unlabelled", "error", repo, n,
                                f"#{n} has {summary.get('total', 0)} sub-issues but no '{EPIC_LABEL}' label",
                                {"op": "add_label", "number": n, "name": EPIC_LABEL}))
        if runnable in labels:
            out.append(_finding("epic-runnable", "error", repo, n,
                                f"epic #{n} carries '{runnable}' — epics group, they don't run",
                                {"op": "remove_label", "number": n, "name": runnable}))
        total, done = summary.get("total", 0), summary.get("completed", 0)
        if total and total == done:
            out.append(_finding("epic-delivered", "warn", repo, n,
                                f"epic #{n}: all {total} sub-issues closed — close it with a delivery "
                                "comment, or re-scope"))
        local_blockers = {b["number"] for b in i.get("blockers", []) if b["repo"] == repo}
        for child in i.get("children", []):
            if child["repo"] == repo and child["state"].upper() == "OPEN" \
                    and child["number"] not in local_blockers:
                out.append(_finding("epic-unanchored", "warn", repo, n,
                                    f"epic #{n} is not blocked-by open child #{child['number']}",
                                    {"op": "add_blocker", "number": n, "blocker": child["number"]}))
    return out


def _check_rungs(board: dict, cfg: dict, now: datetime) -> list[dict]:
    """Exactly one triage rung per open non-epic issue; a ``track:`` lane
    wherever the repo uses lanes at all. Which rung / which lane is a human
    call — the only op is ``needs-triage`` on a rung-less issue."""
    repo = board["repo"]
    rungs = set(RUNG_ROLES) | {cfg["labels"]["runnable"], cfg["labels"]["on_gate_failure"]}
    has_tracks = any(l.startswith("track:") for l in board["labels"])
    epics = _epics(board["issues"])
    out = []
    for i in board["issues"]:
        if not _is_open(i):
            continue
        n, labels = i["number"], _labels(i)
        on = sorted(labels & rungs)
        if len(on) > 1:
            out.append(_finding("rung-contradictory", "error", repo, n,
                                f"#{n} carries {len(on)} triage rungs: {', '.join(on)}"))
        elif not on and n not in epics:
            # The one rung that asserts nothing but "no verdict yet" — honest
            # to apply mechanically, and it puts the issue in /triage's queue.
            out.append(_finding("rung-missing", "warn", repo, n,
                                f"#{n} has no triage rung — invisible to the loop and to /triage",
                                {"op": "add_label", "number": n, "name": "needs-triage"}))
        if has_tracks and not any(l.startswith("track:") for l in labels):
            out.append(_finding("track-missing", "warn", repo, n, f"#{n} has no track: lane"))
    return out


def _check_edges(board: dict, cfg: dict, now: datetime) -> list[dict]:
    """Ordering grammar: titles don't re-encode what native edges already say;
    body ``Blocked-by:`` headers must have a native twin; cross-repo edges are
    legal but invisible to a single-repo ``plan``; runnable-and-unblocked
    issues that sit idle past IDLE_DAYS deserve a look."""
    repo, runnable = board["repo"], cfg["labels"]["runnable"]
    out = []
    epics = _epics(board["issues"])
    for i in board["issues"]:
        n = i["number"]
        blockers, parent = i.get("blockers", []), i.get("parent")
        # A prefix is redundant once the order lives elsewhere: a native edge
        # (open issue), the epic label, or closure — a finished issue has no
        # order left to encode, and the board shows closed titles too.
        has_edge = bool(blockers) or parent is not None or bool(i.get("children"))
        redundant = has_edge or not _is_open(i)
        m = ORDER_PREFIX_RE.match(i.get("title", ""))
        if m is None:
            m = EPIC_PREFIX_RE.match(i.get("title", ""))
            redundant = n in epics or not _is_open(i)
        if m and redundant:
            clean = i["title"][m.end():].strip()
            out.append(_finding("title-order-prefix", "warn", repo, n,
                                f"#{n} title prefix '{m.group(1)}:' duplicates a native edge",
                                {"op": "retitle", "number": n, "title": clean}))
        if not _is_open(i):
            continue
        # Numbers, not (repo, number): a body header can't say which repo it
        # means, and a transferred issue's header points at its old home.
        native = {b["number"] for b in blockers}
        for ref in sorted(_text_blockers(i.get("body", "")) - native):
            if parent is not None and ref == parent["number"]:
                # Stale pre-#92 grammar: "blocked by my epic". The anchor
                # runs the other way (epic blocked-by child); never re-create
                # the inverted root — flag it for a body edit instead.
                out.append(_finding("text-only-blocker", "warn", repo, n,
                                    f"#{n} body says Blocked-by #{ref}, its own parent — stale header"))
                continue
            out.append(_finding("text-only-blocker", "error", repo, n,
                                f"#{n} body says Blocked-by #{ref} but no native edge exists",
                                {"op": "add_blocker", "number": n, "blocker": ref}))
        for b in blockers:
            if b["repo"] != repo:
                out.append(_finding("cross-repo-edge", "info", repo, n,
                                    f"#{n} blocked-by {b['repo']}#{b['number']} — `plan` in this repo cannot see it"))
        if parent is not None and parent["repo"] != repo:
            out.append(_finding("cross-repo-edge", "info", repo, n,
                                f"#{n} parent is {parent['repo']}#{parent['number']}"))
        open_blockers = [b for b in blockers if b["state"].upper() == "OPEN"]
        if runnable in _labels(i) and not open_blockers and not i.get("assignees"):
            updated = datetime.fromisoformat(i["updated_at"])
            if now - updated > timedelta(days=IDLE_DAYS):
                out.append(_finding("runnable-idle", "info", repo, n,
                                    f"#{n} runnable and unblocked, untouched for {(now - updated).days}d"))
    return out


_CHECKS = (_check_labels, _check_epics, _check_rungs, _check_edges)


def doctor(boards: list[dict], cfg: dict, now: datetime | None = None) -> dict:
    """Run every check over every board. ``ok`` is false on any error.
    ``now`` is injectable so the idle-age rule is testable."""
    now = now or datetime.now(UTC)
    findings = [f for board in boards for check in _CHECKS for f in check(board, cfg, now)]
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warn", "info")}
    return {"repos": [b["repo"] for b in boards], "ok": counts["error"] == 0,
            "counts": counts, "findings": findings}


def plan_sweep(report: dict) -> list[dict]:
    """The mechanical edits implied by a doctor report — every finding's op,
    deduped, in a stable order (label creation first so later ops can use them)."""
    order = {"create_label": 0, "add_label": 1, "remove_label": 1, "add_blocker": 2,
             "retitle": 3, "delete_label": 4}
    seen, ops = set(), []
    for f in report["findings"]:
        op = f.get("op")
        if op is None:
            continue
        key = tuple(sorted(op.items()))
        if key not in seen:
            seen.add(key)
            ops.append(op)
    ops.sort(key=lambda o: (order[o["op"]], o.get("number", 0)))
    return ops
