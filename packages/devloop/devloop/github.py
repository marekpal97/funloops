"""The one subprocess seam to ``gh``, plus the issue-snapshot dicts it makes.

This module owns the snapshot shape ``{number, title, state, labels,
assignees, body, native_blocked_count, native_blockers?}`` — the
``github``↔``dag`` contract. ``dag`` never sees raw gh output.
Tracker *mutations* (claim/release) stay in ``cli``, composed from ``run``:
the assign-vs-label convention is loop policy, not gh plumbing.
"""

from __future__ import annotations

import json
import subprocess


def run(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def fetch_issues() -> list[dict]:
    """Snapshot all issues with native-dependency enrichment.

    Uses the REST issues endpoint (not `gh issue list --json`) because it
    carries ``issue_dependencies_summary`` — GitHub's own count of OPEN
    blockers, maintained natively since /to-tickets and /wayfinder publish
    blocking as issue dependencies. For open issues with a nonzero count,
    the actual blocker numbers are fetched (one extra call each) so plans
    can name them and components can include the edges.
    """
    # --jq '.[]' flattens each page to NDJSON — works on gh versions
    # predating --slurp, and never confuses body text for page boundaries.
    out = run(["api", "--paginate", "--jq", ".[]",
               "repos/{owner}/{repo}/issues?state=all&per_page=100"])
    issues = []
    for line in out.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "pull_request" in item:
            continue
        issue = {
            "number": item["number"],
            "title": item.get("title", ""),
            "state": item["state"],
            "labels": item.get("labels", []),
            "assignees": item.get("assignees", []),
            "body": item.get("body") or "",
            "native_blocked_count": (item.get("issue_dependencies_summary") or {}).get("blocked_by", 0),
        }
        if issue["state"].upper() == "OPEN" and issue["native_blocked_count"] > 0:
            try:
                refs = run(["api", f"repos/{{owner}}/{{repo}}/issues/{issue['number']}/dependencies/blocked_by",
                            "--jq", "[.[].number]"])
                issue["native_blockers"] = json.loads(refs)
            except subprocess.CalledProcessError:
                issue["native_blockers"] = []  # count still gates; list is enrichment
        issues.append(issue)
    return issues


def fetch_labels(number: int) -> list[str]:
    """Issue label names via gh (network). Empty list on any failure — a prime
    with no concepts serves an empty block, never crashes the loop."""
    try:
        out = run(["issue", "view", str(number), "--json", "labels",
                   "--jq", "[.labels[].name]"])
        return json.loads(out or "[]")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# Board snapshot — the richer shape `devloop.board` checks


def _repo_of(item: dict) -> str:
    """``owner/name`` from a REST issue's ``repository_url``."""
    return "/".join(item.get("repository_url", "").rsplit("/", 2)[-2:])


def _ref(item: dict) -> dict:
    return {"repo": _repo_of(item), "number": item["number"], "state": item["state"]}


def _refs(endpoint: str) -> list[dict]:
    try:
        out = run(["api", "--paginate", "--jq", ".[]", endpoint])
    except subprocess.CalledProcessError:
        return []
    return [_ref(json.loads(l)) for l in out.splitlines() if l.strip()]


def fetch_repo_labels(repo: str) -> list[str]:
    out = run(["api", "--paginate", "--jq", ".[].name", f"repos/{repo}/labels?per_page=100"])
    return [l for l in out.splitlines() if l]


def fetch_board(repo: str) -> dict:
    """Everything the board checks need, for one repo: the label set plus
    every issue with its native relationships resolved to ``{repo, number,
    state}`` refs (blockers, parent, children). Open issues only get the
    per-issue calls; closed ones are kept as bare rows so closure checks see
    them. Cross-repo refs survive — GitHub allows them, ``plan`` can't see
    them, and the doctor says so."""
    out = run(["api", "--paginate", "--jq", ".[]",
               f"repos/{repo}/issues?state=all&per_page=100"])
    issues = []
    for line in out.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "pull_request" in item:
            continue
        n = item["number"]
        issue = {
            "number": n,
            "title": item.get("title", ""),
            "state": item["state"],
            "labels": item.get("labels", []),
            "assignees": item.get("assignees", []),
            "body": item.get("body") or "",
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "sub_issues": item.get("sub_issues_summary") or {},
            "blockers": [], "children": [], "parent": None,
        }
        if issue["state"].upper() == "OPEN":
            deps = item.get("issue_dependencies_summary") or {}
            if deps.get("total_blocked_by", 0):
                issue["blockers"] = _refs(f"repos/{repo}/issues/{n}/dependencies/blocked_by")
            if issue["sub_issues"].get("total", 0):
                issue["children"] = _refs(f"repos/{repo}/issues/{n}/sub_issues")
            try:
                issue["parent"] = _ref(json.loads(run(["api", f"repos/{repo}/issues/{n}/parent"])))
            except subprocess.CalledProcessError:
                issue["parent"] = None  # 404: no parent
        issues.append(issue)
    return {"repo": repo, "labels": fetch_repo_labels(repo), "issues": issues}


def apply_op(op: dict) -> None:
    """Replay one sweep op through ``gh``. The op vocabulary is
    ``devloop.board``'s; this is the only place it meets the network."""
    repo, kind = op["repo"], op["op"]
    if kind == "create_label":
        run(["label", "create", op["name"], "-R", repo, "--force",
             "--description", op["description"], "--color", op["color"]])
    elif kind == "delete_label":
        run(["label", "delete", op["name"], "-R", repo, "--yes"])
    elif kind == "add_label":
        run(["issue", "edit", str(op["number"]), "-R", repo, "--add-label", op["name"]])
    elif kind == "remove_label":
        run(["issue", "edit", str(op["number"]), "-R", repo, "--remove-label", op["name"]])
    elif kind == "retitle":
        run(["issue", "edit", str(op["number"]), "-R", repo, "--title", op["title"]])
    elif kind == "add_blocker":
        blocker_id = run(["api", f"repos/{repo}/issues/{op['blocker']}", "--jq", ".id"]).strip()
        run(["api", "-X", "POST", f"repos/{repo}/issues/{op['number']}/dependencies/blocked_by",
             "-F", f"issue_id={blocker_id}"])
    else:
        raise ValueError(f"unknown sweep op '{kind}'")
