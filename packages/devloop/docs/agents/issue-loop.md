# The issue-to-PR loop

The first loop in this workspace: issues labeled `ready-for-agent` flow through
implement → gates → draft PR without hand-prompting. Day shift plans the backlog
(`/grill-with-docs` → `/to-spec` → `/to-tickets`, or `/wayfinder` for foggy
multi-session efforts); this loop is the night shift.

Surfaces:

- **the `devloop` package** (`uv run devloop`, or `python -m devloop`) —
  deterministic rail, stdlib-only. DAG snapshot, frontier computation,
  claim/release, command+diff gates. Package map: `devloop-boundaries.md` §2.
- **`loop.toml`** beside this file — every tunable: run caps, fix rounds,
  training mode, label names, and the gate pipeline. A new host repo starts
  from `loop.toml.template`, which carries the same knobs with nothing
  host-specific filled in.
- **`/issue-loop`** (`issue-loop.command.md`) — the orchestrator: dispatches
  implementer/judge subagents, owns all control-plane writes. Dev
  tooling, not a shipped skill: installed by an untracked symlink; see the
  command doc's **Install** block.

The loop runs with or without a memory host. Where the host repo has a
Thinkweave vault, the command doc's marked `host-extension` blocks add
claim-time priming from prior runs and a per-issue write-back; without one they
are skipped and nothing else changes.

## How the issue DAG becomes a script

**The tracker is the DAG.** Since Pocock skills v1.1.0, `/to-tickets` and
`/wayfinder` publish blocking as **GitHub-native issue dependencies** — the
canonical, UI-visible representation — and since #95 that is the *only*
representation the rail reads. Nothing else stores the graph; there is no plan file to rot. GitHub even
maintains the live gate for us: `issue_dependencies_summary.blocked_by`
counts open blockers natively, so an edge to a blocker outside this repo's
snapshot still blocks (the plan warns which).

**The rail computes only the frontier.** `devloop plan` re-reads all
issues each run and partitions the `ready-for-agent` set into:

- **frontier** — open, unclaimed, every blocker CLOSED → runnable now;
- **blocked** — some blocker still open (listed);
- **claimed** — has an assignee (the wayfinder convention: the assignee IS
  the claim) or the legacy claim label. Either way visible in the tracker UI,
  so trivially auditable.

No full topological sort is ever needed. Each PR body says `Closes #N`, so a
human merging a PR closes its issue, which moves the next rank of the DAG
into the frontier for the next run. GitHub's own issue state machine *is*
the DAG executor; the loop is stateless between runs (the Ralph principle:
static prompt, evolving environment — here the environment is the tracker).

**The DAG sets parallelism and scope.** `plan` computes the
**weakly-connected components** of the open-issue graph (component id =
smallest issue number in it), and `plan --dag <N>` restricts a run to one.
How a run uses them — the frontier as the parallel set, `--set`, `run_mode`,
`delivery` — is the command doc's (`issue-loop.command.md` §0, §1, §1e).

This is Pocock's Sandcastle planner made deterministic: he uses an LLM to
pick parallelizable issues, but since `/to-tickets` already encodes the
edges, scheduling is plain graph math in Python — LLM judgment is reserved
for implementation and evaluation, never for scheduling. A wide refactor
ticketed as expand–contract arrives here as an ordinary linear blocked
chain and needs nothing special from the loop.

## How success is defined: the gate pipeline

Evaluation is an **ordered list of typed gates** in `loop.toml`. The closed
set of *kinds* lives in code; the open set of *instances* is pure config —
adding another `command` gate (a linter, a contract test, a benchmark
threshold) touches no code.

| kind | what it checks |
|---|---|
| `diff` | forbidden paths (no line cap) |
| `command` | any shell command; pass = exit 0 — the tests gate; `check --issue <N>` runs every command gate, then the issue's own `verify:` lines, as one list |
| `judge` | the one judgment stage: per-criterion verdicts against the issue's acceptance criteria, all of which must be met, plus advisory findings that never block (dec-611cbd8a) |

Which plane runs each kind is the Gate protocol's (`devloop-boundaries.md` §3).

Design rules baked in:

- **Goal and verification are inseparable** — an issue is only
  `ready-for-agent` if its acceptance criteria are checkable, and the judge
  scores exactly those criteria, not a generic "looks good". Nothing outside
  the contract can fail a PR; what the judge sees beyond it is a finding for
  the PR's findings comment and the triage lane.
- **Demo criteria put the live probe in the contract.** A `demo:` criterion
  is a prose scenario through the real entry point; the implementer runs it
  and the judge scores its evidence (command doc §1c). The loop never
  provisions what a demo needs.
- **Fresh context for judgment.** Reviewing in the implementer's session
  happens in the dumb zone; the judge sees only the issue, the diff, and the
  evidence.
- **Fix loop with a floor.** implement → gates → feed failures back →
  fix, up to `max_fix_rounds`; exhaustion routes the issue to
  `ready-for-human` with the full evidence trail instead of stalling the
  loop or green-washing.
- **Everything visible.** The claim and the PR link are issue comments; the
  gate evidence lives in the PR (command doc §1d).
- **Training mode.** `training_mode = true` stops before push/PR. Flip it off
  once a few runs have earned trust; the guardrail gates (`diff`, caps) stay.

## The TDD contingency

TDD in the loop is **contingent on the baseline, not assumed**: TDD only
disciplines an agent when a green suite makes "new red" attributable to the
new work. The once-per-run baseline probe and what each verdict does are the
command doc's §0.5; `tdd.mode = always | never` overrides the probe for
repos where the answer is known.

## Extension points

- **New deterministic check** → add a `[[gates]]` entry (config-only).
- **New gate kind** (e.g. a benchmark-vs-baseline judge, a docs-drift
  checker) → one `DETERMINISTIC` entry in `devloop/gates.py` (deterministic) or one
  subsection in the `/issue-loop` command (LLM-judged, like `judge`) —
  `devloop-boundaries.md` §3.
- **Per-track policy** (e.g. stricter review on `track:B-core`) → gates grow
  an optional `only_tracks` / `skip_tracks` filter; deliberately not built
  until a real need shows up.
- **Scheduling** — the command is headless-safe; wire it to `/loop`, a cron,
  or a Routine once training mode has been retired. Per-run caps
  (`max_issues_per_run`) bound token burn regardless of trigger.

## Pocock skills

The mint side is Matt Pocock's skills; the loop reads only what they publish.
Native blocking edges and sub-issues have been the wire protocol since v1.1.0
(2026-07-08), the assignee is the claim, and Wayfinder tickets land on the same
frontier unaided. The owner's fork of ten skills, which adds the ticket shape,
the verify grammar and vault routing the loop consumes, is described in the
workspace README under "Pocock skills". The 2026-09 revisit fused acceptance
and review into one judge and made the acceptance criteria the definition of
done.

## Origin

Synthesized from three sources: Matt Pocock's AI Engineer workshop
(DAG-of-issues Kanban, vertical slices, Sandcastle, fresh-context review),
Owain Lewis's agent-loops guide (control-plane visibility, risk labels, per-run
caps), and Austin Marchese's loop-engineering method (goal+verification pairing,
training mode, skills-before-loops).
