"""Risk-lane classification of shipped PRs, pure over (signals, cfg).

Fail-closed on the three required signals (``baseline_green``,
``acceptance``, ``review_severity``): a missing key or an off-enum value
goes red. There is no green lane; labels are applied by the orchestrator.
"""

from __future__ import annotations

from devloop import paths

# LLM-assembled signals drift ("high", "partial"), so a value outside these
# sets fails closed to red rather than slipping through as a skim.
_VALID_REVIEW = {"none", "note", "problem"}
_RED_REVIEW = {"problem"}
_VALID_ACCEPTANCE = {"met", "uncertain", "not-met"}
_RED_ACCEPTANCE = {"uncertain", "not-met"}

# The red label is not here: it comes from labels.on_gate_failure so
# triage-red and gate-failure share one label.
TRIAGE_LABELS = {"yellow": "review-light"}


def classify_pr(signals: dict, cfg: dict, red_label: str | None = None) -> dict:
    """Classify one shipped PR into a risk lane: ``{lane, label, reasons}``.

    ``cfg`` is the resolved ``[triage]`` section; ``red_label`` is the red
    lane's tracker label. Red wins over yellow and ``reasons`` lists every
    triggered rule. The three required signals fail closed to red when absent
    or off-enum; the rest default benignly.

    Signals schema:
      - ``fix_rounds`` int — implement→gate→fix iterations (0 = first try) [opt, →0]
      - ``diff_lines`` int — total changed lines in the PR's diff [opt, →0]
      - ``files_touched`` list[str] — repo-relative paths changed [opt, →[]]
      - ``tests_touched`` bool — the change carries test coverage [opt, →False]
      - ``review_severity`` str — worst judge finding: none|note|problem [REQUIRED]
      - ``baseline_green`` bool — tests gate green on the pristine worktree [REQUIRED]
      - ``acceptance`` str — judge criteria verdict: met|uncertain|not-met [REQUIRED]
    """
    if red_label is None:
        # Imported lazily: cli imports this module, so a top-level import is a cycle.
        from devloop.cli import DEFAULT_CONFIG

        red_label = DEFAULT_CONFIG["labels"]["on_gate_failure"]
    fix_rounds = int(signals.get("fix_rounds", 0) or 0)
    diff_lines = int(signals.get("diff_lines", 0) or 0)
    files = signals.get("files_touched") or []
    tests_touched = bool(signals.get("tests_touched", False))

    red: list[str] = []
    sensitive = paths.hits(files, cfg.get("sensitive_paths", []))
    if sensitive:
        red.append("sensitive path(s): " + ", ".join(sensitive))
    if diff_lines >= cfg["red_min_diff_lines"]:
        red.append(f"large diff: {diff_lines} lines >= {cfg['red_min_diff_lines']}")

    # A truthy "false" string must not pass, so the type is checked too.
    if "baseline_green" not in signals:
        red.append("baseline_green signal missing (fail-closed)")
    elif not isinstance(signals["baseline_green"], bool):
        red.append(f"non-bool baseline_green {signals['baseline_green']!r} (fail-closed)")
    elif not signals["baseline_green"]:
        red.append("degraded baseline (tests not green on the pristine worktree)")

    if "review_severity" not in signals:
        red.append("review_severity signal missing (fail-closed)")
    else:
        review = str(signals["review_severity"]).lower()
        if review not in _VALID_REVIEW:
            red.append(f"unrecognized review_severity '{signals['review_severity']}' (fail-closed)")
        elif review in _RED_REVIEW:
            red.append(f"review severity {review}")

    if "acceptance" not in signals:
        red.append("acceptance signal missing (fail-closed)")
    else:
        acceptance = str(signals["acceptance"]).lower()
        if acceptance not in _VALID_ACCEPTANCE:
            red.append(f"unrecognized acceptance '{signals['acceptance']}' (fail-closed)")
        elif acceptance in _RED_ACCEPTANCE:
            red.append(f"acceptance {acceptance}")

    if red:
        return {"lane": "red", "label": red_label, "reasons": red}

    yellow: list[str] = []
    if fix_rounds > 0:
        yellow.append(f"{fix_rounds} fix round(s)")
    watched = paths.hits(files, cfg.get("watched_paths", []))
    if watched:
        yellow.append("watched path(s): " + ", ".join(watched))
    if not tests_touched:
        yellow.append("no test coverage signal (tests_touched=false)")
    return {"lane": "yellow", "label": TRIAGE_LABELS["yellow"], "reasons": yellow}
