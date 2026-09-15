"""The trajectory note's read face: claim-time prior-trajectory context.

Prime never crashes the loop: any index problem degrades to ``primed=false``
with a ``note``. Prime writes nothing to the index; its only side effect is
the served-event append to the session buffer. All SQL lives in
``devloop.index_client``.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from devloop.index_client import Connection, Error, note_rows, trajectory_candidates


def _coerce_builds_on(raw: object) -> list[str]:
    """Normalize a ``builds_on`` value to a list of note ids, taking the
    trailing id of a ``[[path|id]]`` wikilink; a bad element is dropped."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s.startswith("[[") and s.endswith("]]"):
            s = s[2:-2]
        if "|" in s:
            s = s.split("|")[-1]
        s = s.strip()
        if s:
            out.append(s)
    return out


def resolve_insights(conn: Connection, ids: list[str]) -> list[dict]:
    """``[{id, body}]`` of the insight notes in ``ids`` order, skipping ids
    that do not resolve or have an empty body."""
    by_id = note_rows(conn, ids)
    return [{"id": i, "body": by_id[i]["body"]} for i in ids if by_id.get(i, {}).get("body")]


def resolve_decisions(conn: Connection, ids: list[str]) -> list[dict]:
    """``[{id, title, summary}]`` in ``ids`` order; ``summary`` is the first
    prose line of the decision body. An id the index does not hold keeps its
    place with empty fields, so the renderer can say so."""
    by_id = note_rows(conn, ids, "decision")
    return [{"id": i, "title": by_id.get(i, {}).get("title", ""),
             "summary": _first_prose_line(by_id.get(i, {}).get("body", ""))}
            for i in ids]


def _first_prose_line(body: str) -> str:
    return next((ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")), "")


# Unlabeled and unknown outcomes stay at rank 1, so an all-unlabeled match
# set keeps the fused order.
_OUTCOME_RANK = {
    "merged-clean": 0, "stable": 0,
    "reworked": 2, "reworked-post-merge": 2,
    "closed-unmerged": 2, "reverted": 2, "routed-to-human": 2,
}


def _outcome_rank(label: object) -> int:
    return _OUTCOME_RANK.get(str(label or ""), 1)


def query_trajectories(
    conn: Connection, concepts: list[str], limit: int, scan_cap: int = 40,
    query: str = "",
) -> list[dict]:
    """``[{id, title, issue, outcome, outcome_label, insights}]`` for the
    ``[loop-run]`` notes matching ``concepts`` or ``query`` whose ``builds_on``
    links resolve to insight bodies, sorted by outcome rank (stable), at most
    ``limit``."""
    out: list[dict] = []
    for r in trajectory_candidates(conn, concepts, query, scan_cap):
        try:
            fm = json.loads(r["frontmatter"] or "{}")
        except json.JSONDecodeError:
            fm = {}
        insights = resolve_insights(conn, _coerce_builds_on(fm.get("builds_on")))
        if not insights:
            continue
        out.append({
            "id": r["id"],
            "title": r["title"] or "",
            "issue": fm.get("issue"),
            "outcome": fm.get("outcome", ""),
            "outcome_label": fm.get("outcome_label", ""),
            "insights": insights,
        })
    out.sort(key=lambda t: _outcome_rank(t.get("outcome_label")))
    return out[:limit]


def render_prime_block(
    trajectories: list[dict], decisions: list[dict] | None = None,
    budget_chars: int = 1200,
) -> tuple[str, list[str]]:
    """Render the prime block and the list of ids it served. Each trajectory's
    insight bodies land as a whole piece until ``budget_chars`` is spent; at
    least one lands if any exist. ``decisions`` close the block outside the
    budget. Empty input gives ``('', [])``."""
    decisions = decisions or []
    if not trajectories and not decisions:
        return "", []
    pieces = ["## Prior trajectories — reusable lessons from similar prior runs\n"]
    served: list[str] = []
    for t in trajectories:
        insights = t.get("insights") or []
        head = f"### #{t.get('issue')} — {t.get('title', '')} ({t.get('outcome', '')})".rstrip()
        piece = f"{head}\n" + "\n".join(ins["body"] for ins in insights) + "\n"
        if served and sum(len(x) for x in pieces) + len(piece) > budget_chars:
            break
        pieces.append(piece)
        served.extend(ins["id"] for ins in insights)
    if decisions:
        pieces.append("### Prior decisions\n" + "".join(_decision_bullet(d) for d in decisions))
        served.extend(d["id"] for d in decisions)
    return "\n".join(pieces).strip() + "\n", served


def _decision_bullet(d: dict) -> str:
    line = f"- **{d['id']}** — {d.get('title') or '(not in the index)'}\n"
    return line + (f"  {d['summary']}\n" if d.get("summary") else "")


def build_prime_payload(
    issue_number: int, run_id: str, concepts: list[str], *,
    conn: Connection | None = None,
    limit: int = 3, budget_chars: int = 1200, decisions: list[str] | None = None,
    query: str = "",
) -> dict:
    """Assemble the claim-time prime payload: ``primed``, ``served`` (note ids,
    capped ``limit`` per kind), ``block`` (markdown, ``''`` when unprimed) and
    ``note`` (why unprimed). ``concepts`` and ``query`` are the two retrieval
    legs; ``decisions`` are decision ids resolved to title and summary. Prime
    always serves what it finds."""
    payload = {
        "issue": issue_number, "run_id": run_id, "concepts": list(concepts),
        "query": query, "primed": False, "served": [], "block": "", "note": "",
    }
    # A corrupt file or an older schema raises at the query, not the connect,
    # so the guard sits here: a bad index degrades to unprimed.
    index_error = False
    trajectories: list[dict] = []
    ids = list(dict.fromkeys(decisions or []))[:limit]
    resolved = [{"id": i, "title": "", "summary": ""} for i in ids]
    if conn is not None:
        try:
            trajectories = query_trajectories(conn, concepts, limit, query=query)
            resolved = resolve_decisions(conn, ids)
        except Error:
            index_error = True
    block, served = render_prime_block(trajectories, resolved, budget_chars)
    payload["block"] = block
    payload["served"] = served
    payload["primed"] = bool(served)
    if not served:
        payload["note"] = (
            "index unreadable (corrupt or schema-drift) — ran unprimed"
            if index_error else "no matching prior trajectories"
        )
    return payload


# The indexer keys off this sentinel to assign source='loop-prime'.
LOOP_PRIME_TOOL = "loop_prime"


def append_served_event(
    buffer_path: str, run_id: str, issue_number: int,
    served: list[str], session_id: str = "",
) -> None:
    """Append one ``retrieval`` event tagged ``loop_prime`` to the session
    buffer JSONL."""
    event = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "type": "retrieval",
        "tool": LOOP_PRIME_TOOL,
        "args": {"run_id": run_id, "issue": issue_number, "session_id": session_id},
        "returned_ids": served,
    }
    p = Path(buffer_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
