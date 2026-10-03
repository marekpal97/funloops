---
name: issue-loop
description: "Drain the ready-for-agent frontier of the GitHub issue DAG: implement each unblocked issue in an isolated worktree, run the configured gate pipeline (diff/tests/verify/judge), and open a draft PR per issue. Headless-safe."
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
the frontier is the parallel set (§1). **exhaust** — re-plan after each shipped issue while the frontier
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
keys by name on both paths.
Then `uv run devloop config <set-flags>` (resolved knobs, the per-role
`dispatch` table, and gates) and `uv run devloop plan <set-flags>` (frontier /
blocked / claimed). Each stage you dispatch is a role, and a role is a
`dispatch.<role>` entry: the table's two (implementer, judge) plus
any the host declares — one more role costs one entry and one section of this
doc, the rule `devloop-boundaries.md` states for gate kinds. The entry's
`posture` shapes its pack and its `transport` picks the dispatch recipe, both
in §1b. Record what the entry named in the stage's dispatch join keys (§3). `plan` reports the
whole frontier; the orchestrator takes the first `max_issues_per_run` of the
reported frontier, in the order reported, and the rest wait for the next run.

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
  reads `red` (TDD encouraged, not enforced), and the tests cell of every PR's
  gate table carries `⚠ degraded-baseline` naming the pre-existing failures.

## 1. Per issue — claim, implement, gate, ship

The DAG frontier is the parallel set: dispatch every frontier issue at once,
each in its own worktree. An issue on the frontier has no open blocker, so no
label or knob holds it back; sibling issues that touch one file may conflict at
merge, and a human resolves that. Stacked delivery runs one stack per DAG
component instead (§1e). For each issue:

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
that lands when the concept guess misses. `--decisions` are the ticket's own
ids merged with the file-anchored ids: read the `dec-*` ids under the issue
body's `## Decisions` heading (the durable why the ticket's three-sentence Why
points at, dec-f5bdf9ea), then walk the files named in the issue body →
`weave_graph(file_path=…, filter='decisions_for_file')`; nothing named or found
→ the same walk for their module/dir; pass the union. Still nothing → let the
fusion carry it.

The rail reads the derived index read-only, fuses both legs over `[loop-run]`
notes with RRF, weights them by outcome, resolves each decision id to its
title and first summary line (an id the index does not hold is listed as
such), and emits JSON: `block` (markdown to
splice), `primed`, `served` (the insight and decision ids surfaced). It always
serves what it finds; there is no held-out run. **Write `block` to a file and pass it to
the pack below as `--prime <file>`.** Empty `block`: splice nothing. Record
`primed` and `served` for §3. With `--buffer` (the loop session's buffer JSONL)
the rail also logs the served ids as a `loop_prime` event the indexer projects
to `context_served(source='loop-prime')`; `--dry-run` suppresses that write.

<!-- /host-extension -->

**The pack — every dispatch of every role, no toggle.** `uv run --directory
<worktree> devloop pack <N> --role <role> [--trace FILE]` composes the
dispatch context; splice its stdout **verbatim** into the prompt. The role's
`dispatch.<role>.posture` shapes it. A `writer` pack (the implementer), in
order: the issue — spliced once, here; never re-read into the prompt;
`## Rules` — the packaged
`constitution.md` beside this file, then the host's own
`docs/agents/constitution.md`, one continuously numbered list (a repo overlay
**extends** the default, **never replaces** it); `## Persona` —
`ponytail-persona.md`, the implementer's write-time posture; the repo map;
`## Standing orders` — the implementer's orders and the codegraph drill-down,
owned by `pack.py` and pinned by its golden. A `reader` pack (the judge, any
reviewing role the host declares) is the same issue, rules and
map, no persona and no standing orders, plus `## Touched modules`: the full
text of every file `--base-ref`...HEAD touches, its lines numbered for
`file:line` citations. The rules are part of its contract (§1c); the persona
is not its business. A `shape` pack (`--posture shape`, the judge's stack-tip
pass, §1c) takes every issue of the stack (`devloop pack <N> <N2> …`): the
issues, the rules, the touched modules, and `## Module edges` from codegraph,
with no repo map. Without codegraph the edges block says, in one line, that
the edges are missing; the module text still ships whole. A role's entry may
list several postures; `--posture` picks one, and the first is the default. `{"error": …}` instead
of a pack means the role has no entry or no posture, or the rules or the
persona did not resolve: STOP and surface it —
dispatching without the rules is the fail-open its own rule names. Amendments
to either layer are a human's PR; `watched_paths` lands any `docs/agents/` diff
in the skim lane. An issue names a file for the pack's tier-2 slice by
backticking its repo-relative path.

**The dispatch is the pack plus three lines.** Whatever the transport, the
dispatch text is, verbatim: the pack, the branch name (`<branch_prefix><N>`),
the baseline line (§0.5: `green` or `red`), and the return file path. Nothing
else: every standing order is the pack's, so one text owns each rule, and the
issue arrives once. A gate role's dispatch adds exactly what §1c names for
it (the judge: the diff and the `check --issue` output) and the implementer's worktree path, which
it works inside and never edits. The return file is the one return channel for every role
on both transports: one path per dispatch under the run directory, made once
per run at `$(git rev-parse --git-common-dir)/devloop/runs/<run-id>/`. It is
never inside the worktree, so a return can never land in the diff, and it
outlives worktree teardown and a reboot. The agent writes its whole return
there and you read the file, never its screen (an inline return gets
truncated; a herdr screen loses alternate-screen output). An implementer
whose issue carries `demo:` criteria also writes the evidence directory
`<return file>.demo/` beside it, as its standing orders say. A fix round
names a fresh path. The role's
`dispatch.<role>.transport` picks the recipe:

- **`agent-tool`.** Dispatch a **fresh implementer agent** with worktree
  isolation (Agent tool, a fresh agent type with `isolation: "worktree"`, the
  entry's `model` when it names one; never the fork type — a fork inherits
  this orchestrator's whole conversation, including context the subagents
  must not see). Its prompt is the dispatch text. A fix round is a SendMessage
  to the same agent: it keeps its context. A gate role (the judge) is a
  fresh agent **without** isolation: it works in the implementer's worktree,
  whose path its dispatch names.
- **`herdr`.** The worktree is a herdr workspace and the agent a named herdr
  session; every command answers in JSON, and the ids come from those answers,
  never from a guess. `harness` is required on this transport (`--kind` has no
  default): an entry without one is a config error you surface, not a guess.
  The entry's `args` must settle who answers the harness's approval prompts
  (the template shows a working tail per harness): a prompt no one answers
  reads as `blocked`. Append `--add-dir <return-dir>` to the tail when the
  harness sandboxes writes, so the return file is writable.

  ```bash
  # implementer: a new worktree, the agent in its root pane
  herdr worktree create --cwd <repo-root> --branch <branch_prefix><N> --base origin/main --label <branch_prefix><N> --no-focus
  #   → .result.worktree.path (the <worktree> every rail call targets),
  #     .result.workspace.workspace_id, .result.root_pane.pane_id
  # gate role (the judge): no new worktree; a new pane beside the implementer's
  herdr pane split <implementer-pane-id> --direction right --cwd <worktree>
  #   → .result.pane.pane_id
  herdr agent start <role>-<N> --kind <harness> --pane <pane-id> -- <args>
  #   the entry's harness and args (the literal argv tail); returns once the agent is ready
  herdr agent prompt <role>-<N> "Your dispatch is <dispatch-file>: read it whole and follow it. Write your return to <return-file>." --wait --timeout <ms>
  ```

  `--cwd <repo-root>` is required: without it herdr resolves the repository
  from the focused workspace, which may be another repo. A herdr worktree
  lives outside the repo tree, but a harness that trusts by repository (Codex)
  keys that trust on `<repo-root>`, never on `<worktree>`.
  Write the dispatch text to `<dispatch-file>` beside the return file first:
  the prompt is the pointer, the file is the same text the Agent tool gets
  inline. `--wait` returns the first settled state. `idle` or `done`: read the
  return file. **A settled agent with no return file failed** — a harness
  error (an unsupported model, an API refusal) settles as `idle` too: route
  to human (§1c's route-to-human block) with `herdr agent read <role>-<N>
  --source recent-unwrapped --lines 120` as the evidence. **`blocked`** — herdr recognised an approval or question UI —
  routes the issue to human (§1c's route-to-human block, with `herdr agent
  read <role>-<N> --source recent-unwrapped --lines 120` as the evidence);
  never answer the dialog yourself. `agent_prompt_stalled` or a timeout is the
  same exit, the error as the evidence. A fix round is `herdr agent prompt` to
  the **same agent name** with the evidence, the re-spliced dispatch file and
  a fresh return path: it keeps its context as SendMessage does. `herdr agent
  get <role>-<N>` carries the session facts §3 records. Teardown:
  `herdr worktree remove --workspace <workspace-id>` is this transport's
  `git worktree remove` (§1d, same `--force` rule); it closes the agent's
  pane with the workspace.

### 1c. Gate pipeline

Run the configured gates **in order**, inside the implementer's worktree.

**The gate split — which plane runs which kind.** `command` and `diff` gates
**execute in the rail**: Python runs the shell command / the diff arithmetic and
returns the verdict. `judge` is never executed by the rail —
*this* orchestrator dispatches a fresh agent for it as its `dispatch.<role>`
entry says (§1b's recipe for its transport). A gate agent's return is the
return file its dispatch names, on either transport; you read the file, never
its screen. The rail's `check` runs the two
deterministic kinds and refuses every other kind — judgment kind or typo alike —
with `gate kind '<k>' is LLM-judged — run it from the /issue-loop command, not
the script`. Protocol detail (the two registries, the shared `GateResult` shape,
execute-vs-validate): `devloop-boundaries.md` §3.

```bash
uv run --directory <worktree> devloop check --gate diff-guard --base-ref origin/main   # the diff gate
uv run --directory <worktree> devloop check --issue <N>                               # command gates, then verify lines
```

`diff-guard` is a forbidden-paths check only; a breach is a fix round, never an overage to approve.

**The check — one call, one result list** (dec-e267d040). `check --issue <N>`
runs every configured `command` gate (the tests gate), then each `verify:` line
the issue body carries, via the shell from the worktree root. It prints
`{issue, results, summary}`: one command `GateResult` per check, a verify line
as `id: verify:<k>`, and a check passes on exit 0. Any red result is
**deterministic not-met**: a fix round, rerun until green or `max_fix_rounds`
is spent. A missing binary is red and named, never skipped.

**The judge — the one LLM judgment stage.** `kind: judge` — dispatch a **fresh
judge subagent** (a fresh agent, no implementation context) as
`dispatch.judge` says, with the judge pack (`uv run --directory <worktree>
devloop pack <N> --role judge --base-ref origin/main`, §1b), `git diff origin/main...HEAD`, the
`check --issue` output, the implementer's evidence directory (`<return
file>.demo/`, §1b) when the issue carries a `demo:` criterion, and its return
file path (§1b: it writes the object below to that file, and that file is
what you hand the rail). Tell it, verbatim:

> The contract is the issue's acceptance criteria, the Interfaces block's
> intent, and rules 3, 6, 7 and 8 of the Rules section. Judge the diff and the
> touched modules against that contract and nothing else. Return one
> verdict per criterion, `"met"` or `"not-met"`, each with one line of
> evidence. A criterion is not met when the code does not do what it says.
> A `verify:` criterion takes its verdict from the `check --issue` output in this
> dispatch: cite that output, never re-run the command.
> Where a criterion is prose and no verify line ran it, run it yourself and
> cite the output. Evidence for a prose criterion is something you ran
> against the code, never only a test the diff adds.
> A `demo:` criterion is scored from the evidence directory only; never re-run
> the demo. Its `demo.md` opens with `sha: <commit>`. The steps it records must
> match the demo text, and that SHA must be the implementation tip (`git
> rev-parse HEAD` in the worktree). Rest the verdict on the artifacts you
> inspect yourself (output, screenshots, DOM dumps), never on the
> implementer's own assessment, and cite the artifact file. No evidence, or
> evidence on another SHA, is `not-met`. Evidence that cannot settle the
> demo's observable is `not-met` too: the evidence line names what the
> evidence lacks.
> Code the diff changes that no longer works as the issue intends is `not-met`
> too, even when no criterion names the case: return it as one more criterion
> under the reserved id `intent`, and only when you have the failure in hand.
> A violation of rule 3, 6, 7 or 8 in a touched module is `not-met` under the
> reserved id `rule:<n>`, one entry per violation. Its evidence cites the
> `file:line` from the Touched modules section and says what breaks the rule.
> Rules 1 and 4 are not yours here, and no other rule blocks.
> Every `not-met` names a command, a test, or output; without one it is not a
> `not-met`, it is a finding. Everything else you notice
> — a risk, a smell, a better design, an edge case outside the contract — is a
> finding with one of two severities: `"problem"` (the code is wrong or
> fragile in a way you can describe but did not demonstrate; a human reads it
> before merge) or `"note"` (style, naming, a cleaner shape). List it, do not
> fail the contract over it. A finding is one sentence naming
> the rule in the Rules section it violates (by number) or the exercised path
> it breaks (the documented verb and the normal state that reaches it); a
> finding that names neither is dropped, not listed. The observation first;
> no clause that withdraws it. Findings never block, whatever their
> severity. Do not search for problems the contract does not name. Return
> exactly this object:

```json
{"criteria": [{"id": "AC1", "verdict": "met", "evidence": "<one line>"},
              {"id": "intent", "verdict": "not-met",
               "evidence": "<the command or test you ran and its output>"}],
 "findings": [{"severity": "note", "finding": "<prose>"}]}
```

`evidence` and `finding` are never blank; `findings` may be empty, not absent.
The rail accepts `intent` as it accepts any criterion id. It accepts a
`rule:<n>` id only for rules 3, 6, 7 and 8, and only with a `file:line` in its
evidence; any other is schema-rejected.
**Only a criterion `not-met` blocks.** Findings
never do, whatever their severity: they go to the PR's findings comment and
the triage lane (§1d) — never a fix round, never an issue.

**Every judgment return is schema-checked before it becomes a verdict.** Hand the return file to the rail before
acting on it, on either transport — `uv run devloop validate --gate <id>
--return-json <return-file>`. The rail emits the same `GateResult` the
deterministic gates emit, plus `reasons`, and exits `0` — schema-valid and
**passed** (judge: no criterion
`not-met`); `1` — schema-valid
and **failed**: a real verdict, run a fix round per the failure flow below; `2`
— **schema-rejected**: `reasons` names each offending field path and value
(`criteria[1].verdict: 'probably' is not one of met | not-met`). **Re-ask**
the same agent with those reasons and a fresh return path (SendMessage on the
Agent tool, `herdr agent prompt <name>` on herdr) — never hand-fix its return,
never read a verdict out of a rejected one, never pass it on to `--gates-json`.
A second rejection is a failed gate: route to human with the reasons.

**The shape posture — rules 1 and 4 and every Interfaces block.** The judge
takes a second posture once per stack tip of two or more slices (§1e), after
the last slice passes its gates. Dispatch a fresh judge as `dispatch.judge`
says, with the shape pack (`uv run --directory <worktree> devloop pack <N>
<N2> … --role judge --posture shape --base-ref origin/main`, every completed
issue of the stack) and its return file path. In `pr-per-issue` delivery no
separate dispatch runs: the single per-slice judge carries both postures. Its
dispatch adds this brief and a second return file for the shape object, and
its pack is the reader pack, which already holds every touched module.
Tell it, verbatim:

> The contract is rules 1 and 4 of the Rules section and every Interfaces
> block in the issues above. Judge the module structure of the touched
> modules against that contract and nothing else. For rule 1, name each
> domain concept the stack touches (a record, a lifecycle, a status rule)
> and every module that holds its logic. One concept's logic spread over
> several modules breaks rule 1, and so does a new module with one consumer;
> adding no module does not by itself keep it. For rule 4, read each touched
> module from its top. It breaks when the top does not show the module's
> flow in a few short names, or when one module holds several jobs side by
> side. Return `"met"` when each concept has one owning module, each module
> reads top-down, and each owns what its Interfaces block declares.
> Otherwise return `"not-met"` with a restructure case. `flow` states the current flow in at most five
> lines and names each broken rule by number. `owns` says, per module, what
> it should own. `options` gives one to three shape options, one sentence
> each. Do not edit code. Return exactly this object:

```json
{"verdict": "not-met",
 "flow": "<at most five lines naming rule 1 and/or 4>",
 "owns": [{"module": "<path>", "owns": "<what it should own>"}],
 "options": ["<shape option>"]}
```

A `met` return may leave `flow` empty and `owns` and `options` as empty lists.
Validate it with `uv run devloop validate --gate judge --posture shape
--return-json <return-file>`: exit `0` met, `1` a restructure case, `2`
schema-rejected (re-ask once, as above). **A restructure case stops the run at
the human.** It never starts a fix round. Route to human (the block below),
the case as the comment's evidence: in stacked delivery on the DAG root
issue, naming the branch and its tip sha, and no PR opens; in
`pr-per-issue` on the issue, before the PR opens.

**On a required-gate failure:** feed the evidence (gate id, summary, detail, the
failed criteria with their evidence, the red verify lines) back to the
implementer subagent (the same agent, by its transport's fix-round line in
§1b — it keeps its context) for a fix round, re-splicing **the pack** and
naming a fresh return path (§1b). An `intent` entry rides that round
like any other criterion. Re-run the pipeline
**from the first failed gate**; the re-judge covers **the failed criteria plus
every `demo:` criterion** (the envelope then carries just those entries) — the
implementer re-ran every demo on the new tip, so no demo verdict rests on an
earlier SHA. It does not re-open other met criteria and does not hunt. `max_fix_rounds` is the budget; you never extend it. After
it is exhausted:

```bash
uv run devloop release <N>
gh issue edit <N> --remove-label ready-for-agent --add-label <on_gate_failure>
gh issue comment <N> --body "🤖 issue-loop run <run-id>: routed to human at <sha>. <gate evidence table>"
```

This is the one issue comment that carries a gate table: no PR exists, so the
issue is the evidence's only home. The table is §1d's; a failed row carries
the failed criteria with their evidence or the red verify lines, and one
sentence under the table says what the fix rounds attempted.

Then continue with the next frontier issue — one stuck issue must not stall the
loop. **Three exits, no others:** **ship** (every criterion met, no findings);
**ship with findings** (criteria met, findings in the PR's findings comment); **route
to human** (a criterion stays `not-met` past `max_fix_rounds`, a judge return
stays schema-rejected after a re-ask, or the shape posture returns a
restructure case).

### 1d. Ship

All required gates green. If `training_mode = true`, STOP here for this issue
and present the gate evidence table to the user; only push/PR after approval
(headless with training_mode on: leave the branch committed in the worktree,
comment the evidence + worktree path on the issue, report — do not push). Else:

```bash
git push -u origin <branch_prefix><N>
gh pr create --draft --title "<issue title>" --body "<body>"
gh pr comment <pr-url> --body "<findings comment>"
gh issue comment <N> --body "🤖 issue-loop run <run-id>: shipped at <sha>, PR <pr-url>"
```

The issue comment is that one line: run id, tip sha, PR link. Issue comments
carry no gate table — the claim comment stays as the rail writes it, and gate
evidence lives in the PR and nowhere else. The one exception is §1c's
route-to-human comment, because no PR exists. The PR title is the issue
title.

**The PR body carries each fact once, and nothing else:** `Closes #<N>`; one
sentence that says what the code now does (not the issue title); the gate
table; the demo line; the findings line; the attribution block.

- The gate table is one table, columns gate | verdict | summary, one row per
  gate. A summary cell carries only what varies: `133 lines`, `274 passed`,
  `5/5`, `8/8`, or the failure named. Never `no forbidden paths`, never
  `exited 0`, never the command text; a pass with nothing to report is the
  number alone.
- The demo line is one line under the table: `demo: <short sha>`, the SHA
  the implementer's `demo.md` names. An issue with
  no `demo:` criterion has no demo line.
- The findings line is `Findings: see comment` or `Findings: none`.
- No summary bullets, no run parameters, no line-count deltas of documents,
  no notes addressed to the orchestrator.

**The findings comment — one per PR, posted right after `gh pr create`.** It
names findings once (dec-f7e7dd53): one `###` heading per severity present,
one bullet per finding, the judge's sentence verbatim. A finding is one
sentence: the observation first, then the rule number or the exercised path in
a trailing clause. No hedge and no self-retraction: a finding that would end
"so no action is needed" or "cosmetic only" is dropped, not softened. A
finding already filed as an issue is the issue number alone. Each deviation
the implementer's return names is one bullet under `### deviation`, one
sentence, ahead of the judge's findings. A PR with no findings and no
deviations gets no comment; its body line reads `Findings: none`. A PR whose
implementer named a deviation is never `Findings: none`.

```markdown
### deviation
- <the deviation and its reason, one sentence>

### problem
- <the observation, then the rule number or the exercised path>

### note
- <one sentence>
```

Keep the `ready-for-agent` label and the assignee — the issue closes on merge;
if the PR is rejected, a human unassigns to re-queue.

**Risk-lane triage — label what a human should look at.** After the PR is
opened, assemble its signal set — you already hold all of it — into a JSON file
and run `uv run devloop triage <N> --signals-json <signals-file>`. The two
**safety-critical** keys are REQUIRED and fail closed — an absent key or an
unrecognized enum value classifies **red** (naming the offending key/value),
because you assemble these signals and enum drift is realistic:

| key               | type      | required | meaning                                             |
| ----------------- | --------- | -------- | --------------------------------------------------- |
| `review_severity` | str       | **yes**  | worst judge finding: `none`/`note`/`problem`        |
| `baseline_green`  | bool      | **yes**  | the tests gate was green on the pristine worktree   |
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
baseline, or any `problem` finding — the
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

One larger piece of work, no intermittent PRs: each DAG component is one stack.
A stack is sequential inside its component; distinct components run as
concurrent stacks, each on its own branch and worktree. `--dag <N>` scopes the
run to one component. Differences from the flow above:

- **One branch, one worktree per stack.** `loop/dag-<N>`, `<N>` the component's
  root, created once from origin/main.
  Each issue's implementer agent is FRESH (§1b) but works in this same worktree,
  stacking commits on the previous slices. Record the tip sha before each issue.
- **Blockers advance in-branch, not by merge.** After an issue passes all gates,
  add it to the done-list and re-plan with `plan --dag <N> --assume-done
  <done-list>` — its dependents become workable immediately.
- **Per-issue gates, scoped diffs.** Run `check --gate diff-guard --base-ref
  <tip-before-this-issue>` so the forbidden-paths check applies per slice; the
  tests gate always runs on the whole branch (earlier slices must stay green —
  that IS the stacking guarantee). The judge sees the per-issue diff (`git diff
  <tip-before>...HEAD`), and its pack takes `--base-ref <tip-before>`.
- **Tracker visibility without PRs.** After each issue passes, one line: `gh
  issue comment <N> --body "🤖 issue-loop run <run-id>: slice landed on
  loop/dag-<root> at <sha> — PR at end of run"`. No gate table: the evidence
  waits for the PR. Do NOT close the issue; do NOT open a PR yet.
- **The shape posture at the tip.** When the stack holds two or more
  completed slices, run §1c's shape posture over all of them before the PR.
  A restructure case stops the run at the human: no PR opens, and the case
  goes on the DAG root issue with the branch and its tip sha. A one-slice
  stack takes the `pr-per-issue` form: its single judge carries both postures.
- **One PR at the end** (DAG exhausted, cap hit, or an issue routed to human):
  push the branch and open a single draft PR. Its title is the epic title or
  the DAG root's title, then the issue numbers in parentheses; never clauses
  joined by semicolons. Its body carries each fact once, and nothing else:
  the `Closes #A` lines for every completed issue; one sentence per issue,
  issue number first, saying what its slice does (not its title, no heading,
  no paragraph); one gate table for the stack, one row per issue and one
  column per gate (diff, tests, verify, judge), each cell the varying number
  only (`133 lines`, `274 passed`, `5/5`, `8/8`) and a failed cell naming the
  failure — no per-issue tables; the demo line per issue that carries a `demo:`
  criterion; `Findings: see comment` or `Findings: none`; a `Not included` line only
  when part of the DAG remains, naming the issues and why; the attribution
  block. No summary bullets, no `###` per issue, no orchestrator notes, no
  run parameters, no line-count deltas of documents. Then §1d's one findings
  comment, covering every completed issue. Then one more line per issue: `gh issue comment
  <N> --body "🤖 issue-loop run <run-id>: PR <pr-url>"`. `training_mode`
  pauses once, here. Then remove the `loop/dag-<N>` worktree (same teardown
  rule as §1d).
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
on user approval. §1d removes the implementer worktree the moment the PR is
open, so record from the main checkout and name the shipped branch: the branch
ref outlives its worktree, and `--branch` reads its commits and files there.
For each processed issue, assemble the deterministic half —

```bash
uv run --directory <repo-root> devloop trajectory <N> --branch <branch> \
  --gates-json <results-file> --skills-json <dispatch-log> [--skill-centric] \
  [--primed | --no-primed] [--served-json <served-ids-file>] \
  [--trace-json <trace-file>] \
  --fix-rounds <R> --outcome <o> --pr-url <url> --run-id <run-id>
```

`<branch>` is the branch §1d shipped: `<branch_prefix><N>`, or `loop/dag-<N>`
in stacked mode.

Mirror the §1b prime verdict: `--primed` with `--served-json` (a JSON list of
the `served` ids the prime emitted) when this issue's implementer received
prime context, or `--no-primed` when nothing was served (`primed: false`);
omitting both keeps the pre-serving shape. `primed`/`served` are facts about
the run, never an experiment arm. `--skills-json` is a list of `{id, role,
skill, outcome, fix_rounds_attributed}` you write, one per stage dispatched
(the implementer subagent, the judge — `kind: judge` gate — the judge's shape
posture as one more entry, role `judge`, id `judge:shape`, and any future
stage); `skill` names the skill that ran the stage, verbatim (`code-review` for a
judge fork), empty when none ran; `fix_rounds_attributed` is how many fix rounds that stage caused
(total: `--fix-rounds`). Omit it for `skills: []`; add `--skill-centric` when
the record is primarily about a skill invocation.

Each entry may also carry the **dispatch join keys** — the facts that later
answer which harness and model ran the stage and how it went. The rail passes
them through verbatim and rejects a wrong type with the field path in
`reasons` (`skills[0].tokens: expected int, got 'many'`). Fill them from what
you know at dispatch and return time, never from a guess:

- `transport` — the role's entry's `transport`, the recipe §1b ran:
  `agent-tool` (the Agent tool), `herdr` (a herdr session), `headless-argv` (a
  harness CLI you ran as a subprocess).
- `harness` — the entry's `harness` (herdr's `--kind`); on the Agent tool, the
  harness you are running in.
- `model` and `effort` — the entry's `model` and `effort`, the values you
  passed on (the Agent tool's `model` argument, the argv tail's model flag).
- `session_ref` — the harness's own session id when the transport exposes one:
  on herdr, `herdr agent get <name>` → `.result.agent.agent_session.value`
  (an id or a path, as its `kind` says); a headless run's session id. The
  Agent tool exposes none: omit it.
- `duration_sec` — whole seconds from dispatch to the stage's return.
- `tokens` — the total tokens the harness reported for the stage, when it
  reports one.

Omit any key you cannot fill, `tokens` and `session_ref` included — never
zeroed or blanked: an absent key means "not recorded", a `0` or `""` would
read as a measurement.

`--trace-json` points at a JSON file **you compose from the gate agents' own
reports** — no new model call: the judge's per-criterion evidence, findings and
verdict flips, the TDD red-confirmation
— the envelopes §1c already validated, condensed into

```json
{"rounds": [{"gate": "judge", "finding": "<prose>", "severity": "note",
             "disposition": "accepted", "fixed_by": "<prose>"}],
 "criteria": [{"id": "AC1", "verdict": "met", "flipped_by_round": 1}],
 "edge_cases": ["<prose>"], "deviations": ["<prose>"],
 "tdd": {"red_confirmed": true}}
```

`deviations` holds the same sentences as the findings comment's
`### deviation` bullets (§1d), one string each.

The rail only accepts and shapes it (unknown keys dropped; a non-dict trace is
rejected). `severity` is `problem` or `note`, the judge envelope's own values.
It lands under the single
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
— `/to-tickets`, `/wayfinder`, an interactive session —
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
