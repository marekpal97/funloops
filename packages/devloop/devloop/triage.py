"""Risk-lane classification of shipped PRs, pure over (signals, cfg).

``read_signals`` gathers the signal record from what the run already holds:
the gate results, the validated judge return, the baseline line and the git
diff. Fail-closed on the two required signals (``baseline_green``,
``review_severity``): a missing key or an off-enum value goes red. There is
no green lane; labels are applied by the orchestrator.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from devloop import paths

# LLM-assembled signals drift ("high", "partial"), so a value outside these
# sets fails closed to red rather than slipping through as a skim.
_VALID_REVIEW = {"none", "note", "problem"}
_RED_REVIEW = {"problem"}

# The red label is not here: it comes from labels.on_gate_failure so
# triage-red and gate-failure share one label.
TRIAGE_LABELS = {"yellow": "review-light"}

# A touched path in one of these forms (paths.match) is the coverage signal.
# ponytail: naming conventions only; a host with another layout reads as uncovered.
TEST_PATHS = ("tests/", "*/tests/*", "test_*", "*_test.*")


def read_signals(gate_results: list[dict], judge: object, baseline: str, fix_rounds: int,
                 root: Path, base: str) -> dict:
    """The signal record ``classify_pr`` takes: the diff gate's changed-line
    count, the judge's worst finding, the baseline line, the fix rounds, and
    the files ``base...HEAD`` touches in ``root``. An input that cannot yield
    its signal raises ``ValueError`` naming it."""
    counts = [g.get("changed_lines") for g in gate_results
              if isinstance(g, dict) and g.get("kind") == "diff"]
    if not counts or not isinstance(counts[0], int):
        raise ValueError("gates-json: no diff gate result with an int changed_lines")
    if not isinstance(judge, dict) or not isinstance(judge.get("findings"), list):
        raise ValueError("judge-json: expected the judge return object with a findings list")
    files = touched_files(root, base)
    return {
        "fix_rounds": fix_rounds,
        "diff_lines": counts[0],
        "files_touched": files,
        "tests_touched": any(paths.match(f, p) for f in files for p in TEST_PATHS),
        "review_severity": worst_severity(judge["findings"]),
        "baseline_green": baseline == "green",
    }


def classify_pr(signals: dict, cfg: dict, red_label: str | None = None) -> dict:
    """Classify one shipped PR into a risk lane: ``{lane, label, reasons}``.

    ``cfg`` is the resolved ``[triage]`` section; ``red_label`` is the red
    lane's tracker label. Red wins over yellow and ``reasons`` lists every
    triggered rule. The two required signals fail closed to red when absent
    or off-enum; the rest default benignly.

    Signals schema:
      - ``fix_rounds`` int — implement→gate→fix iterations (0 = first try) [opt, →0]
      - ``diff_lines`` int — total changed lines in the PR's diff [opt, →0]
      - ``files_touched`` list[str] — repo-relative paths changed [opt, →[]]
      - ``tests_touched`` bool — the change carries test coverage [opt, →False]
      - ``review_severity`` str — worst judge finding: none|note|problem [REQUIRED]
      - ``baseline_green`` bool — tests gate green on the pristine worktree [REQUIRED]
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
    if diff_lines >= cfg["red_diff_lines"]:
        red.append(f"large diff: {diff_lines} lines >= {cfg['red_diff_lines']}")

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


def worst_severity(findings: list) -> str:
    """``problem`` over ``note`` over ``none``; a severity off the enum is
    returned as found, so ``classify_pr`` fails it closed."""
    found = [f.get("severity") if isinstance(f, dict) else f for f in findings]
    odd = [s for s in found if s not in _VALID_REVIEW]
    if odd:
        return str(odd[0])
    return next((s for s in ("problem", "note") if s in found), "none")


def touched_files(root: Path, base: str) -> list[str]:
    """The repo-relative paths ``git diff base...HEAD`` names in ``root``."""
    proc = subprocess.run(["git", "diff", "--name-only", "-z", f"{base}...HEAD"],
                          cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ValueError(f"git diff {base}...HEAD: {proc.stderr.strip()}")
    return sorted(filter(None, proc.stdout.split("\0")))
