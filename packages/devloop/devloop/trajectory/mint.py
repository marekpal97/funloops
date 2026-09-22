"""The trajectory note's write face: payload assembly for the memory feed.

Internal to this file: the trace normalizers and the skill projection.
"""

from __future__ import annotations

TRANSPORTS = ("agent-tool", "herdr", "headless-argv")
TIERS = ("base", "small")
DISPATCH_KEYS: dict[str, type] = {
    "transport": str, "harness": str, "model": str, "effort": str,
    "session_ref": str, "duration_sec": int, "tokens": int, "tier": str,
}
DISPATCH_CHOICES = {"transport": TRANSPORTS, "tier": TIERS}


def _normalize_skill(entry: dict, where: str, reasons: list[str]) -> dict:
    """Project one stage-dispatch record to ``{id, role, outcome,
    fix_rounds_attributed}`` plus whichever dispatch join keys it carries,
    verbatim; extra keys are dropped. A wrong-typed join key appends a
    ``<where>.<key>: …`` reason instead of landing in the record."""
    out = {
        "id": entry.get("id", ""),
        "role": entry.get("role", ""),
        "outcome": entry.get("outcome", ""),
        "fix_rounds_attributed": int(entry.get("fix_rounds_attributed", 0) or 0),
    }
    for key, kind in DISPATCH_KEYS.items():
        if key not in entry:
            continue
        value = entry[key]
        if not isinstance(value, kind) or isinstance(value, bool):
            reasons.append(f"{where}.{key}: expected {kind.__name__}, got {value!r}")
        elif key in DISPATCH_CHOICES and value not in DISPATCH_CHOICES[key]:
            reasons.append(f"{where}.{key}: {value!r} is not one of "
                           f"{' | '.join(DISPATCH_CHOICES[key])}")
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Semantic execution trace


def _as_int_or_none(value: object) -> int | None:
    """Coerce a nullable count: an int stays, a bool or a non-int-like value
    becomes ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_trace_round(entry: dict) -> dict:
    """Project one review round to ``{gate, finding, severity, disposition,
    fixed_by}``."""
    return {
        "gate": str(entry.get("gate", "") or ""),
        "finding": str(entry.get("finding", "") or ""),
        "severity": str(entry.get("severity", "") or ""),
        "disposition": str(entry.get("disposition", "") or ""),
        "fixed_by": str(entry.get("fixed_by", "") or ""),
    }


def _normalize_trace_criterion(entry: dict) -> dict:
    """Project one acceptance criterion to ``{id, verdict, flipped_by_round}``."""
    return {
        "id": str(entry.get("id", "") or ""),
        "verdict": str(entry.get("verdict", "") or ""),
        "flipped_by_round": _as_int_or_none(entry.get("flipped_by_round")),
    }


def _normalize_trace_whatwhy(entry: dict) -> dict:
    """Project one simplify cut or keep to ``{what, why}``."""
    return {
        "what": str(entry.get("what", "") or ""),
        "why": str(entry.get("why", "") or ""),
    }


def _normalize_trace_simplify(section: dict) -> dict:
    """Project one simplify envelope to ``{outcome, cuts, kept, lines_delta}``;
    a malformed ``lines_delta`` degrades to 0."""
    cuts = section.get("cuts")
    kept = section.get("kept")
    return {
        "outcome": str(section.get("outcome", "") or ""),
        "cuts": [_normalize_trace_whatwhy(c) for c in cuts if isinstance(c, dict)]
                if isinstance(cuts, list) else [],
        "kept": [_normalize_trace_whatwhy(c) for c in kept if isinstance(c, dict)]
                if isinstance(kept, list) else [],
        "lines_delta": _as_int_or_none(section.get("lines_delta")) or 0,
    }


def _normalize_trace(raw: object) -> dict:
    """Shape a trace object into its stored envelope. A non-dict raises;
    unknown keys are dropped and each section is projected to its known
    fields. A backstop only: gate returns are enforced at the rail's
    ``validate`` verb."""
    if not isinstance(raw, dict):
        raise ValueError("trace must be a JSON object")
    out: dict = {}
    rounds = raw.get("rounds")
    if isinstance(rounds, list):
        out["rounds"] = [_normalize_trace_round(e) for e in rounds if isinstance(e, dict)]
    criteria = raw.get("criteria")
    if isinstance(criteria, list):
        out["criteria"] = [_normalize_trace_criterion(e) for e in criteria if isinstance(e, dict)]
    for key in ("simplify", "stack_simplify"):
        section = raw.get(key)
        if isinstance(section, dict):
            out[key] = _normalize_trace_simplify(section)
    edge_cases = raw.get("edge_cases")
    if isinstance(edge_cases, list):
        out["edge_cases"] = [str(x) for x in edge_cases if isinstance(x, str)]
    tdd = raw.get("tdd")
    if isinstance(tdd, dict):
        out["tdd"] = {"red_confirmed": bool(tdd.get("red_confirmed", False))}
    return out


def build_trajectory(issue: dict, *, branch: str, commits: list[str],
                     numstat: str, gates: list[dict], fix_rounds: int,
                     outcome: str, pr_url: str = "", run_id: str = "",
                     skills: list[dict] | None = None,
                     skill_centric: bool = False,
                     primed: bool | None = None,
                     served: list[str] | None = None,
                     trace: dict | None = None) -> dict:
    """Assemble the deterministic half of a per-issue trajectory note as a
    weave_create-shaped payload: the mechanical facts in frontmatter, the body
    a skeleton the orchestrator fills.

    ``skills`` is the stage-dispatch log, ``[{id, role, outcome,
    fix_rounds_attributed}]``, each entry optionally carrying the dispatch
    join keys in ``DISPATCH_KEYS``; a wrong-typed key raises ``ValueError``
    with one field-path reason per arg. ``skill_centric`` adds the
    ``skill-invocation`` tag. ``primed``/``served`` mirror the claim-time
    prime verdict; ``primed=None`` omits both keys. ``trace`` is stored under one ``trace``
    key; ``trace=None`` omits it.
    """
    files = [line.split("\t")[2] for line in numstat.strip().splitlines()
             if len(line.split("\t")) == 3]
    reasons: list[str] = []
    stages = [_normalize_skill(s, f"skills[{i}]", reasons) for i, s in enumerate(skills or [])]
    if reasons:
        raise ValueError(*reasons)
    tags = ["loop-run"] + (["skill-invocation"] if skill_centric else [])
    frontmatter = {
        "issue": issue["number"],
        "issue_url": issue.get("html_url", ""),
        "pr_url": pr_url,
        "run_id": run_id,
        "branch": branch,
        "outcome": outcome,
        "fix_rounds": fix_rounds,
        "commits": len(commits),
        "files_touched": sorted(set(files)),
        "gates": [{"id": g["id"], "passed": g["passed"], "summary": g.get("summary", "")}
                  for g in gates],
        "skills": stages,
    }
    if primed is not None:
        if served is not None and (
            not isinstance(served, list)
            or not all(isinstance(s, str) for s in served)
        ):
            raise ValueError("served must be a list of note-id strings")
        frontmatter["primed"] = primed
        frontmatter["served"] = list(served or [])
    if trace is not None:
        frontmatter["trace"] = _normalize_trace(trace)
    return {
        "type": "note",
        "title": f"loop trajectory #{issue['number']}: {issue.get('title', '')[:80]}",
        "tags": tags,
        "frontmatter": frontmatter,
        "body_skeleton": (
            "## What\n<1-2 sentences: the slice delivered>\n\n"
            "## How it went\n<fix rounds and why; seams chosen; surprises>"
        ),
        "concept_hints": [l["name"] if isinstance(l, dict) else l
                          for l in issue.get("labels", [])],
    }
