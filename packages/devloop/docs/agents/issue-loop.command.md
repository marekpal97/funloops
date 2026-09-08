---
name: issue-loop
description: "Drain the ready-for-agent frontier of the GitHub issue DAG: implement each unblocked issue in an isolated worktree, run the configured gate pipeline (diff/tests/judge/simplify), and open a draft PR per issue. Headless-safe."
argument-hint: "[issue-number] | --dag <issue> to work one DAG | --stacked | --max-issues <n> | --set key=value | nothing to drain the frontier"
disable-model-invocation: true
---

# Issue Loop — issue → gates → PR

Drain the runnable frontier of the issue DAG. Merged PRs close their issues
(`Closes #N`), which unblocks dependents; the tracker is the state machine.

**The loop's goal** (dec-611cbd8a): high-quality automated development that
keeps scope contained to the task, does not hunt for failures, and is relentless
in fulfilling the initial contract — the issue body: its acceptance criteria
plus the Interfaces block's intent. Nothing outside the contract can fail a PR.

**Install (machine-local).** Dev tooling, not a shipped skill: the symlink into
`.claude/commands/` is never committed (same as the vendored ponytail files
beside it). From the repo root:

```bash
ln -s ../../packages/devloop/docs/agents/issue-loop.command.md .claude/commands/issue-loop.md
```

**The rail** is the `devloop` console script (`python -m devloop` is the same
entry point); command blocks below write `uv run devloop …`, this workspace's
invocation. Every rail call that targets a worktree is `uv run --directory
<worktree> devloop …`: uv runs the rail from the worktree, so the branch's own
`loop.toml` governs its gates (dec-232d2fa4) and `--cwd` defaults to the
worktree. It searches upward from the working directory for
`docs/agents/loop.toml`, stops at the repo root, and falls back to the packaged
copy — the repo being worked on owns its gate pipeline; `devloop config` prints
what resolved. Config: `loop.toml` beside this file (a new host repo starts from
`loop.toml.template`). Semantics: `issue-loop.md`; tracker conventions:
`issue-tracker.md`; label vocabulary: `triage-labels.md`.

`run_mode`: **pass** — one pass over the frontier, up to `max_issues_per_run`;
issues from **distinct DAG components** (`component` in `plan` output) may run
in parallel. **exhaust** — re-plan after each shipped issue while the frontier
is non-empty (still capped); it widens only as PRs merge, so this pairs with a
human merging as you ship. Dry with blocked issues left: report, never busy-wait.

**Optional host extension: the memory feed.** Where the host repo has a
Thinkweave vault, the loop primes each implementer from prior runs and writes
back what happened, in stretches delimited by `host-extension` HTML comments.
**Without a memory host, skip every marked block** — the loop below is complete
and runs unchanged.

## 0. Resolve config and plan

**Per-run overrides.** `loop.toml` holds the *defaults*; the arguments set this
run's *posture*. Translate sugar flags — `--stacked` → `--set delivery=stacked`,
`--max-issues <n>` → `--set max_issues_per_run=<n>` — and pass any explicit
`--set [section.]key=value` through verbatim. Append the collected `--set` flags
to **every** `devloop` invocation in this run, so rail and orchestrator see the
same effective config. Never edit `loop.toml` on the user's behalf. Gates are
file-only (a trust boundary, not a run-time posture); the rail rejects unknown
keys by name on both paths, and `--stacked` without `--dag` is an error per §1e.
Then `uv run devloop config <set-flags>` (resolved knobs + gates) and `uv run
devloop plan <set-flags>` (frontier / blocked / claimed).

If the user passed an issue number, the frontier is just that issue (still
verify via `plan` that it is unblocked and unclaimed — if not, say so and stop).
If the user passed `--dag <N>`, scope every `plan` call with `--dag N` — the run
works only that DAG component and `run_mode` defaults to `exhaust`. If the
frontier is empty, report why (all blocked? all claimed? PRs awaiting human
merge?) and stop. Generate a run id: `loop-<YYYYMMDD>-<4 random hex>`.

## 0.5 Baseline probe (once per run)

Create the first implementer worktree, and **before any edits** run the tests
gate in it (pristine = origin/main state): `uv run --directory <worktree>
devloop check --gate tests`. The verdict is the **baseline line** of every
implementer dispatch (§1b): `green` or `red`, the probe's colour under
`tdd.mode = auto`; `always` writes `green` and `never` writes `red`, whatever
the probe said.

- **Green** → proceed. The pack's standing orders enforce TDD against a
  `green` baseline line.
- **Red** and `require_green_baseline = true` (default) → **stop before
  implementing anything.** Name the open issue that owns the failure ("baseline
  red — fix #N first"). Training mode: ask the user; headless: refuse.
- **Red** and `require_green_baseline = false` → proceed degraded: the tests
  gate is scoped to the implementer's declared test targets, the baseline line
  reads `red` (TDD encouraged, not enforced), and every PR body carries `⚠ degraded-baseline` naming the
  pre-existing failures.

## 1. Per issue — claim, implement, gate, ship

Process frontier issues **sequentially** by default. If `max_parallel > 1` AND
every picked issue has `parallel_safe: true`, you may dispatch implementer
subagents concurrently, each in its own worktree, at most `max_parallel` at
once and **never two issues from the same DAG `component`** (one DAG is chased
sequentially, whatever the labels say). For each issue:

### 1a. Claim (control-plane visibility)

`uv run devloop claim <N> --run-id <run-id>` — the assignee is the claim; it
renders natively in the tracker UI.

### 1b. Implement

<!-- host-extension: memory feed — needs a Thinkweave vault. Without one, skip to "Dispatch blocks" below; the implementer is dispatched unprimed, which is exactly what `primed=false` records. -->

**Prime from prior trajectories (claim-time).** Before spawning the implementer,
fetch the reusable half of prior similar runs — the insight notes prior
trajectories link via `builds_on`:

```bash
uv run devloop prime <N> --run-id <run-id> \
  --concepts "<2-3 ontology terms>" --query "<the issue's title (+ body)>" \
  [--decisions "<comma-separated note ids>"] --vault <vault-root> \
  [--buffer <weave_dir>/buffer/<this-session-id>.jsonl] <set-flags>
```

**You resolve the three signals; the rail fuses them.** `--concepts` are
ontology terms, never GitHub labels (labels match zero trajectory notes — the
dead-join diagnosis and the host overlay doc's location are in
`devloop-boundaries.md` §4): map the issue to 2–3 terms via `weave_concepts`; a
labels-only call with no `--query` stamps a warning in the payload's `note`.
`--query` is the issue's own text (title, or title + body): the full-text leg
that lands when the concept guess misses. `--decisions` are file-anchored ids:
files named in the issue body → `weave_graph(file_path=…,
filter='decisions_for_file')`; nothing named or found → the same walk for their
module/dir; still nothing → let the fusion carry it.

The rail reads the derived index read-only, fuses both legs over `[loop-run]`
notes with RRF, weights them by outcome, and emits JSON: `block` (markdown to
splice), `primed`, `served` (the insight and decision ids surfaced). It always
serves what it finds; there is no held-out run. **Write `block` to a file and pass it to
the pack below as `--prime <file>`**, and add this
standing order: *Check prior decisions for every file you touch
(`weave_graph(file_path=…, filter='decisions_for_file')`; fall back to `weave
decisions --file <path>` if MCP is absent). Do not re-litigate a settled
decision — surface conflicts instead.* Empty `block`: splice nothing. Record
`primed` and `served` for §3. With `--buffer` (the loop session's buffer JSONL)
the rail also logs the served ids as a `loop_prime` event the indexer projects
to `context_served(source='loop-prime')`; `--dry-run` suppresses that write.

<!-- /host-extension -->

**The pack — every implementer and fix-round dispatch, no toggle.** `uv run
--directory <worktree> devloop pack <N> --role implementer [--trace FILE]`
composes the dispatch context; splice its stdout **verbatim** into the
implementer prompt. Its sections, in order: the issue — spliced once, here;
never re-read into the prompt; `## Rules` — the packaged
`constitution.md` beside this file, then the host's own
`docs/agents/constitution.md`, one continuously numbered list (a repo overlay
**extends** the default, **never replaces** it); `## Persona` —
`ponytail-persona.md`, the implementer's write-time posture; the repo map;
`## Standing orders` — the implementer's orders and the codegraph drill-down,
owned by `pack.py` and pinned by its golden. The judge's
copy is `--role judge`: the same issue, rules and map, no persona and no
standing orders — the rules are the shape standard its findings cite (§1c),
the persona is not its business. `{"error": …}` instead of
a pack means the rules or the persona did not resolve: STOP and surface it —
dispatching without the rules is the fail-open its own rule names. Amendments
to either layer are a human's PR; `watched_paths` lands any `docs/agents/` diff
in the skim lane. An issue names a file for the pack's tier-2 slice by
backticking its repo-relative path.

**The dispatch is the pack plus two lines.** Dispatch a **fresh implementer
agent** with worktree isolation (Agent tool, a fresh agent type with
`isolation: "worktree"`; never the fork type — a fork inherits this
orchestrator's whole conversation, including context the subagents must not
see). Its prompt is, verbatim: the pack, the branch name
(`<branch_prefix><N>`), and the baseline line (§0.5: `green` or `red`).
Nothing else: every standing order is the pack's, so one text owns each rule,
and the issue arrives once. A report longer than a screen is written to a
file in the worktree and its path returned; an inline return that long gets
truncated.

### 1c. Gate pipeline

Run the configured gates **in order**, inside the implementer's worktree.

**The gate split — which plane runs which kind.** `command` and `diff` gates
**execute in the rail**: Python runs the shell command / the diff arithmetic and
returns the verdict. `judge` and `simplify` are never executed by the rail —
*this* orchestrator dispatches a fresh agent for each (§1b). A gate agent's
report longer than a screen is written to a file in the
worktree and its path returned; you read the file. The rail's `check` runs the two
deterministic kinds and refuses every other kind — judgment kind or typo alike —
with `gate kind '<k>' is LLM-judged — run it from the /issue-loop command, not
the script`. Protocol detail (the two registries, the shared `GateResult` shape,
execute-vs-validate): `devloop-boundaries.md` §3.

```bash
uv run --directory <worktree> devloop check --gate <id> --base-ref origin/main   # command | diff
uv run --directory <worktree> devloop check --issue <N>                          # the verify rail
```

`diff-guard` is a forbidden-paths check only; a breach is a fix round, never an overage to approve.

**The verify rail — the issue's own runnable criteria** (dec-2f5bf66a). After the
tests gate, `check --issue <N>` runs every `verify:` line the body carries via the
shell from the worktree root, one command `GateResult` per line (`id: verify:<k>`;
pass = exit 0 plus, after the line's LAST ` => `, that text in stdout), as `{issue,
results, summary}`; no lines = `no verify lines`, exit 0. Re-running `check --issue`
on an issue already being verified is a fixed point (excluded, counted); a runaway
cycle is one red result naming it. Red = **deterministic not-met**: a fix round,
rerun until green or `max_fix_rounds` is spent; a missing binary is red, named, never skipped.

**The judge — the one LLM judgment stage.** `kind: judge` — dispatch a **fresh
judge subagent** (a fresh agent, no implementation context) with the judge
pack (`uv run --directory <worktree> devloop pack <N> --role judge`, §1b: the issue with its
acceptance criteria and Interfaces block, the `## Rules` section, the repo
map), `git diff origin/main...HEAD`, and the test and verify-rail output. Tell
it, verbatim:

> The contract is the issue's acceptance criteria plus the Interfaces block's
> intent. Judge the diff against that contract and nothing else. Return one
> verdict per criterion, `"met"` or `"not-met"`, each with one line of
> evidence. A `not-met` must cite the criterion and the observable — a
> command's output, a test, a diff line; if you cannot cite one, it is not a
> `not-met`. Where a criterion is prose for an executable outcome and no verify
> line ran it, run it yourself and cite the output. Everything else you notice
> — a risk, a smell, a better design, an edge case outside the contract — is a
> finding with a severity (`"critical"` / `"major"` / `"minor"` / `"nit"`):
> list it, do not fail the contract over it. A finding names the rule in the
> Rules section it violates (by number) or the exercised path it breaks (the
> documented verb and the normal state that reaches it); a finding that names
> neither is dropped, not listed. Findings never block, whatever their
> severity. Do not search for problems the contract does not name. Return
> exactly this object:

```json
{"criteria": [{"id": "AC1", "verdict": "met", "evidence": "<one line>"}],
 "findings": [{"severity": "minor", "finding": "<prose>"}]}
```

`evidence` and `finding` are never blank; `findings` may be empty, not absent.
**Only a criterion `not-met` blocks.** Findings never do, whatever their
severity: they go to the PR body under *Findings*, the triage lane (§1d) and
the follow-up path — never a fix round.

**Every judgment return is schema-checked before it becomes a verdict.** (For
`simplify` the **vendored** skill owns its output format, a prose delete-list;
you condense it into the envelope.) Write the JSON to a file and hand it to
the rail before acting on it — `uv run devloop validate --gate <id>
--return-json <return-file>`. The rail emits the same `GateResult` the
deterministic gates emit, plus `reasons`, and exits `0` — schema-valid and
**passed** (judge: no criterion
`not-met` per the gate's `threshold`, `all` or `majority`); `1` — schema-valid
and **failed**: a real verdict, run a fix round per the failure flow below; `2`
— **schema-rejected**: `reasons` names each offending field path and value
(`criteria[1].verdict: 'probably' is not one of met | not-met`). SendMessage
those reasons to the same subagent and **re-ask** — never hand-fix its return,
never read a verdict out of a rejected one, never pass it on to `--gates-json`.
A second rejection is a failed gate: route to human with the reasons.

**The simplify stage (`kind: simplify`, after the judge).** Runs **last, only
after every required gate is green**, and is safe by construction: it can only
*shrink* the verified diff. `required = false` — its "failure" mode is a revert.

1. **Snapshot the tip.** `pre=$(git -C <worktree> rev-parse HEAD)` (per-slice in
   stacked mode, so a revert only unwinds the trim, never prior slices).
2. **Get the delete-list.** Dispatch a **fresh subagent** with the text of the
   **vendored** `ponytail-review.command.md` skill beside this file (host
   `/simplify` is the fallback) and the slice diff — `git diff
   origin/main...HEAD`; in stacked mode `git diff <tip-before-this-issue>...HEAD`
   per §1e. Condense its delete-list and `net: -<N> lines possible` tally into
   `{"outcome": "applied", "lines_delta": -<N>, "cuts": [{"what": …, "why":
   …}], "kept": [{…}]}` and `validate` it. `outcome` is `"lean"` when the
   subagent said `Lean already. Ship.` (skip the rest, note "simplify: lean
   already" in the PR body); `"applied"` when you apply the delete-list;
   `"reverted"` is step 4's terminal value after a red re-verify.
3. **Apply** the delete-list as a single commit on the branch.
4. **Re-verify and keep or revert.** Re-run the gate's `rerun` list (`tests`
   via the rail, then `judge` via a fresh judge subagent) on the shrunk diff.
   Both green → keep, noting `simplify: -<N> lines, tests+judge green` in the PR
   body. Either red → `git -C <worktree> reset --hard $pre`, ship the
   **pre-simplify** diff, and add the gate's `revert_note`
   (`⚠ simplify-reverted`) to the PR body naming the failing gate.

**On a required-gate failure:** feed the evidence (gate id, summary, detail, the
failed criteria with their evidence, the red verify lines) back to the
implementer subagent (SendMessage to the same agent — it keeps its context) for
a fix round, re-splicing **the pack** (§1b). Re-run the pipeline
**from the first failed gate**; the re-judge covers **only the failed criteria**
(the envelope then carries just those entries) — it does not re-open met ones
and does not hunt. `max_fix_rounds` is the budget; you never extend it. After
it is exhausted:

```bash
uv run devloop release <N>
gh issue edit <N> --remove-label ready-for-agent --add-label <on_gate_failure>
gh issue comment <N> --body "<gate evidence table + what was attempted>"
```

Then continue with the next frontier issue — one stuck issue must not stall the
loop. **Three exits, no others:** **ship** (every criterion met, no findings);
**ship with findings** (criteria met, findings listed in the PR body); **route
to human** (a criterion stays `not-met` past `max_fix_rounds`, or a judge return
stays schema-rejected after a re-ask).

### 1d. Ship

All required gates green. If `training_mode = true`, STOP here for this issue
and present the gate evidence table to the user; only push/PR after approval
(headless with training_mode on: leave the branch committed in the worktree,
comment the evidence + worktree path on the issue, report — do not push). Else:

```bash
git push -u origin <branch_prefix><N>
gh pr create --draft --title "<issue title> (#<N>)" --body "<body>"
gh issue comment <N> --body "🤖 issue-loop run <run-id>: PR <url> opened. <gate table>"
```

PR body must contain: `Closes #<N>`, a summary of the change, the gate evidence
table (gate | verdict | summary), a *Findings* list (severity + finding, from
the judge; "none" when empty), and the standard Claude Code attribution line.
Keep the `ready-for-agent` label and the assignee — the issue closes on merge;
if the PR is rejected, a human unassigns to re-queue. **No stack-tip simplify
here — a documented no-op:** a pr-per-issue branch holds one slice, so §1c's
simplify already ran at what IS the stack tip (the whole-branch pass is §1e's).

**Risk-lane triage — label what a human should look at.** After the PR is
opened, assemble its signal set — you already hold all of it — into a JSON file
and run `uv run devloop triage <N> --signals-json <signals-file>`. The three
**safety-critical** keys are REQUIRED and fail closed — an absent key or an
unrecognized enum value classifies **red** (naming the offending key/value),
because you assemble these signals and enum drift is realistic:

| key               | type      | required | meaning                                             |
| ----------------- | --------- | -------- | --------------------------------------------------- |
| `review_severity` | str       | **yes**  | worst judge finding: `none`/`minor`/`major`/`critical` |
| `baseline_green`  | bool      | **yes**  | the tests gate was green on the pristine worktree   |
| `acceptance`      | str       | **yes**  | judge criteria verdict: `met`/`uncertain`/`not-met` |
| `fix_rounds`      | int       | no (→0)  | implement→gate→fix iterations (0 = first try)       |
| `diff_lines`      | int       | no (→0)  | total changed lines (the diff-guard gate's count)   |
| `files_touched`   | list[str] | no (→[]) | repo-relative paths the PR changed                  |
| `tests_touched`   | bool      | no (→F)  | the change carries test coverage                    |

The rail returns `{issue, lane, label, reasons}` — red wins, `reasons` lists
every triggered rule. **You** apply the label via gh (`gh issue edit <N>
--add-label <label>`, or `gh pr edit`). **yellow** (`review-light`) is the
default lane: a human skims, the reasons (fix rounds, a watched path, no
coverage signal) telling them where to look; there is no auto-merge lane.
**red** (`ready-for-human`): sensitive path (always), big diff, degraded
baseline, `major`/`critical` finding, or uncertain/not-met judge verdict — the
`on_gate_failure` label reused deliberately, the same "human, please look" rung
as a gate failure. Thresholds and the sensitive-path list are `[triage]` knobs
in `loop.toml`, per-host, never hardcoded.

**Teardown.** Once the PR is open, from the main checkout: `git worktree remove
<worktree>` (`--force` when the only dirt is regenerated lockfiles). A lingering
worktree pins its branch, and git then refuses every human attempt to check the
PR out (the VS Code PR extension fails with "error switching to pull request").
Only the evidence paths — `training_mode` headless holds and gate-failure
routing — keep a worktree, and those are listed in the run report (§2). In
`run_mode = exhaust`: after shipping, re-run `plan` and continue with any new
frontier issues until the per-run cap; otherwise report and stop.

### 1e. Stacked delivery (`delivery = stacked`)

One larger piece of work, no intermittent PRs. Requires a `--dag <N>` scope and
is sequential (`max_parallel` is ignored). Differences from the flow above:

- **One branch, one worktree.** `loop/dag-<N>`, created once from origin/main.
  Each issue's implementer agent is FRESH (§1b) but works in this same worktree,
  stacking commits on the previous slices. Record the tip sha before each issue.
- **Blockers advance in-branch, not by merge.** After an issue passes all gates,
  add it to the done-list and re-plan with `plan --dag <N> --assume-done
  <done-list>` — its dependents become workable immediately.
- **Per-issue gates, scoped diffs.** Run `check --gate diff-guard --base-ref
  <tip-before-this-issue>` so the forbidden-paths check applies per slice; the
  tests gate always runs on the whole branch (earlier slices must stay green —
  that IS the stacking guarantee). The judge and the per-slice simplify subagent
  see the per-issue diff (`git diff <tip-before>...HEAD`) — cross-slice trimming
  belongs to the stack-tip pass below.
- **Tracker visibility without PRs.** After each issue passes: `gh issue comment
  <N> --body "🤖 issue-loop run <id>: slice landed on loop/dag-<root> at <sha> —
  PR at end of run. <gate table>"`. Do NOT close the issue; do NOT open a PR yet.
- **Stack-tip simplify — whole-branch ponytail review before PR-open.** The
  per-slice gate cannot see cross-slice redundancy (a later slice re-rolling an
  earlier slice's helper). So once the stack is final — DAG exhausted, cap hit,
  or an issue routed to human — run the §1c simplify flow ONCE more over the
  whole branch, before pushing anything. Same gate entry, same steps, differing
  in two inputs: the diff is the **cumulative merge-base diff** `git diff
  origin/main...HEAD`, and the subagent also receives the **whole-file** contents
  of every touched file (`git diff --name-only origin/main...HEAD`, then read
  each) so it can judge duplication across slices. Keep-or-revert reuses the
  gate config: snapshot `pre=$(git rev-parse HEAD)`, apply the delete-list as
  one commit, re-run the gate's `rerun` list — `tests` on the whole branch via
  the rail, `judge` as one fresh judge over EVERY completed issue's criteria
  against the cumulative diff, per the gate's `threshold` — and on any red `git
  reset --hard $pre` and add the gate's `revert_note` (`⚠ simplify-reverted`,
  suffixed `(stack-tip)`) to the PR body. Slices that **individually passed**
  simplify can still receive cuts here — that is the point. Note the win
  (`stack-tip simplify: -<N> lines, tests+judge green`) or the revert.
  <!-- host-extension: memory feed — needs a Thinkweave vault. -->
  Record the result in the final completed issue's §3 trace under
  `stack_simplify` (same envelope as the per-slice `simplify` key).
  <!-- /host-extension -->
- **One PR at the end** (DAG exhausted, cap hit, or an issue routed to human):
  push the branch and open a single draft PR whose body carries `Closes #A`
  lines for every completed issue, the per-issue gate tables and findings, and
  — if some of the DAG remains — which issues are NOT included and why.
  `training_mode` pauses once, here. Then remove the `loop/dag-<N>` worktree
  (same teardown rule as §1d).
- **A failed issue doesn't poison the stack.** If an issue exhausts its fix
  rounds, reset the branch to the last good tip (`git reset --hard
  <tip-before-this-issue>`), route it to human as usual, and stop extending this
  DAG (independent siblings may continue). Ship the PR with what completed.

## 2. Report

Per issue: number, outcome (PR opened / awaiting approval / routed-to-human),
gate results, findings, fix rounds used. Plus: frontier remaining, issues newly
blocked-on-human, and **every worktree left behind** (evidence paths only — path
+ branch + why), so a human can `git worktree remove` them after acting on the
evidence. If nothing shipped, say what unblocks the DAG (usually: merge loop PRs).

<!-- host-extension: memory feed — everything below needs a Thinkweave vault on the host. Without one the run ends at §2: the tracker, the PR, and the report already carry the complete record, and nothing is written back. The host's overlay doc is located in devloop-boundaries.md section 4. -->

## 3. Feed the vault — write one trajectory note per processed issue

**Optional host extension.** Runs unattended where a vault exists — do not gate
on user approval. For each processed issue, assemble the deterministic half —

```bash
uv run --directory <worktree> devloop trajectory <N> \
  --gates-json <results-file> --skills-json <dispatch-log> [--skill-centric] \
  [--primed | --no-primed] [--served-json <served-ids-file>] \
  [--trace-json <trace-file>] \
  --fix-rounds <R> --outcome <o> --pr-url <url> --run-id <run-id>
```

Mirror the §1b prime verdict: `--primed` with `--served-json` (a JSON list of
the `served` ids the prime emitted) when this issue's implementer received
prime context, or `--no-primed` when nothing was served (`primed: false`);
omitting both keeps the pre-serving shape. `primed`/`served` are facts about
the run, never an experiment arm. `--skills-json` is a list of `{id, role,
outcome, fix_rounds_attributed}` you write, one per stage dispatched (the
implementer subagent, the judge — `kind: judge` gate — and any future stage);
`fix_rounds_attributed` is how many fix rounds that stage caused (total:
`--fix-rounds`). Omit it for `skills: []`; add `--skill-centric` when the
record is primarily about a skill invocation.

`--trace-json` points at a JSON file **you compose from the gate agents' own
reports** — no new model call: the judge's per-criterion evidence, findings and
verdict flips, the simplify gate's cut/keep rationale, the TDD red-confirmation
— the envelopes §1c already validated, condensed into

```json
{"rounds": [{"gate": "judge", "finding": "<prose>", "severity": "minor",
             "disposition": "accepted", "fixed_by": "<prose>"}],
 "criteria": [{"id": "AC1", "verdict": "met", "flipped_by_round": 1}],
 "simplify": {"outcome": "applied", "lines_delta": -12,
              "cuts": [{"what": "<prose>", "why": "<prose>"}], "kept": [{…}]},
 "stack_simplify": {"<same envelope as simplify>": "…"},
 "edge_cases": ["<prose>"], "tdd": {"red_confirmed": true}}
```

The rail only accepts and shapes it (unknown keys dropped; a non-dict trace is
rejected). `stack_simplify` records the §1e stack-tip pass, at most once per
stacked run, on the **final completed issue's** trajectory — which issue is
final is orchestrator knowledge the rail never holds. It lands under the single
`trace` frontmatter key — the machine-readable half of the tracker's gate
evidence, not a second prose owner. Omit `--trace-json` and the key is absent.

**Mint portable lessons as insight notes, then link them.** The trajectory body
is the run-causal register only (What / How it went) — there is
`no Lessons section`. The reusable wisdom a *future* run would apply is minted
as separate **insight notes** at ship time (concepts at creation, from the
ontology — `weave_concepts` first), then linked from the trajectory via
`builds_on`, which is how prime serves them. The register test that sorts every
artifact: `run-bound semantic trace` → the trajectory's `trace`; a
`portable lesson` → an `insight note`, linked; an enumerable fact → a
`frontmatter key`. Compose, per issue:

1. **Insight notes** for the portable lessons (skip when the run taught nothing
   reusable): `weave_create(type=note, title=…, body=…, project=…,
   session_id=…, frontmatter={"concepts": [<ontology terms>]})`. The MCP schema
   accepts only `type/title/body/project/tags/frontmatter/session_id` — extra
   top-level kwargs are **silently dropped**, so `concepts` MUST nest under
   `frontmatter=`. `project=` is NOT optional: without it (and when `session_id`
   doesn't resolve — e.g. headless runs before the session note exists) the
   writer drops the note as a bare file at the vault's `projects/` root.
2. **The trajectory note** — body ≤1K chars (What / How it went only), then
   `weave_create(type=note, tags=<payload tags>, project=…, session_id=…,
   frontmatter={**<payload frontmatter>, "builds_on": [<insight ids>]})` (same
   nesting, same dropped-kwarg trap). The payload's `tags` already carry
   `loop-run` (plus `skill-invocation` when `--skill-centric`). If MCP is down,
   fall back to `weave add -f …`.

Do not duplicate gate evidence or run history — the tracker and PR own those.

## 4. Wrap coverage — do NOT run `/wrap` here

**Optional host extension**, and it is a *don't*: headless loop runs are
wrap-covered without an explicit run-end `/wrap` — the `SessionStart` hook mints
this run's session note and the nightly `/dream` `dream-wrap-worker` catch-up
synthesises + `weave wrap-finalize`s it; the per-issue content is already in
the §3 trajectory notes. Session synthesis and **decision promotion** belong to
the session-note owner; a loop that minted decisions would break the
single-owner rule. See [`vault-issue-contract.md`](vault-issue-contract.md).

<!-- /host-extension -->

## 5. Board hygiene — `devloop board doctor` / `sweep`

The tracker is the loop's input contract (§0 reads it as a DAG), so its grammar
is enforced by the same rail — a read-only lint plus a mechanical sweep, run
across every repo that installs devloop (funloops#9). **Conventions, in one
table** — each row is a `doctor` check; the last column says whether the fix is
mechanical (a sweep op) or a human verdict (finding only):

| convention | check | fix |
| --- | --- | --- |
| Epic membership = native **sub-issue**; anything with sub-issues carries the `epic` label | `epic-unlabelled` | op `add_label` |
| Epics group, they never run — no runnable rung on an epic | `epic-runnable` | op `remove_label` |
| The epic is **blocked-by every open child** (anchor: `plan --dag <epic>` scopes to the tree, epic closes last) | `epic-unanchored` | op `add_blocker` |
| Epic closes when its last child closes, or gets an explicit re-scope comment | `epic-delivered` | human |
| Ordering = native **blocked-by**; a body `Blocked-by: #N` header with no native twin is the mint-time gap | `text-only-blocker` | op `add_blocker` (never when #N is the issue's own parent — that is the inverted-root error, flagged only) |
| Titles describe the work; `W1a:` / `A3:` / `S5:` prefixes are retired once a native edge carries the order, or the issue is closed (no order left to encode); `EPIC:` / `PRD:` prefixes go once the `epic` label is on | `title-order-prefix` | op `retitle` |
| Exactly **one** triage rung per open non-epic issue (`triage-labels.md` table) | `rung-contradictory` (error) / `rung-missing` (warn) | human / op `add_label needs-triage` (the rung that asserts only "no verdict yet" — it queues the issue for `/triage`) |
| `track:*` = subsystem lane, on every open issue where the repo uses lanes | `track-missing` | human |
| The repo's label set carries the whole triage table + `epic` + the loop's `[labels]`, and none of GitHub's boilerplate five | `label-missing` / `label-boilerplate` | op `create_label` / `delete_label` |
| Cross-repo edges are legal (multi-repo DAGs share one substrate) but a single-repo `plan` cannot see them | `cross-repo-edge` | info |
| Runnable, unblocked, unassigned and untouched for 14 days — re-verify it | `runnable-idle` | info |

**Mint-time rule** (the other half of the contract): any route that mints a DAG
— `/to-tickets`, `/wayfinder`, an interactive session, the §1d follow-up path —
publishes native edges + the runnable label at creation; `doctor` catches a
route that forgot.

```bash
uv run devloop board doctor --repo owner/a --repo owner/b   # JSON report; exit 1 on any error
uv run devloop board sweep  --repo owner/a                  # print the op plan (dry run)
uv run devloop board sweep  --repo owner/a --apply [--only create_label,add_label]
```

`--repo` is repeatable and defaults to the cwd's clone. The sweep only ever
executes ops the pure layer emitted; it cannot close an issue, pick a rung, or
choose a track — those stay findings for a human (or a `/triage` session).
Safe to run unattended: the weekly slow loop runs `doctor` across all boards
and `sweep --apply` for the op kinds listed in its cron line.
