"""Deterministic rail for the /issue-loop dev workflow.

The issue tracker IS the DAG: blocking edges live as GitHub-native issue
dependencies (what /to-tickets and /wayfinder publish since Pocock skills
v1.1.0) and nowhere else. The graph advances through GitHub's own state machine —
a merged PR closes its issue via ``Closes #N``, which unblocks dependents on
the next run. This script never stores state; it re-reads the tracker and
computes the current frontier, plus the weakly-connected components that
tell the orchestrator which open issues belong to one DAG (chase
sequentially, ``run_mode=exhaust``) vs unrelated work (parallel-safe across
components). LLM judgment stays in the /issue-loop command (implementer,
judge); everything schedulable is plain graph math here.

Subcommands:
  plan     — snapshot issues via `gh`, compute frontier + components (JSON)
  claim    — claim an issue for a run (the assignee IS the claim)
  release  — drop the claim
  config     — print resolved loop config (defaults merged with loop.toml;
               an unknown or deleted key is refused by name)
  check      — run one deterministic gate (kind: command | diff) and emit JSON
  validate   — validate a judgment gate's subagent return (kind: judge |
               simplify) against its schema; rejects for a re-ask
  prime      — assemble prior-trajectory prime context for an issue at claim
               time (reads the derived index read-only; always serves what
               it finds)
  trajectory — assemble a per-issue trajectory payload for the memory feed
               (the optional host extension; shape: devloop-boundaries.md §4)
  board      — doctor: lint one or more repos' boards against the grammar
               dag.py reads (JSON report, exit 1 on errors); sweep: replay
               the mechanical fixes (--plan by default, --apply runs them)
  pack       — compose one dispatch's context (issue, persona + constitution,
               the repo map spliced from codegraph's CLI) and print it

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

from devloop import board, dag, github, index_client, pack, trajectory, triage

# Imported by name: `main` binds a local `gates` in the trajectory branch,
# which would shadow a module of that name for the whole function.
from devloop.gates import (
    COMMON_GATE_KEYS,
    DETERMINISTIC,
    GATE_KEYS,
    JUDGMENT,
    reject,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_REL = Path("docs") / "agents" / "loop.toml"
PACKAGE_CONFIG = REPO_ROOT / CONFIG_REL
CONSTITUTION_REL = Path("docs") / "agents" / "constitution.md"
PACKAGE_CONSTITUTION = REPO_ROOT / CONSTITUTION_REL
PACKAGE_PERSONA = REPO_ROOT / "docs" / "agents" / "ponytail-persona.md"


def _find_upward(rel: Path, start: Path | None = None) -> Path | None:
    """The host repo's copy of ``rel``, walking up from the working directory —
    the same convention every repo-scoped tool uses. Stops at the first
    ``.git``: never silently inherit an ancestor directory's file."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        found = directory / rel
        if found.is_file():
            return found
        if (directory / ".git").exists():   # repo root: stop, don't escape it
            break
    return None


def find_config(start: Path | None = None) -> Path:
    """The host repo's ``docs/agents/loop.toml``, else the package's own copy.

    The gate pipeline belongs to the repo being worked on, not to the installed
    rail, so the host's file *replaces* the packaged one when found.

    The fallback is what the funloops workspace itself resolves (it keeps its
    loop.toml with the package, not at the workspace root), and it may not
    exist at all in a wheel install that shipped no ``docs/`` — ``load_config``
    treats a missing file as "defaults only", which is honest: no gates
    configured, and every ``check`` says so by name.
    """
    return _find_upward(CONFIG_REL, start) or PACKAGE_CONFIG


def find_constitution(start: Path | None = None) -> list[Path]:
    """The packaged constitution, plus the host repo's overlay when it has one.

    Same upward walk as ``find_config``, opposite merge posture: loop.toml is
    replace-on-find (the gate pipeline is the repo's), the constitution is
    extend-on-find — the packaged default always applies and a repo's
    ``docs/agents/constitution.md`` is appended after it, never substituted
    (dec-1746aec3). The walk finding the packaged file itself — by path (this
    checkout) or by identical bytes (another checkout/worktree of it) —
    serves it once, keeping the splice single.

    A missing packaged default raises rather than resolving: an install that
    shipped no ``docs/`` (the wheel packages only ``devloop/``) must not hand
    the orchestrator a path that splices as silence — a dispatch that loses
    all twelve rules unannounced is the fail-open the constitution's own
    rule 7 names. loop.toml's missing-file degrade is honest because defaults
    exist in code; the constitution has no in-code fallback.
    """
    if not PACKAGE_CONSTITUTION.is_file():
        raise FileNotFoundError(
            f"packaged constitution missing: {PACKAGE_CONSTITUTION} — this "
            "install shipped no docs/; do not dispatch without the rules")
    overlay = _find_upward(CONSTITUTION_REL, start)
    # ponytail: byte-equality dedupes the reachable case (worktrees of the
    # same commit); a DIVERGED copy of the packaged file in another checkout
    # still serves twice. Upgrade path: anchor on repo-relative position.
    if overlay is None or overlay.read_bytes() == PACKAGE_CONSTITUTION.read_bytes():
        return [PACKAGE_CONSTITUTION]
    return [PACKAGE_CONSTITUTION, overlay]

# Stamped on a prime payload built the pre-#100 way (labels as concepts, no
# text leg) — the dead-by-vocabulary join; see issue-loop.command.md §1b.
DEAD_VOCAB_NOTE = (
    "called with GH labels as concepts and no --query — prime v3 retrieval is "
    "likely dead by vocabulary; pass ontology --concepts and/or --query "
    "(docs/agents/issue-loop.command.md §1b)"
)

DEFAULT_CONFIG: dict = {
    "loop": {
        "max_issues_per_run": 3,
        "max_parallel": 1,
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
        "red_min_diff_lines": 800,     # "big diff" → red
    },
    "gates": [],
}

# The scalar sections a file or --set may carry. Gates are file-only and
# checked per kind (gates.GATE_KEYS); anything else is unknown by name.
SECTIONS = ("loop", "labels", "tdd", "triage")


# ---------------------------------------------------------------------------
# Config


def _known_key(section: str, key: str) -> None:
    """One check for both config paths: a key DEFAULT_CONFIG does not carry is
    unknown — a typo and a knob the subtractive pass deleted (dec-cf8f0d33)
    are refused the same way, naming the key, never silently dropped."""
    if key not in DEFAULT_CONFIG[section]:
        known = ", ".join(sorted(DEFAULT_CONFIG[section]))
        raise ValueError(f"unknown key '{section}.{key}' (known: {known})")


def _checked_gates(gates: list[dict]) -> list[dict]:
    """A gate entry names a known kind and carries only the keys that kind's
    verb reads; a stale kind (`review`) or a missing one is refused by name."""
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
    """Defaults merged with loop.toml. Gates come only from the file.

    ``path`` defaults to whatever ``find_config`` resolves for the working
    directory, so the host repo's file wins over the packaged one. A section
    or key the defaults do not carry raises ``ValueError`` naming it.
    """
    path = path if path is not None else find_config()
    cfg: dict = {section: dict(DEFAULT_CONFIG[section]) for section in SECTIONS}
    cfg["gates"] = []
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for section, values in data.items():
            if section == "gates":
                cfg["gates"] = _checked_gates(values)
                continue
            if section not in SECTIONS:
                raise ValueError(
                    f"unknown section '{section}' in {path.name} (known: {' | '.join(SECTIONS)})")
            for key in values:
                _known_key(section, key)
            cfg[section].update(values)
    return cfg


def parse_override(spec: str) -> tuple[str, str, object]:
    """Parse one ``--set [section.]key=value`` spec.

    The section defaults to ``loop`` (the common case: ``--set
    delivery=stacked``). The value is parsed as a TOML scalar so the
    override language is exactly loop.toml's (``6`` → int, ``true`` → bool,
    quoted or bare words → str).
    """
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
    """Per-run config overrides, applied after loop.toml.

    Only existing scalar knobs may be overridden — an unknown section or key
    is a hard error (typo protection), and gates are file-only by design
    (the gate pipeline is a trust boundary, not a run-time posture).
    """
    for spec in specs:
        section, key, value = parse_override(spec)
        if section not in SECTIONS:
            raise ValueError(
                f"--set section '{section}' not overridable ({' | '.join(SECTIONS)})")
        _known_key(section, key)
        cfg[section][key] = value
    return cfg


def _split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# CLI


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser (factory so the argparse contract is testable
    without going through main → gh → git)."""
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--set", action="append", dest="overrides", default=[],
        metavar="[SECTION.]KEY=VALUE",
        help="per-run config override, e.g. --set delivery=stacked "
             "--set max_issues_per_run=6 (section defaults to 'loop'; "
             "repeatable; applied after loop.toml; gates are file-only)",
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

    p_check = sub.add_parser("check", help="run one deterministic gate", parents=[common])
    p_check.add_argument("--gate", required=True)
    p_check.add_argument("--cwd", default=".")
    p_check.add_argument("--base-ref", default="origin/main")

    p_validate = sub.add_parser(
        "validate", help="validate a judgment gate's subagent return", parents=[common])
    p_validate.add_argument("--gate", required=True)
    p_validate.add_argument("--return-json", required=True,
                            help="file with the subagent's JSON return (schema per "
                                 "kind: judge {criteria[], findings[]}, "
                                 "simplify {outcome, lines_delta, cuts[], kept[]})")

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
                               "review_severity, baseline_green, acceptance}")

    p_traj = sub.add_parser("trajectory", help="assemble a per-issue trajectory payload (memory feed)", parents=[common])
    p_traj.add_argument("number", type=int)
    p_traj.add_argument("--cwd", default=".", help="the issue's implementer worktree")
    p_traj.add_argument("--base-ref", default="origin/main")
    p_traj.add_argument("--gates-json", required=True, help="file with the gate results list")
    p_traj.add_argument("--skills-json", default=None,
                        help="file with the stage-dispatch log: a list of "
                             "{id, role, outcome, fix_rounds_attributed} — the "
                             "skills the loop dispatched (implementer, acceptance "
                             "judge, reviewer, ...). Omit for an empty skills[].")
    p_traj.add_argument("--skill-centric", action="store_true",
                        help="mark this record skill-centric (adds the "
                             "skill-invocation tag alongside loop-run)")
    p_traj.add_argument("--primed", action=argparse.BooleanOptionalAction, default=None,
                        help="mirror the claim-time prime verdict: --primed (received "
                             "prior-trajectory context) / --no-primed (nothing served). "
                             "Omit to leave both prime keys out (pre-#57 shape).")
    p_traj.add_argument("--served-json", default=None,
                        help="file with the served note ids (prime output's `served`) to "
                             "mirror into the trajectory note frontmatter")
    p_traj.add_argument("--trace-json", default=None,
                        help="file with the semantic execution trace (issue #85): a JSON "
                             "object {rounds[], criteria[], simplify, stack_simplify, "
                             "edge_cases[], tdd} "
                             "the orchestrator condenses from the gate agents' own reports. "
                             "Omit to leave the trace key out (pre-#85 shape).")
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
    p_pack.add_argument("number", type=int)
    p_pack.add_argument("--role", choices=["implementer", "judge"], default="implementer",
                        help="implementer: issue + persona/constitution + repo map + "
                             "standing orders; judge: issue + repo map")
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
        # The orchestrator's half of the constitution contract: splice the
        # listed files in order; an "error" entry means STOP and surface it.
        try:
            cfg["constitution"] = [str(p) for p in find_constitution()]
        except FileNotFoundError as exc:
            cfg["constitution"] = {"error": str(exc)}
        print(json.dumps(cfg, indent=2))
    elif args.cmd == "plan":
        limit = args.limit if args.limit is not None else cfg["loop"]["max_issues_per_run"]
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
        result = dag.compute_frontier(issues, cfg, limit=limit)
        print(json.dumps(result, indent=2))
    elif args.cmd == "claim":
        # wayfinder convention: the assignee IS the claim — renders natively
        # in the tracker UI, no label vocabulary consumed (dec-cf8f0d33
        # retired the label mode; labels.claimed stays readable for `plan`).
        github.run(["issue", "edit", str(args.number), "--add-assignee", "@me"])
        github.run(["issue", "comment", str(args.number), "--body",
                    f"🤖 issue-loop: claimed by run `{args.run_id}`."])
        print(f"claimed #{args.number}")
    elif args.cmd == "release":
        github.run(["issue", "edit", str(args.number), "--remove-assignee", "@me"])
        print(f"released #{args.number}")
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
            # A return that is not even JSON is the first thing worth re-asking
            # for, so it takes the same rejection path as a schema violation.
            result = reject(gate, [f"payload: not valid JSON ({e})"])
        else:
            result = validate(gate, raw)
        print(json.dumps(result, indent=2))
        return 2 if result["reasons"] else (0 if result["passed"] else 1)
    elif args.cmd == "prime":
        # Label fallback (pre-#100 convention); gh fetch only when neither flag given.
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
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=cwd,
                                capture_output=True, text=True, check=True).stdout.strip()
        commits = subprocess.run(
            ["git", "log", "--oneline", f"{args.base_ref}..HEAD"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        numstat = subprocess.run(
            ["git", "diff", "--numstat", f"{args.base_ref}...HEAD"],
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
            print(json.dumps({"error": str(e)}))
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
        issue = json.loads(github.run(["issue", "view", str(args.number),
                                       "--json", "title,body"], cwd=root))
        persona = ""
        if args.role == "implementer":
            try:
                # The rules fail closed (find_constitution raises; so does a
                # persona missing from a docs-less wheel): an error marker,
                # never a pack that dispatches without them.
                persona = "\n\n".join(pack.body(p.read_text(encoding="utf-8"))
                                      for p in [PACKAGE_PERSONA, *find_constitution(root)])
            except FileNotFoundError as exc:
                print(json.dumps({"error": str(exc)}))
                return 2
        repo_map = pack.render_map(args.codegraph_bin, root, issue["title"],
                                   pack.named_files(issue["body"], root))
        print(pack.compose(
            role=args.role, number=args.number, issue=issue, persona=persona,
            repo_map=repo_map,
            prime=Path(args.prime).read_text(encoding="utf-8") if args.prime else "",
            trace=Path(args.trace).read_text(encoding="utf-8") if args.trace else "",
        ), end="")
    return 0
