"""Deterministic rail for the /issue-loop dev workflow.

The issue tracker is the DAG: blocking edges live as GitHub-native issue
dependencies and nowhere else. This script stores no state; it re-reads
the tracker and computes the current frontier and its components. LLM
judgment stays in the /issue-loop command; everything schedulable is
graph math here.

Subcommands:
  plan     — snapshot issues via `gh`, compute frontier + components (JSON)
  claim    — claim an issue for a run (the assignee IS the claim)
  release  — drop the claim
  config     — print resolved loop config (defaults merged with loop.toml;
               an unknown or deleted key is refused by name)
  check      — run one deterministic gate (kind: command | diff) and emit JSON;
               --issue N runs the command gates, then the issue's `verify:`
               lines, as one result list
  validate   — validate a judgment gate's subagent return (kind: judge)
               against its schema; rejects for a re-ask
  prime      — assemble prior-trajectory prime context for an issue at claim
               time (reads the derived index read-only; always serves what
               it finds)
  trajectory — assemble a per-issue trajectory payload for the memory feed
               (the optional host extension; shape: devloop-boundaries.md §4)
  board      — doctor: lint one or more repos' boards against the grammar
               dag.py reads (JSON report, exit 1 on errors); sweep: replay
               the mechanical fixes (--plan by default, --apply runs them)
  pack       — compose one dispatch's context (issue, the constitution as
               rules, the persona for the implementer, the repo map spliced
               from codegraph's CLI) and print it

Stdlib only. Config: the host repo's docs/agents/loop.toml, found by walking up
from the cwd, else the copy shipped with the package (see find_config).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import get_args

from devloop import board, dag, github, index_client, pack, trajectory, triage

# Imported by name: `main` binds a local `gates` in the trajectory branch,
# which would shadow a module of that name for the whole function.
from devloop.gates import (
    COMMON_GATE_KEYS,
    DETERMINISTIC,
    GATE_KEYS,
    JUDGMENT,
    reject,
    run_command_gate,
    validate,
    verify_gates,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_REL = Path("docs") / "agents" / "loop.toml"
CONSTITUTION_REL = Path("docs") / "agents" / "constitution.md"


def _package_docs() -> Path:
    """The rail's own docs/agents: inside the package when installed from a
    wheel, beside it in the source tree."""
    inside = Path(__file__).resolve().parent / "docs" / "agents"
    return inside if inside.is_dir() else REPO_ROOT / "docs" / "agents"


PACKAGE_DOCS = _package_docs()
PACKAGE_CONFIG = PACKAGE_DOCS / "loop.toml"
PACKAGE_CONSTITUTION = PACKAGE_DOCS / "constitution.md"
PACKAGE_PERSONA = PACKAGE_DOCS / "ponytail-persona.md"


def _walk_up(rel: Path, start: Path | None = None):
    """Every copy of ``rel`` on the way up from the working directory, nearest
    first. Stops at the first ``.git`` so an ancestor repo's file is never
    inherited."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        found = directory / rel
        if found.is_file():
            yield found
        if (directory / ".git").exists():
            break


def _is_packaged(constitution: Path) -> bool:
    """Whether this constitution is the packaged one: it sits beside or
    inside a ``devloop`` package. Place, not bytes, so a diverged copy in a
    worktree is still the default and never an overlay."""
    home = constitution.parents[2]
    return (home / "devloop" / "__init__.py").is_file() or (
        home.name == "devloop" and (home / "__init__.py").is_file())


def find_config(start: Path | None = None) -> Path:
    """The host repo's ``docs/agents/loop.toml``, else the package's own copy.
    The host's file replaces the packaged one; ``load_config`` treats a
    missing file as defaults only."""
    return next(_walk_up(CONFIG_REL, start), PACKAGE_CONFIG)


def find_constitution(start: Path | None = None) -> list[Path]:
    """The packaged constitution, then the host repo's overlay when it has one.

    The overlay extends the default and never replaces it. A walk that starts
    below the packaged file serves it once and goes on to the repo's overlay.
    A missing packaged default raises: a dispatch must never lose every rule
    unannounced.
    """
    if not PACKAGE_CONSTITUTION.is_file():
        raise FileNotFoundError(
            f"packaged constitution missing: {PACKAGE_CONSTITUTION} — this "
            "install shipped no docs/; do not dispatch without the rules")
    overlay = [p for p in _walk_up(CONSTITUTION_REL, start) if not _is_packaged(p)][:1]
    return [PACKAGE_CONSTITUTION, *overlay]

# Stamped on a prime payload built from labels alone, with no text leg.
DEAD_VOCAB_NOTE = (
    "called with GH labels as concepts and no --query — prime v3 retrieval is "
    "likely dead by vocabulary; pass ontology --concepts and/or --query "
    "(docs/agents/issue-loop.command.md §1b)"
)

DEFAULT_CONFIG: dict = {
    "loop": {
        "max_issues_per_run": 3,
        "max_fix_rounds": 2,
        "training_mode": True,
        "branch_prefix": "loop/issue-",
        "require_green_baseline": True,
        "run_mode": "pass",      # pass: one frontier pass | exhaust: re-plan until dry
        "delivery": "pr-per-issue",  # pr-per-issue | stacked (one branch, one final PR)
    },
    "tdd": {
        "mode": "auto",  # auto: enforced iff the baseline probe is green
    },
    "labels": {
        "runnable": "ready-for-agent",
        "claimed": "agent-claimed",
        "on_gate_failure": "ready-for-human",
    },
    "triage": {
        # Sensitive paths → always red. Three pattern forms (see classify_pr):
        # dir prefix (trailing '/'), bare basename, glob. Empty by default:
        # a packaged rail cannot know a host repo's layout, and a guess
        # inherited from some other repo classifies the wrong files. Each host
        # declares its own in loop.toml (see loop.toml.template).
        "sensitive_paths": [],
        # Watched paths → at most yellow (skim, don't gate). Empty by default.
        "watched_paths": [],
        "red_diff_lines": 800,     # "big diff" → red
    },
    "gates": [],
}

# The scalar sections a file or --set may carry. Gates are file-only and
# checked per kind (gates.GATE_KEYS); anything else is unknown by name.
SECTIONS = ("loop", "labels", "tdd", "triage")

# [dispatch]: which agent runs a role, one sub-table per role. A role IS its
# entry: the loop's own two always resolve, and any other entry declares
# one more. The rail checks shape only; a harness or model name is the host's
# to get right. Absent keys mean the Agent tool, the session's model, no
# argv tail.
ROLES = ("implementer", "judge")
DISPATCH_KEYS = ("posture", "transport", "harness", "model", "effort", "args")
DISPATCH_CHOICES = {"transport": ("agent-tool", "herdr"), "posture": get_args(pack.Posture)}
DISPATCH_DEFAULT = {"transport": "agent-tool"}
OVERRIDABLE = " | ".join((*SECTIONS, "dispatch.<role>"))


# ---------------------------------------------------------------------------
# Config


def _known_key(section: str, key: str) -> None:
    """Refuse by name a key DEFAULT_CONFIG does not carry; a typo and a
    deleted knob fail the same way, never silently dropped."""
    if key not in DEFAULT_CONFIG[section]:
        known = ", ".join(sorted(DEFAULT_CONFIG[section]))
        raise ValueError(f"unknown key '{section}.{key}' (known: {known})")


def _checked_dispatch(role: str, entry: object) -> dict:
    """Refuse by name a key a role's entry does not carry, or a value of the
    wrong shape: transport and posture take their declared values, args is a
    list of strings, posture a string or a list of them, the rest are
    strings."""
    if not isinstance(entry, dict):
        raise ValueError(f"dispatch.{role}: expected a table, got {entry!r}")
    for key, value in entry.items():
        if key not in DISPATCH_KEYS:
            raise ValueError(f"unknown key 'dispatch.{role}.{key}' "
                             f"(known: {', '.join(DISPATCH_KEYS)})")
        strings = value if isinstance(value, list) else [value]
        choices = DISPATCH_CHOICES.get(key)
        if choices and not (strings and all(v in choices for v in strings)):
            raise ValueError(f"dispatch.{role}.{key}: expected "
                             f"{' | '.join(choices)}, got {value!r}")
        listed = key == "args" or (key == "posture" and isinstance(value, list))
        if isinstance(value, list) != listed or not all(isinstance(a, str) for a in strings):
            want = "a list of strings" if key == "args" else "a string"
            raise ValueError(f"dispatch.{role}.{key}: expected {want}, got {value!r}")
    return entry


def _checked_gates(gates: list[dict]) -> list[dict]:
    """Refuse by name a gate entry with an unknown kind or a key its kind's
    verb does not read."""
    for i, gate in enumerate(gates):
        kind = gate.get("kind")
        if kind not in GATE_KEYS:
            raise ValueError(f"unknown kind {kind!r} at gates[{i}] "
                             f"(known: {', '.join(sorted(GATE_KEYS))})")
        allowed = COMMON_GATE_KEYS | GATE_KEYS[kind]
        for key in sorted(set(gate) - allowed):
            raise ValueError(f"unknown key 'gates[{i}].{key}' for kind '{kind}' "
                             f"(known: {', '.join(sorted(allowed))})")
    return gates


def load_config(path: Path | None = None) -> dict:
    """Defaults merged with loop.toml; gates come only from the file. A
    section or key the defaults do not carry raises ``ValueError`` naming it."""
    path = path if path is not None else find_config()
    cfg: dict = {section: dict(DEFAULT_CONFIG[section]) for section in SECTIONS}
    cfg["dispatch"] = {role: dict(DISPATCH_DEFAULT) for role in ROLES}
    cfg["gates"] = []
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for section, values in data.items():
            if section == "gates":
                cfg["gates"] = _checked_gates(values)
                continue
            if section == "dispatch":
                for role, entry in values.items():
                    checked = _checked_dispatch(role, entry)
                    cfg["dispatch"].setdefault(role, dict(DISPATCH_DEFAULT)).update(checked)
                continue
            if section not in SECTIONS:
                raise ValueError(
                    f"unknown section '{section}' in {path.name} (known: {OVERRIDABLE})")
            for key in values:
                _known_key(section, key)
            cfg[section].update(values)
    return cfg


def parse_override(spec: str) -> tuple[str, str, object]:
    """Parse one ``--set [section.]key=value`` spec. The section defaults to
    ``loop``; ``dispatch.<role>.<key>`` keeps ``<role>.<key>`` as the key; the
    value is parsed as a TOML scalar, a bare word as a string."""
    head, sep, raw = spec.partition("=")
    if not sep or not head.strip() or not raw.strip():
        raise ValueError(f"malformed --set '{spec}' (expected [section.]key=value)")
    section, dot, key = head.strip().partition(".")
    if not dot:
        section, key = "loop", section
    try:
        value = tomllib.loads(f"v = {raw.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()  # bare word: a plain string, e.g. delivery=stacked
    return section, key, value


def apply_overrides(cfg: dict, specs: list[str]) -> dict:
    """Apply per-run ``--set`` overrides after loop.toml. Only existing scalar
    knobs and ``dispatch.<role>.<key>`` may be overridden; gates are file-only."""
    for spec in specs:
        section, key, value = parse_override(spec)
        if section == "dispatch":
            role, _, key = key.partition(".")
            checked = _checked_dispatch(role, {key: value})
            cfg["dispatch"].setdefault(role, dict(DISPATCH_DEFAULT)).update(checked)
            continue
        if section not in SECTIONS:
            raise ValueError(f"--set section '{section}' not overridable ({OVERRIDABLE})")
        _known_key(section, key)
        cfg[section][key] = value
    return cfg


def _split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# CLI


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--set", action="append", dest="overrides", default=[],
        metavar="[SECTION.]KEY=VALUE",
        help="per-run config override, e.g. --set delivery=stacked "
             "--set max_issues_per_run=6 --set dispatch.judge.model=opus "
             "(section defaults to 'loop'; repeatable; applied after "
             "loop.toml; gates are file-only)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="compute the runnable frontier", parents=[common])
    p_plan.add_argument("--limit", type=int, default=None)
    p_plan.add_argument("--dag", type=int, default=None, metavar="N",
                        help="scope to the DAG component containing issue N")
    p_plan.add_argument("--assume-done", default="", metavar="N,N",
                        help="treat these issues as closed (stacked delivery: slices already on the branch)")

    p_claim = sub.add_parser("claim", help="claim an issue for a run", parents=[common])
    p_claim.add_argument("number", type=int)
    p_claim.add_argument("--run-id", required=True)

    p_release = sub.add_parser("release", help="release a claimed issue", parents=[common])
    p_release.add_argument("number", type=int)

    sub.add_parser("config", help="print resolved config as JSON", parents=[common])

    p_check = sub.add_parser(
        "check", help="run one deterministic gate, or an issue's verify: lines", parents=[common])
    what = p_check.add_mutually_exclusive_group(required=True)
    what.add_argument("--gate", help="a command | diff gate id from loop.toml")
    what.add_argument("--issue", type=int, metavar="N",
                      help="run the command gates, then issue N's verify: lines, as one list")
    p_check.add_argument("--cwd", default=".")
    p_check.add_argument("--base-ref", default="origin/main")

    p_validate = sub.add_parser(
        "validate", help="validate a judgment gate's subagent return", parents=[common])
    p_validate.add_argument("--gate", required=True)
    p_validate.add_argument("--return-json", required=True,
                            help="file with the subagent's JSON return (schema per "
                                 "kind: judge {criteria[], findings[]})")
    p_validate.add_argument("--posture", default="reader", choices=("reader", "shape"),
                            help="shape: the shape posture's return {verdict, flow, "
                                 "owns[], options[]}")

    p_prime = sub.add_parser("prime", help="assemble prior-trajectory prime context for an issue", parents=[common])
    p_prime.add_argument("number", type=int)
    p_prime.add_argument("--run-id", required=True)
    p_prime.add_argument("--labels", default=None,
                         help="comma-separated issue label names; omit to fetch via gh")
    p_prime.add_argument("--concepts", default=None,
                         help="comma-separated ONTOLOGY concepts to match (what the "
                              "write side tags trajectories with); omit to derive "
                              "from --labels")
    p_prime.add_argument("--query", default="",
                         help="the issue's own text (title, or title + body) — the "
                              "full-text retrieval leg, fused with --concepts")
    p_prime.add_argument("--db", default=None, help="index db path (opened read-only)")
    p_prime.add_argument("--vault", default=None,
                         help="vault root; resolves the index under the vault's "
                              "weave_dir override (config.toml) when --db is absent, "
                              "else <vault>/.weave/index.db")
    p_prime.add_argument("--limit", type=int, default=3,
                         help="max prior trajectories (and decisions) to splice — top-N per kind")
    p_prime.add_argument("--budget-chars", type=int, default=1200,
                         help="char budget for the spliced block")
    p_prime.add_argument("--decisions", default=None,
                         help="comma-separated decisions_for_file note ids to fold into served context")
    p_prime.add_argument("--buffer", default=None,
                         help="session buffer JSONL to append the loop_prime served-context event to")
    p_prime.add_argument("--session-id", default="",
                         help="loop session id, stamped into the served-context event")
    p_prime.add_argument("--dry-run", action="store_true",
                         help="print the payload and suppress the buffer write even "
                              "with --buffer — inspect what prime would serve "
                              "without logging it as served")

    p_triage = sub.add_parser("triage", help="classify a shipped PR into a risk lane", parents=[common])
    p_triage.add_argument("number", type=int, nargs="?", default=None,
                          help="issue/PR number for the output (optional; the "
                               "signals JSON may also carry an 'issue' key)")
    p_triage.add_argument("--signals-json", required=True,
                          help="file with the PR's signal set: {fix_rounds, "
                               "diff_lines, files_touched, tests_touched, "
                               "review_severity, baseline_green}")

    p_traj = sub.add_parser("trajectory", help="assemble a per-issue trajectory payload (memory feed)", parents=[common])
    p_traj.add_argument("number", type=int)
    p_traj.add_argument("--cwd", default=".", help="the checkout to read git from")
    p_traj.add_argument("--base-ref", default="origin/main")
    p_traj.add_argument("--branch", default=None, metavar="REF",
                        help="the branch to record; defaults to the checkout's HEAD branch")
    p_traj.add_argument("--gates-json", required=True, help="file with the gate results list")
    p_traj.add_argument("--skills-json", default=None,
                        help="file with the stage-dispatch log: a list of "
                             "{id, role, skill, outcome, fix_rounds_attributed} "
                             "plus the optional dispatch join keys "
                             "(issue-loop.command.md §3) — the skills the loop "
                             "dispatched (implementer, judge, ...). `skill` names "
                             "the skill that ran the stage, empty when none did. "
                             "Omit for an empty skills[].")
    p_traj.add_argument("--skill-centric", action="store_true",
                        help="mark this record skill-centric (adds the "
                             "skill-invocation tag alongside loop-run)")
    p_traj.add_argument("--primed", action=argparse.BooleanOptionalAction, default=None,
                        help="mirror the claim-time prime verdict: --primed (received "
                             "prior-trajectory context) / --no-primed (nothing served). "
                             "Omit to leave both prime keys out.")
    p_traj.add_argument("--served-json", default=None,
                        help="file with the served note ids (prime output's `served`) to "
                             "mirror into the trajectory note frontmatter")
    p_traj.add_argument("--trace-json", default=None,
                        help="file with the semantic execution trace: a JSON "
                             "object {rounds[], criteria[], edge_cases[], deviations[], tdd} "
                             "the orchestrator condenses from the gate agents' own reports. "
                             "Omit to leave the trace key out.")
    p_traj.add_argument("--fix-rounds", type=int, default=0)
    p_traj.add_argument("--outcome", required=True,
                        choices=["shipped", "routed-to-human", "awaiting-approval"])
    p_traj.add_argument("--pr-url", default="")
    p_traj.add_argument("--run-id", default="")

    p_board = sub.add_parser("board", help="issue-board hygiene (doctor | sweep)", parents=[common])
    p_board.add_argument("verb", choices=["doctor", "sweep"])
    p_board.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                         help="repo to check (repeatable); default: the cwd's clone")
    p_board.add_argument("--apply", action="store_true",
                         help="sweep: run the planned ops through gh (default: print the plan)")
    p_board.add_argument("--only", default="", metavar="OP,OP",
                         help="sweep: restrict to these op kinds (create_label, add_label, "
                              "remove_label, add_blocker, retitle, delete_label)")

    p_pack = sub.add_parser("pack", help="compose one dispatch's context and print it", parents=[common])
    p_pack.add_argument("number", type=int, nargs="+",
                        help="the issue; the shape posture takes every issue of a stack")
    p_pack.add_argument("--role", default="implementer", metavar="ROLE",
                        help="any [dispatch] role; its posture shapes the pack — "
                             "writer: issue + rules + persona + repo map + standing "
                             "orders; reader: issue + rules + repo map + touched "
                             "modules; shape: issues + rules + touched modules + "
                             "module edges")
    p_pack.add_argument("--posture", default=None, choices=get_args(pack.Posture),
                        help="one of the role's postures (default: its first)")
    p_pack.add_argument("--base-ref", default="origin/main",
                        help="reader and shape: the touched modules are base-ref...HEAD's")
    p_pack.add_argument("--cwd", default=".",
                        help="the worktree to map (its .codegraph index is self-provisioned)")
    p_pack.add_argument("--codegraph-bin",
                        default=os.environ.get("CODEGRAPH_BIN") or "codegraph",
                        help="codegraph executable (default: $CODEGRAPH_BIN, else "
                             "`codegraph` on PATH); absent or failing → a degraded block")
    p_pack.add_argument("--prime", default=None, metavar="FILE",
                        help="host extension: file with the prime block to splice")
    p_pack.add_argument("--trace", default=None, metavar="FILE",
                        help="host extension: file with the run's threaded trace to splice")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        cfg = apply_overrides(load_config(), args.overrides)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2

    if args.cmd == "config":
        # An "error" entry tells the orchestrator to stop, not to splice nothing.
        try:
            cfg["constitution"] = [str(p) for p in find_constitution()]
        except FileNotFoundError as exc:
            cfg["constitution"] = {"error": str(exc)}
        # Compact, like `gh --json`: a verify line matches ` => ` as a raw substring, so lists stay on one line.
        print(json.dumps(cfg))
    elif args.cmd == "plan":
        issues = github.fetch_issues()
        if args.dag is not None:
            try:
                issues = dag.scope_to_dag(issues, args.dag)
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                return 2
        if args.assume_done:
            done = {int(n) for n in args.assume_done.split(",") if n.strip()}
            issues = dag.apply_assume_done(issues, done)
        result = dag.compute_frontier(issues, cfg, limit=args.limit)
        print(json.dumps(result, indent=2))
    elif args.cmd == "claim":
        # The assignee is the claim; labels.claimed stays readable for `plan`.
        github.run(["issue", "edit", str(args.number), "--add-assignee", "@me"])
        github.run(["issue", "comment", str(args.number), "--body",
                    f"🤖 issue-loop: claimed by run `{args.run_id}`."])
        print(f"claimed #{args.number}")
    elif args.cmd == "release":
        github.run(["issue", "edit", str(args.number), "--remove-assignee", "@me"])
        print(f"released #{args.number}")
    elif args.cmd == "check" and args.issue is not None:
        cwd = Path(args.cwd).resolve()
        try:
            body = json.loads(github.run(["issue", "view", str(args.issue),
                                          "--json", "body"], cwd=cwd))["body"]
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
            # Exit 2, not 1: exit 1 would read as "a check is red".
            detail = (e.stderr or "").strip() if hasattr(e, "stderr") else str(e)
            print(json.dumps({"error": f"cannot read issue #{args.issue}: {detail}"}))
            return 2
        checks = [g for g in cfg["gates"] if g["kind"] == "command"] + verify_gates(body)
        results = [run_command_gate(g, cwd) for g in checks]
        passed = sum(r["passed"] for r in results)
        print(json.dumps({"issue": args.issue, "results": results,
                          "summary": f"{passed}/{len(results)} passed"}, indent=2))
        return 0 if passed == len(results) else 1
    elif args.cmd in ("check", "validate"):
        gate = next((g for g in cfg["gates"] if g["id"] == args.gate), None)
        if gate is None:
            print(json.dumps({"error": f"no gate '{args.gate}' in config"}))
            return 2
        if args.cmd == "check":
            cwd = Path(args.cwd).resolve()
            execute = DETERMINISTIC.get(gate["kind"])
            if execute is None:
                print(json.dumps({"error": f"gate kind '{gate['kind']}' is LLM-judged — run it from the /issue-loop command, not the script"}))
                return 2
            result = execute(gate, cwd, args.base_ref)
            print(json.dumps(result, indent=2))
            return 0 if result["passed"] else 1
        if gate["kind"] not in JUDGMENT:
            print(json.dumps({"error": f"gate kind '{gate['kind']}' is deterministic — run it with `check`, not `validate`"}))
            return 2
        try:
            raw = json.loads(Path(args.return_json).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # Non-JSON takes the rejection path, so the orchestrator re-asks.
            result = reject(gate, [f"payload: not valid JSON ({e})"])
        else:
            result = validate(gate, raw, args.posture)
        print(json.dumps(result, indent=2))
        return 2 if result["reasons"] else (0 if result["passed"] else 1)
    elif args.cmd == "prime":
        concepts = (_split_csv(args.concepts) if args.concepts is not None
                    else _split_csv(args.labels) if args.labels is not None
                    else github.fetch_labels(args.number))
        conn = None
        db_path = index_client.resolve_db_path(args.db, args.vault)
        if db_path and Path(db_path).exists():
            try:
                conn = index_client.open_ro(db_path)
            except index_client.Error:
                conn = None
        try:
            payload = trajectory.build_prime_payload(
                args.number, args.run_id, concepts, conn=conn,
                limit=args.limit, budget_chars=args.budget_chars,
                decisions=_split_csv(args.decisions) if args.decisions else None,
                query=args.query,
            )
        finally:
            if conn is not None:
                conn.close()
        if args.concepts is None and not args.query:
            payload["note"] = "; ".join(filter(None, [payload["note"], DEAD_VOCAB_NOTE]))
        if args.buffer and not args.dry_run and payload["primed"] and payload["served"]:
            trajectory.append_served_event(args.buffer, args.run_id, args.number,
                                           payload["served"], args.session_id)
        print(json.dumps(payload, indent=2))
    elif args.cmd == "triage":
        signals = json.loads(Path(args.signals_json).read_text(encoding="utf-8"))
        if not isinstance(signals, dict):
            print(json.dumps({"error": "signals-json must be a JSON object"}))
            return 2
        result = triage.classify_pr(signals, cfg["triage"],
                                    red_label=cfg["labels"]["on_gate_failure"])
        issue = args.number if args.number is not None else signals.get("issue")
        print(json.dumps({"issue": issue, **result}, indent=2))
    elif args.cmd == "trajectory":
        cwd = Path(args.cwd).resolve()
        issue = json.loads(github.run(["api", f"repos/{{owner}}/{{repo}}/issues/{args.number}"]))
        branch = args.branch or subprocess.run(
            ["git", "branch", "--show-current"], cwd=cwd,
            capture_output=True, text=True, check=True).stdout.strip()
        commits = subprocess.run(
            ["git", "log", "--oneline", f"{args.base_ref}..{branch}"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        numstat = subprocess.run(
            ["git", "diff", "--numstat", f"{args.base_ref}...{branch}"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout
        gates = json.loads(Path(args.gates_json).read_text(encoding="utf-8"))
        skills = (json.loads(Path(args.skills_json).read_text(encoding="utf-8"))
                  if args.skills_json else [])
        served = (json.loads(Path(args.served_json).read_text(encoding="utf-8"))
                  if args.served_json else None)
        trace = (json.loads(Path(args.trace_json).read_text(encoding="utf-8"))
                 if args.trace_json else None)
        try:
            payload = trajectory.build_trajectory(
                issue, branch=branch, commits=commits, numstat=numstat, gates=gates,
                fix_rounds=args.fix_rounds, outcome=args.outcome,
                pr_url=args.pr_url, run_id=args.run_id,
                skills=skills, skill_centric=args.skill_centric,
                primed=args.primed, served=served, trace=trace,
            )
        except ValueError as e:
            print(json.dumps({"error": "; ".join(e.args), "reasons": list(e.args)}))
            return 2
        print(json.dumps(payload, indent=2))
    elif args.cmd == "board":
        repos = args.repo or [github.run(["repo", "view", "--json", "nameWithOwner",
                                          "--jq", ".nameWithOwner"]).strip()]
        report = board.doctor([github.fetch_board(r) for r in repos], cfg)
        if args.verb == "doctor":
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        ops = board.plan_sweep(report)
        if args.only:
            ops = [o for o in ops if o["op"] in set(_split_csv(args.only))]
        if not args.apply:
            print(json.dumps({"ops": ops, "note": "dry run — pass --apply to execute"}, indent=2))
            return 0
        applied, failed = [], []
        for op in ops:
            try:
                github.apply_op(op)
                applied.append(op)
            except subprocess.CalledProcessError as e:
                failed.append({**op, "error": (e.stderr or "").strip()})
        print(json.dumps({"applied": applied, "failed": failed}, indent=2))
        return 1 if failed else 0
    elif args.cmd == "pack":
        root = Path(args.cwd).resolve()
        roles = list(cfg["dispatch"])
        declared = cfg["dispatch"].get(args.role, {}).get("posture")
        postures = declared if isinstance(declared, list) else [declared] if declared else []
        # A role no entry declares, a posture its entry does not carry, a
        # missing rules file or persona, a range git cannot diff: each is an
        # error marker, never a pack shaped by a guess or dispatched without them.
        if args.role not in roles:
            print(json.dumps({"error": f"unknown role '{args.role}' (known: {', '.join(roles)})"}))
            return 2
        posture = args.posture or (postures[0] if postures else None)
        if posture not in postures:
            print(json.dumps({"error": f"dispatch.{args.role}.posture: required to shape "
                                       f"the pack, and {posture!r} is not among {postures} "
                                       f"({' | '.join(DISPATCH_CHOICES['posture'])})"}))
            return 2
        issues = {n: pack.Issue(**json.loads(github.run(
            ["issue", "view", str(n), "--json", "title,body"], cwd=root))) for n in args.number}
        try:
            rules = [pack.body(p) for p in find_constitution(root)]
            persona = pack.body(PACKAGE_PERSONA) if posture == "writer" else ""
            touched = None if posture == "writer" else pack.Touched.since(args.base_ref, root)
            text = pack.compose(
                issues, args.role, posture, rules, persona,
                pack.Codegraph(args.codegraph_bin, root), touched,
                prime=Path(args.prime).read_text(encoding="utf-8") if args.prime else "",
                trace=Path(args.trace).read_text(encoding="utf-8") if args.trace else "",
            )
        except (FileNotFoundError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(text, end="")
    return 0
