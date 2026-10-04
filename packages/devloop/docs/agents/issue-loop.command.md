---
name: issue-loop
description: "Drain the ready-for-agent frontier of the GitHub issue DAG: implement each unblocked issue in its own worktree, run the configured gate pipeline (diff/tests/verify/judge), and open a draft PR per issue. Headless-safe."
argument-hint: "[issue-number] | --dag <issue> to work one DAG | --stacked | --max-issues <n> | --set key=value | nothing to drain the frontier"
disable-model-invocation: true
---

# Issue Loop — issue → gates → PR

Drain the runnable frontier of the issue DAG. A merged PR closes its issue
and unblocks its dependents. The contract is the issue's acceptance criteria
and its Interfaces block's intent; nothing outside it can fail a PR.

**Install (machine-local, never committed).** From the repo root:

```bash
ln -s ../../packages/devloop/docs/agents/issue-loop.command.md .claude/commands/issue-loop.md
```

**The rail** is the `devloop` console script. A rail call that targets a
worktree is `uv run --directory <worktree> devloop …`, so the branch's own
`docs/agents/loop.toml` governs its gates (a new host starts from
`loop.toml.template`). See also `issue-tracker.md`, `triage-labels.md` and
`devloop-boundaries.md`.

<!-- host-extension: memory feed — needs a Thinkweave vault on the host. Without one, skip every marked block; the loop below is complete and runs unchanged. -->

**Optional host extension: the memory feed.** Where the host has a Thinkweave
vault, the loop primes each implementer from prior runs (§1b) and writes back
one trajectory per issue (§3). These stretches sit in `host-extension` blocks.

<!-- /host-extension -->

## 0. Plan

`loop.toml` holds the defaults; the arguments set this run's posture.
Translate `--stacked` to `--set delivery=stacked` and `--max-issues <n>` to
`--set max_issues_per_run=<n>`, and pass any other `--set
[section.]key=value` through verbatim. Append the collected `--set` flags to
every `devloop` call in this run. Never edit `loop.toml` for the user.

`run_mode`: **pass** works the frontier once. **exhaust** re-plans after each
shipped issue while the frontier is non-empty; when only blocked issues
remain, report and stop, never busy-wait. `delivery`: **pr-per-issue** ships
each issue as its own branch and draft PR; **stacked** is §1e.

Run `uv run devloop config` and `uv run devloop plan`. `plan` prints the
`run_id`, the `frontier` cut to `max_issues_per_run`, the `deferred` rest,
`blocked` and `claimed`. Each dispatched stage is a role whose
`dispatch.<role>` entry picks its posture and transport.

- An issue number as argument: the frontier is that issue. Check in `plan`
  that it is unblocked and unclaimed; if not, say so and stop.
- `--dag <N>`: pass `--dag <N>` to every `plan` call; `run_mode` defaults to
  `exhaust`.
- An empty frontier: report why (all blocked, all claimed, PRs awaiting
  merge) and stop.

Make the run directory `$(git rev-parse --git-common-dir)/devloop/runs/<run-id>/`.
Every dispatch and return file lives there, outside every worktree.

## 0.5 Worktree and baseline

**You create every worktree.** For each issue (each stack in §1e):

```bash
git worktree add .claude/worktrees/<name> -b <branch> origin/main
```

`<branch>` is `<branch_prefix><N>`. No transport creates a worktree.

Once per run, **before any edits**, run the baseline in the first worktree:
`uv run --directory <worktree> devloop check --baseline`. Its `baseline`
field (`green` or `red`) is the baseline line of every implementer dispatch.

- **green**: proceed. The pack's standing orders enforce TDD.
- **red** with `require_green_baseline = true`: stop before implementing.
  Name the open issue that owns the failure. Training mode: ask the user;
  headless: refuse.
- **red** with `require_green_baseline = false`: proceed degraded. The tests
  gate runs on the implementer's declared test targets, and the tests cell of
  every PR's gate table carries `⚠ degraded-baseline` naming the
  pre-existing failures.

## 1. Per issue

Dispatch every frontier issue at once, each in its own worktree. Sibling
issues that touch one file may conflict at merge; a human resolves that.

### 1a. Claim

`uv run devloop claim <N> --run-id <run-id>`. The assignee is the claim.

### 1b. Implement

<!-- host-extension: memory feed — needs a Thinkweave vault. Without one, dispatch unprimed and record nothing; `primed=false` is the vault-less fact. -->

**Prime.** Before the implementer's pack, fetch prior lessons:

```bash
uv run devloop prime <N> --run-id <run-id> \
  --concepts "<2-3 ontology terms>" --query "<the issue's title (+ body)>" \
  [--decisions "<comma-separated note ids>"] --vault <vault-root> \
  [--buffer <weave_dir>/buffer/<this-session-id>.jsonl]
```

`--concepts` are ontology terms from `weave_concepts`, never GitHub labels.
`--query` is the issue's own text. `--decisions` is the union of the ids
under the issue body's `## Decisions` heading and the ids
`weave_graph(file_path=…, filter='decisions_for_file')` finds for the files
the issue names (none found: walk their directory). Write the printed `block`
to a file and pass it to the pack as `--prime <file>`; an empty `block`
splices nothing. Keep `primed` and `served` for §3.

<!-- /host-extension -->

**The pack is the whole dispatch.** Write it into the run directory:

```bash
uv run --directory <worktree> devloop pack <N> --role implementer \
  --branch <branch> --baseline <green|red> \
  --return <run-dir>/impl-<N>.md --out <run-dir>/dispatch-<N>.md
```

`pack --out` prints the dispatch file's path; add nothing to the file. A JSON
`error` instead means a role, posture, rules file or persona did not resolve:
stop and surface it. Every role writes its return to the file its dispatch
names; read that file, never the agent's screen. A fix round or a re-ask
names a fresh return path.

Dispatch by the role's `transport`, with this prompt: "Your complete dispatch
is `<dispatch file>`. Read the whole file first and follow it exactly."

- **`agent-tool`**: a fresh agent (never the fork type, which inherits this
  conversation), with the entry's `model` when it names one. A fix round is a
  SendMessage to the same agent, which keeps its context.
- **`herdr`**: the recipe in [`herdr-transport.md`](herdr-transport.md).

### 1c. Gates

Run the gates in order, in the implementer's worktree:

```bash
uv run --directory <worktree> devloop check --gate diff-guard --base-ref origin/main
uv run --directory <worktree> devloop check --issue <N> > <run-dir>/check-<N>.json
```

`check --issue` runs the command gates, then the issue's `verify:` lines. Any
red result, a forbidden-path breach included, is a fix round.

**The judge.** Write its dispatch and send it to a fresh agent as
`dispatch.judge` says:

```bash
uv run --directory <worktree> devloop pack <N> --role judge --base-ref origin/main \
  --check-json <run-dir>/check-<N>.json [--evidence <run-dir>/impl-<N>.md.demo] \
  --return <run-dir>/judge-<N>.json --out <run-dir>/dispatch-judge-<N>.md
```

Pass `--evidence` when the issue carries a `demo:` criterion. Validate the
return before acting on it:

```bash
uv run devloop validate --gate judge --return-json <run-dir>/judge-<N>.json
```

Exit `0`: passed. Exit `1`: a criterion is `not-met`; run a fix round. Exit
`2`: schema-rejected; re-ask the same agent with the printed `reasons` and a
fresh return path. Never hand-fix a return or read a verdict out of a
rejected one. A second rejection routes to human. Only a `not-met` criterion
blocks. Findings go to the PR's findings comment and the triage lane, never
to a fix round or an issue.

**The shape posture** judges rules 1 and 4 once per PR. In pr-per-issue, or a
one-slice stack, send the same judge the shape dispatch after its first return
validates. A stack of two or more slices gets a fresh judge at the tip (§1e).

```bash
uv run --directory <worktree> devloop pack <N> [<N2> …] --role judge --posture shape \
  --base-ref origin/main --return <run-dir>/shape-<N>.json --out <run-dir>/dispatch-shape-<N>.md
uv run devloop validate --gate judge --posture shape --return-json <run-dir>/shape-<N>.json
```

Exit `0`: met. Exit `2`: re-ask once. Exit `1` is a restructure case: it
stops the run at the human, never a fix round. Route to human with the case as
evidence, on the issue (stacked: on the DAG root, naming the branch and tip
sha); no PR opens.

**Fix rounds.** Send the evidence (gate id, summary, failed criteria with
their evidence, red verify lines) to the same implementer, with a fresh pack
and return path. Re-run the gates from the first failed one; the re-judge
covers the failed criteria plus every `demo:` criterion. `max_fix_rounds` is
the budget; never extend it. When it is spent, route to human:

```bash
uv run devloop release <N>
gh issue edit <N> --remove-label ready-for-agent --add-label <on_gate_failure>
gh issue comment <N> --body "🤖 issue-loop run <run-id>: routed to human at <sha>. <gate evidence table>"
```

The table is §1d's; a failed row carries its failed criteria or red verify
lines, and one sentence says what the fix rounds tried. Continue with the
next issue. There are three exits: ship, ship with findings, route to human.

### 1d. Ship

With `training_mode = true`, stop: show the gate table and push only after
approval (headless: comment the table and worktree path on the issue, never
push). Otherwise:

```bash
git push -u origin <branch>
gh pr create --draft --title "<issue title>" --body "<body>"
gh pr comment <pr-url> --body "<findings comment>"
gh issue comment <N> --body "🤖 issue-loop run <run-id>: shipped at <sha>, PR <pr-url>"
```

Only the route-to-human comment carries a gate table; otherwise the
evidence lives in the PR.

**The PR body carries each fact once:** `Closes #<N>`; one sentence on what
the code now does (not the issue title); the gate table; the demo line; the
findings line; the attribution block. Nothing else.

- The gate table: gate | verdict | summary, one row per gate. A summary
  carries only what varies (`133 lines`, `274 passed`, `5/5`, the failure).
- The demo line: `demo: <short sha>`, the SHA `demo.md` names; none without a
  `demo:` criterion.
- The findings line: `Findings: see comment` or `Findings: none`.

**The findings comment** is one per PR, posted right after `gh pr create`:
a `###` heading per severity present (`problem`, `note`), one bullet per
finding, the judge's sentence verbatim. Each deviation the implementer names
is a bullet under a first heading, `### deviation`. Drop a finding that hedges or names
neither a rule nor an exercised path. A finding already filed as an issue is
its number alone. No findings and no deviations: no comment, and the body
reads `Findings: none`.

**Triage.** Write the gate results to a file and run:

```bash
uv run devloop triage <N> --gates-json <run-dir>/gates-<N>.json \
  --judge-json <run-dir>/judge-<N>.json --baseline <green|red> \
  --fix-rounds <R> --cwd <worktree> --base-ref origin/main
```

Apply the printed `label` with `gh pr edit <pr-url> --add-label <label>`.

**Teardown.** Once the PR is open, from the main checkout: `git worktree
remove <worktree>` (`--force` when only lockfiles are dirty); a lingering
worktree pins its branch. Only a training-mode hold or a route to human keeps
one; list it in the report. In `exhaust`, re-plan now (§0).

### 1e. Stacked delivery

Each DAG component is one stack: one branch, one worktree, one PR at the end.
Components run as concurrent stacks. Differences from the flow above:

- **One branch per stack.** `loop/dag-<N>`, `<N>` the component's root, made
  once (§0.5). Each slice's implementer is fresh but works in this worktree.
  Record the tip sha before each slice.
- **Blockers advance in-branch.** After a slice passes, add it to the
  done-list and re-plan with `plan --dag <N> --assume-done <done-list>`.
  In-branch done is never written to the tracker.
- **Per-slice gates.** `check --gate diff-guard` and the judge's pack take
  `--base-ref <tip before the slice>`. `check --issue` runs on the whole
  branch, so earlier slices stay green.
- **Per-slice comment.** After each slice passes: `gh issue comment <N>
  --body "🤖 issue-loop run <run-id>: slice landed on loop/dag-<root> at
  <sha> — PR at end of run"`. Do not close the issue.
- **Shape at the tip.** With two or more completed slices, run §1c's shape
  posture over all of them before the PR.
- **One PR at the end** (DAG done, cap hit, or a slice routed to human). Its
  title is the epic's or the root's, then the issue numbers in parentheses.
  Its body is §1d's per issue: a `Closes` line and one sentence each, one gate
  table (a row per issue, a column per gate), a demo line per demo, and a
  `Not included` line when part of the DAG remains. One findings comment
  covers the stack. Then `gh issue comment <N> --body "🤖 issue-loop run <run-id>: PR
  <pr-url>"` per issue. Training mode pauses once, here. Then tear down.
- **A failed slice does not poison the stack.** When a slice spends its fix
  rounds, `git reset --hard <tip before the slice>`, route it to human, and
  stop extending this stack. Ship what completed.

<!-- host-extension: memory feed — per-slice records need a Thinkweave vault. Without one, skip this block. -->

- **One trajectory per slice.** Record each slice with `--base-ref <tip
  before the slice>` and `--branch <slice tip>`, so it holds only that
  slice's commits. Put the `judge:shape` stage on the tip issue's
  trajectory.

<!-- /host-extension -->

## 2. Report

Per issue: number, outcome (PR opened, awaiting approval, routed to human),
gate results, findings, fix rounds used. Then the deferred and blocked
issues, the issues newly waiting on a human, and every worktree left behind
(path, branch, why). If nothing shipped, say what unblocks the DAG.

<!-- host-extension: memory feed — everything below needs a Thinkweave vault. Without one the run ends at §2; the tracker, the PR and the report carry the whole record. -->

## 3. Feed the vault

Runs unattended. The worktree is gone, so record from the main checkout:

```bash
uv run --directory <repo-root> devloop trajectory <N> --branch <branch> \
  --gates-json <results-file> --skills-json <dispatch-log> \
  [--primed --served-json <served-file> | --no-primed] [--trace-json <trace-file>] \
  --fix-rounds <R> --outcome <o> --pr-url <url> --run-id <run-id>
```

`--skills-json` lists each dispatched stage as `{id, role, skill, outcome,
fix_rounds_attributed}`; the shape pass is id `judge:shape`, role `judge`.
`skill` is the skill that ran the stage, or empty. Add the join keys you know;
omit the rest, never `0` or `""`:

- `posture`: `writer`, `reader` or `shape`.
- `transport`: `agent-tool`, `herdr` or `headless-argv`.
- `harness`, `model`, `effort`: the values the stage ran with.
- `session_ref`: on herdr, `herdr agent get <name>` →
  `.result.agent.agent_session.value`. The Agent tool exposes none.
- `duration_sec`: whole seconds from dispatch to return.
- `tokens`: the harness's reported total, when it reports one.

`--trace-json` is condensed from the validated returns:

```json
{"reviews": [{"gate": "judge", "finding": "<prose>", "severity": "note",
             "disposition": "accepted", "fixed_by": "<prose>"}],
 "criteria": [{"id": "AC1", "verdict": "met", "flipped_by_round": 1}],
 "edge_cases": ["<prose>"], "deviations": ["<the findings comment's deviation bullets>"],
 "tdd": {"red_confirmed": true}}
```

Then write, per issue:

1. **Insight notes**, one per portable lesson (skip when the run taught
   nothing): `weave_create(type=note, title=…, body=…, project=…,
   session_id=…, frontmatter={"concepts": [<ontology terms>]})`. `concepts`
   nests under `frontmatter=`; a top-level kwarg is silently dropped.
   `project=` is required.
2. **The trajectory note**: body at most 1K chars, What and How it went only:
   `weave_create(type=note, tags=<payload tags>, project=…, session_id=…,
   frontmatter={**<payload frontmatter>, "builds_on": [<insight ids>]})`. MCP
   down: `weave add -f …`.
3. **The round on the epic's task**: write the payload to a file in the run
   directory, then `weave task record-run <payload-file> --trajectory
   <trajectory-id> --project <project> [--session <session-id>]`. A non-zero
   exit, or a task whose `asked` does not name the epic, is a warning in the
   report, never a failed run.

Do not run `/wrap`; the session-note owner covers a loop run.

<!-- /host-extension -->
