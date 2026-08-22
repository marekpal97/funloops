"""Board doctor: pure checks over a board snapshot, and the sweep plan.

No gh, no network — boards are plain dicts in the ``github.fetch_board`` shape.
"""

from datetime import UTC, datetime

from devloop import board, cli

CFG = {"labels": {"runnable": "ready-for-agent", "claimed": "agent-claimed",
                  "on_gate_failure": "ready-for-human"}}
REPO = "o/r"
NOW = datetime(2026, 8, 22, tzinfo=UTC)
FULL_LABELS = [*board.REQUIRED_LABELS, "track:A"]


def _issue(number, title="Issue", labels=("ready-for-agent", "track:A"), state="OPEN",
           body="", blockers=(), children=(), parent=None, sub=None,
           updated="2026-08-21T00:00:00Z", assignees=()):
    return {
        "number": number, "title": title, "state": state,
        "labels": [{"name": l} for l in labels], "assignees": list(assignees),
        "body": body, "updated_at": updated, "created_at": updated,
        "sub_issues": sub or {"total": len(children), "completed":
                              sum(c["state"] == "CLOSED" for c in children)},
        "blockers": list(blockers), "children": list(children), "parent": parent,
    }


def _ref(number, state="OPEN", repo=REPO):
    return {"repo": repo, "number": number, "state": state}


def _board(*issues, labels=FULL_LABELS):
    return {"repo": REPO, "labels": list(labels), "issues": list(issues)}


def _checks(report, check):
    return [f for f in report["findings"] if f["check"] == check]


# ---------------------------------------------------------------------------
# a clean board is clean


def test_clean_board_has_no_findings():
    epic = _issue(1, "Epic", labels=("epic", "track:A"),
                  children=[_ref(2)], blockers=[_ref(2)])
    child = _issue(2, "Do the thing", parent=_ref(1))
    report = board.doctor([_board(epic, child)], CFG, now=NOW)
    assert report["ok"] and report["findings"] == []


# ---------------------------------------------------------------------------
# labels


def test_missing_and_boilerplate_labels():
    report = board.doctor([_board(labels=["bug", "help wanted"])], CFG)
    missing = {f["op"]["name"] for f in _checks(report, "label-missing")}
    assert "epic" in missing and "ready-for-agent" in missing and "agent-claimed" in missing
    assert [f["op"] for f in _checks(report, "label-boilerplate")] == [
        {"repo": REPO, "op": "delete_label", "name": "help wanted"}]
    assert not report["ok"]


def test_boilerplate_label_in_use_is_kept():
    b = _board(_issue(1, labels=("question", "ready-for-agent")),
               labels=[*FULL_LABELS, "question"])
    assert _checks(board.doctor([b], CFG, now=NOW), "label-boilerplate") == []


# ---------------------------------------------------------------------------
# epics


def test_epic_grammar():
    epic = _issue(1, "Epic", labels=("ready-for-agent", "track:A"),
                  children=[_ref(2), _ref(3, "CLOSED")], blockers=[])
    report = board.doctor([_board(epic, _issue(2, parent=_ref(1)))], CFG, now=NOW)
    assert _checks(report, "epic-unlabelled")[0]["op"] == {
        "repo": REPO, "op": "add_label", "number": 1, "name": "epic"}
    assert _checks(report, "epic-runnable")[0]["op"]["op"] == "remove_label"
    anchors = _checks(report, "epic-unanchored")
    assert [f["op"]["blocker"] for f in anchors] == [2]   # closed child needs no anchor
    assert _checks(report, "epic-delivered") == []


def test_epic_with_all_children_closed_is_flagged_without_op():
    epic = _issue(1, "Epic", labels=("epic", "track:A"), children=[_ref(2, "CLOSED")])
    f = _checks(board.doctor([_board(epic)], CFG, now=NOW), "epic-delivered")
    assert len(f) == 1 and "op" not in f[0]


def test_epic_label_alone_makes_an_epic_exempt_from_rung_check():
    epic = _issue(1, "Epic", labels=("epic", "track:A"))
    assert _checks(board.doctor([_board(epic)], CFG, now=NOW), "rung-missing") == []


# ---------------------------------------------------------------------------
# rungs and tracks — judgment calls, never ops


def test_rung_findings_only_op_is_needs_triage():
    b = _board(_issue(1, labels=("ready-for-agent", "ready-for-human", "track:A")),
               _issue(2, labels=("enhancement",)))
    report = board.doctor([b], CFG, now=NOW)
    contra = _checks(report, "rung-contradictory")
    assert len(contra) == 1 and contra[0]["severity"] == "error" and "op" not in contra[0]
    missing = _checks(report, "rung-missing")
    assert [f["number"] for f in missing] == [2]
    assert missing[0]["op"] == {"repo": REPO, "op": "add_label", "number": 2, "name": "needs-triage"}
    tracks = _checks(report, "track-missing")
    assert [f["number"] for f in tracks] == [2] and "op" not in tracks[0]


def test_track_check_only_where_the_repo_uses_tracks():
    b = _board(_issue(1, labels=("ready-for-agent",)), labels=list(board.REQUIRED_LABELS))
    assert _checks(board.doctor([b], CFG, now=NOW), "track-missing") == []


# ---------------------------------------------------------------------------
# edges


def test_title_prefix_is_stripped_only_when_a_native_edge_exists():
    with_edge = _issue(1, "W5a: prime v4 — semantic leg", blockers=[_ref(9, "CLOSED")])
    bare = _issue(2, "[SPIKE]: no edges anywhere")
    plain = _issue(3, "PRD: this is not an order prefix", blockers=[_ref(9, "CLOSED")])
    report = board.doctor([_board(with_edge, bare, plain)], CFG, now=NOW)
    f = _checks(report, "title-order-prefix")
    assert [x["op"] for x in f] == [
        {"repo": REPO, "op": "retitle", "number": 1, "title": "prime v4 — semantic leg"}]


def test_text_only_blocker_becomes_a_native_edge_unless_it_is_the_parent():
    child = _issue(1, body="Wave: 1 | Blocked-by: #5, #7 | Parallel-safe: no",
                   blockers=[_ref(7, "CLOSED", repo="other/repo")], parent=_ref(5))
    report = board.doctor([_board(child)], CFG, now=NOW)
    f = _checks(report, "text-only-blocker")
    # #7 exists natively (cross-repo still counts); #5 is the parent → warn, no op
    assert len(f) == 1 and f[0]["severity"] == "warn" and "op" not in f[0]
    sibling = _issue(2, body="Blocked-by: #1")
    f = _checks(board.doctor([_board(sibling)], CFG, now=NOW), "text-only-blocker")
    assert f[0]["op"] == {"repo": REPO, "op": "add_blocker", "number": 2, "blocker": 1}


def test_cross_repo_edges_are_info_and_idle_runnables_are_info():
    i = _issue(1, blockers=[_ref(3, "CLOSED", repo="other/repo")],
               parent=_ref(4, repo="other/repo"), updated="2026-07-01T00:00:00Z")
    report = board.doctor([_board(i)], CFG, now=NOW)
    assert len(_checks(report, "cross-repo-edge")) == 2
    assert len(_checks(report, "runnable-idle")) == 1
    assert report["ok"] and report["counts"] == {"error": 0, "warn": 0, "info": 3}


def test_assigned_or_blocked_runnable_is_not_idle():
    assigned = _issue(1, updated="2026-07-01T00:00:00Z", assignees=[{"login": "x"}])
    blocked = _issue(2, updated="2026-07-01T00:00:00Z", blockers=[_ref(1)])
    assert _checks(board.doctor([_board(assigned, blocked)], CFG, now=NOW), "runnable-idle") == []


# ---------------------------------------------------------------------------
# sweep plan


def test_plan_sweep_dedupes_and_orders_label_creation_first():
    report = {"findings": [
        {"op": {"repo": REPO, "op": "retitle", "number": 2, "title": "t"}},
        {"op": {"repo": REPO, "op": "add_label", "number": 1, "name": "epic"}},
        {"op": {"repo": REPO, "op": "add_label", "number": 1, "name": "epic"}},
        {"op": {"repo": REPO, "op": "create_label", "name": "epic", "description": "d", "color": "c"}},
        {"check": "rung-missing"},
    ]}
    assert [o["op"] for o in board.plan_sweep(report)] == ["create_label", "add_label", "retitle"]


def test_board_cli_contract():
    parser = cli.build_arg_parser()
    args = parser.parse_args(["board", "sweep", "--repo", "o/a", "--repo", "o/b",
                              "--only", "add_label,retitle"])
    assert args.verb == "sweep" and args.repo == ["o/a", "o/b"] and not args.apply
    assert parser.parse_args(["board", "doctor"]).repo == []
