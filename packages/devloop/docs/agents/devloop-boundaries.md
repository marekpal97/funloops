# devloop/ boundary spec — module map, Gate protocol, trajectory module, index_client contract

**Status:** accepted design, and the boundary authority for anything that
redesigns inside these modules. It specifies *where seams go and what each
interface promises*. Issue numbers throughout are thinkweave's, where the
package was designed and extracted before the carve-out into this workspace.

Vocabulary: *module* = interface + implementation (scale-agnostic); *interface*
= everything a caller must know (signatures, invariants, error modes, config);
*seam* = where an interface lives; *deep* = much behavior behind a small
interface. Terms per the codebase-design doctrine; goals per epic #88's
north-star block (fewer POCs; deep but interpretable modules with boundaries at
likely redesign points; conceptual fidelity; generic utils never beside key
logic; no contract asserted in prose without an enforcing seam).

## 1. The two planes and the external seam

The loop is two planes with one seam between them:

- **Judgment plane** — the `/issue-loop` command (`issue-loop.command.md`): an
  LLM orchestrator that dispatches implementer/judge/reviewer subagents,
  composes traces, applies labels, opens PRs.
- **Deterministic plane** — the `devloop/` package: graph math, gate
  execution, classification, payload assembly. Stdlib-only, never imports its
  host, no LLM calls (both pinned by `test_devloop_boundaries.py`).

The seam between the planes is the **CLI subcommand surface** (JSON on stdout,
exit codes): `config · plan · claim · release · check · validate · prime ·
triage · trajectory · board`. That surface is the package's one external interface — the
orchestrator knows nothing else. It is reached through the `devloop` console
script or `python -m devloop`; the two are one entry point, pinned byte-equal
by `test_funloops_packaging.py`.

Everything below the CLI is interior. Modules receive resolved config
*sections* and plain dicts as parameters — no module below `cli` reads
`loop.toml`, touches `gh`, or resolves paths on its own (accept dependencies,
don't create them).

## 2. Package map

```
packages/devloop/
  pyproject.toml       package metadata + the `devloop` console script
  devloop/
    __init__.py        (empty)
    __main__.py        `python -m devloop` → cli.main
    cli.py             entry point: argparse, config resolution, dispatch
    dag.py             tracker-as-DAG math + the body-grammar it parses
    board.py           board hygiene: the grammar dag.py reads, as checks + sweep ops
    gates.py           Gate protocol + deterministic executors
    triage.py          risk-lane classification of shipped PRs
    paths.py           leaf util: the three-form path matcher
    github.py          gh plumbing: issue snapshot + tracker mutations
    index_client.py    the sqlite-RO seam into the derived index
    trajectory/
      __init__.py      re-exports the module's public interface
      mint.py          write face: trajectory-note payload assembly
      prime.py         read face: claim-time prior-trajectory context
  docs/agents/         the judgment plane: command docs + loop.toml
  tests/
```

Nine public names. `trajectory/` is **one module** with two implementation
files — its interface is what `trajectory/__init__.py` re-exports; `mint.py`
and `prime.py` are internal seams, not siblings (§4). There is no `config.py`,
no `utils.py`, no `git.py` (§2.1, §6). `docs/agents/` is the other plane's home
and imports nothing: the command doc, the vendored skills, this spec, the
packaged constitution (the default every installing repo inherits and may
extend via its own `docs/agents/constitution.md`; resolution contract
`cli.find_constitution`), and the repo's `loop.toml` beside the host-neutral
`loop.toml.template`.

Per-module interfaces:

**`cli.py`** — owns `DEFAULT_CONFIG`, `load_config`, `parse_override`,
`apply_overrides`, `build_arg_parser`, `main`. Config lives here deliberately:
resolution-and-override is run-posture, exercised only at the entry point;
unknown-key rejection (one check for `--set` and the file, so a deleted knob
is refused by name rather than silently ignored) and "gates are file-only"
are CLI contract lines.
`cli.py` is also the imperative shell: the `trajectory` subcommand's git reads
(branch, log, numstat) and file-argument loading happen here, feeding the pure
`build_trajectory` — subprocess git is argument-gathering, not a module.

**`dag.py`** — `parse_wave`, `parse_parallel_safe`, `blockers`,
`compute_components`, `scope_to_dag`, `apply_assume_done`, `compute_frontier`.
Pure over issue-snapshot dicts (shape owned by `github`, § below). Edges come
from the snapshot's native dependency keys only. The two `Wave:` /
`Parallel-safe:` regexes stay: they are **domain grammar, not generic
parsing** — body metadata with no native GitHub field, living beside the
frontier math that consumes them.

**`gates.py`** — the Gate protocol (§3) plus `run_command_gate`,
`evaluate_diff_gate` (pure), `run_diff_gate`. Runs its own `git diff`
subprocess — execution is the gate's job.

**`triage.py`** — `classify_pr`, the signal enums, `TRIAGE_LABELS`.
Fail-closed posture on the three safety-critical signals is part of the
interface, stated in the docstring and pinned by the existing tests.

**`paths.py`** — `match(path, pattern)` (dir-prefix / glob / bare-basename
dispatch by pattern shape) and `hits(files, patterns)`. The only leaf util
(§6). Two callers: `triage` (sensitive/watched paths) and the diff gate
(`forbidden_paths` — unified by #94; all shipped `forbidden_paths` entries end
in `/`, where `match` is exactly `startswith`. The unification means
`forbidden_paths` *adopts* the three-form convention; a future non-slash entry
gets basename/glob semantics, documented in loop.toml when #94 lands).

**`github.py`** — `run(args)` (the one subprocess seam to `gh`),
`fetch_issues()` (REST snapshot + native-dependency enrichment),
`fetch_labels(number)`. This module owns the **issue-snapshot dict shape** —
`{number, title, state, labels, assignees, body, native_blocked_count,
native_blockers?}` — which is the `github`↔`dag` contract; `dag` never sees
`gh` output, only these dicts. Tracker *mutations* for claim/release stay in
`cli.py` composed from `github.run`: the assign-vs-label claim convention is
loop policy, not gh plumbing, and one-call-site wrappers would fail the
deletion test. For the `board` verb it also owns the richer **board-snapshot
shape** — `{repo, labels[], issues[{…, sub_issues, blockers[], children[],
parent}]}` with every relationship resolved to a `{repo, number, state}` ref
(`fetch_board(repo)`) — and `apply_op(op)`, the one place the sweep's op
vocabulary meets the network.

**`board.py`** — the enforcing seam for the board grammar `dag.py` assumes
(funloops#9): sub-issue = epic membership, native blocked-by = ordering, the
`epic` anchor is blocked-by every open child, `[labels]` rungs are exclusive,
titles don't re-encode order a native edge already carries. Pure checks over
the board snapshot (labels · epics · rungs · edges — private; the interface is two functions),
each yielding findings with a severity and — only where the fix is mechanical,
never where it is a judgment (which rung, which track, is this epic done) — a
sweep **op** (`create_label · delete_label · add_label · remove_label ·
retitle · add_blocker`). `doctor()` runs them all; `plan_sweep()` turns a
report into the deduped, ordered op list `board sweep --apply` replays.
Conventions text: `issue-loop.command.md` §Board hygiene.

**`index_client.py`** — §5.

**`trajectory/`** — §4.

### 2.1 Deliberate non-modules

- **No `config.py`** — the issue's module list omits it on purpose; config is
  a `cli` concern (above).
- **No `git.py`** — two call sites (`gates`, `cli`) with two-line bodies each;
  a wrapper would be a pass-through (deletion test fails).
- **No `shared/` package in the workspace** — the umbrella is a layout, not an
  abstraction; the deferred joint-util candidates are named in the workspace
  README and get extracted when a second loop is the second caller, not before.
- **No `utils.py`** — §6.

## 3. The Gate protocol

A gate is a config entry (`loop.toml [[gates]]`) with a `kind`. The protocol's
structural claim: **every kind has exactly one verb, and which verb it has
states which plane runs it.**

- **Deterministic kinds** (`command`, `diff`) — the rail *executes*:
  `execute(gate_cfg, ctx) -> GateResult`, where ctx is the worktree cwd (+
  base ref for diff).
- **Judgment kinds** (`judge`, `simplify`) — the rail never executes; the
  orchestrator dispatches a subagent and the rail *validates* the subagent's
  return: `validate(gate_cfg, raw) -> GateResult`, rejecting schema-violating
  returns (#99's re-ask loop keys off the rejection). `judge` is the fused
  acceptance+review stage (funloops#39, dec-611cbd8a): its envelope carries
  `criteria[]` verdicts and `findings[]`, and only a criterion `not-met`
  fails it. A gate entry may carry only the keys its kind reads
  (`GATE_KEYS`); the config loader refuses any other key by name.

`GateResult` is a plain dict shape, not a class: `{id, kind, passed, summary,
detail}` — already what both executors emit and what the trajectory
frontmatter stores; the shape is shared by both verbs so downstream consumers
(`--gates-json`, the PR evidence table) never care which plane produced it.

Structurally, `gates.py` carries one registry per verb:

```python
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"judge": validate_judge, "simplify": validate_simplify}
```

The `check` subcommand dispatches **only** through `DETERMINISTIC`; any other
kind — judgment-side or typo — gets the existing "LLM-judged — run it from
the /issue-loop command" error (previously an `else` branch; the registry
promotes it from error-message prose to structure, byte-identical output).
The `validate` subcommand (#99) dispatches **only** through `JUDGMENT`, and
the two registries are pinned disjoint + covering the shipped pipeline.

A judgment result is `GateResult` plus `reasons`, and that key carries the
whole execute-vs-validate difference: **empty `reasons` = a verdict**
(`passed: false` is a fix round), **non-empty `reasons` = the return never
became a verdict** — each entry names an offending field path and value, and
the orchestrator re-asks the same subagent. `validate` maps this onto
`check`'s exit codes with rejection on the error rung: `0` passed, `1` failed,
`2` re-ask.

*(Amended 2026-08-01, owner ruling during #94: the original draft shipped
`JUDGMENT` as a data-only set at #94 time. Both the review and simplify gates
independently flagged that as the epic's own half-mechanism shape, and the
owner sided with the anti-goal over the draft.)*

**No class hierarchy.** A dict registry + one shared result shape state the
split completely; `typing.Protocol` machinery would be interface without
behavior.

## 4. The trajectory module — one primitive, two faces

The trajectory note is one primitive with a write face and a read face, and
**one module owns both** because both faces share one vocabulary: the
frontmatter keys (`outcome`, `primed`, `served`, `builds_on`, `trace`), the
`loop-run` tag, the trajectory→insight `builds_on` link. Split mint from prime into
sibling modules and that vocabulary needs a third home or gets duplicated —
the exact "retrieval, triage, trajectory composition share a bucket" failure
inverted. Prime is a *trajectory mechanic*: it reads what mint writes.

Public interface (re-exported by `trajectory/__init__.py`, everything else
internal):

- `build_trajectory(issue, *, branch, commits, numstat, gates, fix_rounds,
  outcome, ...) -> dict` — pure; emits the weave_create-shaped payload.
  (mint face)
- `build_prime_payload(issue_number, run_id, concepts, *, conn, limit,
  budget_chars, decisions, query) -> dict` — the claim-time payload.
  `concepts` (ontology terms) and `query` (the issue's text) are the two
  retrieval legs; `decisions` are the file-anchored ids the orchestrator
  resolved. It always serves what it finds — the sampled holdout was retired
  by dec-cf8f0d33. (prime face)
- `append_served_event(buffer_path, run_id, issue_number, served, session_id)`
  + `LOOP_PRIME_TOOL` — the served-context write-through to the session
  buffer JSONL.

Internal to `mint.py`: the trace normalizers (`_normalize_trace*`,
`_as_int_or_none`) and the skill projection. Two settled scope rules the module
carries, stated here because they bound what these internals may become:

**`_normalize_trace` is a backstop, not the validation seam.** Gate-return
enforcement lives at the `validate` verb (§3), where the subagent's return
arrives and a rejection can be re-asked. The normalizer survives only to shape
legacy or degraded input — strict on type (a non-dict trace is rejected),
lenient on keys (unknowns dropped, each item projected). Add enforcement at the
seam, never here.

**`skills[]` is the stage-dispatch log, not a capture of every Skill
invocation.** It records the stages *this loop dispatched* — implementer,
the judge, and future stages — as
`{id, role, outcome, fix_rounds_attributed}`. The generic capture-all is
**parked**: it needs a new mechanism (a Skill-tool hook or transcript scraping)
and has no live reader, and a capability ships with its consumer or not at all.
*Unpark trigger:* a consumer that needs invocations the loop did not itself
dispatch — concretely, asking which invocations correlate with rework. Until
then nothing implies capture-all exists.

**Invocation-trajectory extension — the parking note above is the contract.**
Internal to `prime.py`:
`_coerce_builds_on`, the outcome-rank table, `render_prime_block`, and the two
composition helpers over the seam (`query_trajectories`, `resolve_insights`).

Two interface-level invariants, stated on the module:

- **Prime never crashes the loop.** Any index problem (missing db, corrupt
  file, schema drift) degrades to `primed=false` with a `note` — the
  orchestrator dispatches unchanged. This is a contract line callers build
  on, not an implementation nicety.
- **Prime writes nothing to the index.** Its only side effect is the
  served-event append to the *session buffer* (markdown-adjacent log); the
  index stays strictly read-only (§5).
- **Prime retrieves on two legs, and the concept leg alone is not enough.**
  Concept-only retrieval was dead by construction: the write side tags
  trajectories with ontology concepts while the rail was being handed GitHub
  labels, so the join matched nothing and every run was effectively unprimed.
  Hence the full-text leg over the issue's own words, RRF-fused in
  `index_client` (§5), and hence the warning the payload's `note` carries when
  called with labels and no `--query`. The orchestrator's half of this contract
  is the command doc §1b.

**The host overlay.** How these notes reach a *particular* memory host — the
vault write-back, its four-surface ownership partition, the wrap-coverage rail —
is host-side documentation, not this package's. For a Thinkweave host that
document is `docs/agents/issue-loop-memory.md` in the **thinkweave** repo; this
paragraph is the only place it is located, so a move updates one line. What lives here is the shape the package emits and reads back;
what lives there is what a host does with it.

## 5. The index_client contract

`index_client.py` is the package's **single seam into the derived SQLite
index** — stdlib-only, read-only, never imports the host.

Interface (#94, completed by #100):

- `resolve_db_path(db: str | None, vault: str | None) -> str | None` — `--db`
  wins; else the vault's `weave_dir` override from `config/config.toml` /
  legacy `.weave/config.toml` (mirroring `core.config` resolution), else
  `THINKWEAVE_INDEX_DB`; `None` when nothing resolves (never guess a path).
- `open_ro(db_path) -> sqlite3.Connection` — URI `mode=ro`, `Row` factory.
- `Error = sqlite3.Error`, `Connection = sqlite3.Connection` — aliases so no
  other module ever imports `sqlite3` (cli's degrade guard, prime's
  annotations post-#100), keeping the importer-allowlist seam tight.
- `trajectory_candidates(conn, concepts, query, scan_cap) -> list[dict]` — the
  retrieval surface. Two legs (concept match; fts5 match over `notes_fts`)
  fused by RRF at `RRF_K = 60`, the retrieval doctrine's constant (the main
  package's knob is `retrieval.rrf_k`; the rail reads no vault config, so it is
  a constant here rather than a parameter nobody passes). Either leg's input
  may be empty. The FTS leg is best-effort *only while the other leg is
  carrying*: a vault with no `notes_fts` still primes on concepts, but a broken
  FTS with nothing else retrieved raises into the degrade guard — FTS is
  load-bearing, so its failure must not read as a clean empty match.
- `note_bodies(conn, ids) -> dict[str, str]` — ids → body text, `type='note'`
  only (a `builds_on` id may name a decision or session; those never serve).

The SQL-home invariant: every SQL string devloop issues lives here — the
thinkweave index is the package's only database (codegraph is a CLI the pack
invokes, never an index devloop reads: #28, dec-d2de831e).
`test_devloop_boundaries.py` pins the speaker set (a SELECT appearing in any
other module is a new database seam nobody designed). The query surface is
*index-vocabulary-shaped* (tags,
concepts, FTS match, ids → bodies), returning plain dicts — schema knowledge
inside, domain knowledge outside. Trajectory-domain judgment (which tag is
`loop-run`, outcome ranking, color filtering, budgeting) stays in
`trajectory/prime.py`, composing over the seam. The fusion sits *inside* the
seam because both legs are retrievers over the index; prime never sees a
rank list, only fused candidates.

Four enforcing seams — prose alone is banned by the epic. Three live here;
the schema pin can only live where a real index does:

1. **Importer-allowlist test** — asserts which devloop modules import
   `sqlite3`: the `{index_client}` singleton. Five lines, and the seam is
   enforced rather than remembered (`test_devloop_boundaries.py`).
1b. **SQL-speaker test** — asserts which modules contain SQL at all: the
   same `{index_client}` singleton (`test_devloop_boundaries.py`).
2. **No-host test** — asserts no module imports the host it was carved out of.
   This workspace's CI has no host installed, so without the seam the coupling
   would surface as a confusing ImportError in some later slice rather than as
   a boundary violation (`test_devloop_boundaries.py`).
3. **Schema-pin test** — builds a real fixture index by importing the *host's
   indexer*, then exercises every devloop SQL path against it: `resolve_db_path`
   + `open_ro` + the trajectory queries (`notes`, `note_tags`, `note_concepts`
   tables and the columns they read), so indexer schema drift fails a test
   instead of silently degrading prime to unprimed forever. It runs **host-side
   only** — it needs both sides of the seam in one process, and this package
   deliberately cannot import the host. The hand-built-schema tests in
   `test_issue_loop.py` are the fast unit checks that travel with the package.

## 6. Leaf-util doctrine

The north-star bans generic utils beside key logic; the anti-goal bans
speculative structure. The reconciling rule: **a leaf util exists only when
two modules need the same semantics** (one caller = hypothetical seam; two =
real).

- `paths.py` qualifies today — `triage` and the diff gate, unified by #94.
- Nothing else does. `_split_csv` (cli), `_as_int_or_none` (mint's trace
  semantics: bool-is-not-int), `_coerce_builds_on` (prime's wikilink
  tolerance) are *domain-shaped* coercions with one home each; they stay
  module-private where used. If a later wave gives one a second
  cross-module caller with identical semantics, mint the leaf then.
- No `utils.py`, ever — a junk drawer is the "generic beside key logic"
  violation with a folder around it.

## 7. Redesign-point ledger

The boundary placement is justified by what it localizes. The rows are the
thinkweave-era changes that tested it; they are kept as evidence the seams hold,
not as open work:

| Issue | Change | Touches |
|---|---|---|
| #95 | delete body-regex DAG grammar (native deps) | `dag.py` only |
| #98 | delete v1 Lessons fallback | `trajectory/prime.py` only |
| #99 | judgment-gate validators + normalize-to-backstop | `gates.py` (introduces the `JUDGMENT` validator registry + its tests), `trajectory/mint.py` |
| #100 | prime v3 retrieval (FTS+concept via the seam) | `index_client.py`, `trajectory/prime.py` |
| #102 | rework-evidence stamps | `trajectory/mint.py` (+ orchestrator) |

Any of these needing a third module is a boundary bug — file it against this
spec.

## 8. Where this package came from

`devloop` was extracted from a single `scripts/` rail in its host repo, then
carved into this workspace with history preserved (`git log --follow` on any
module reaches back through its whole prior life). Both moves were
behavior-frozen; the function-by-function migration map that governed the first
one has served its purpose and lives in that history.

Standing acceptance for any future move: whole suite green, CLI surface
byte-compatible (including the judgment-kind error message and `--help` text),
no semantic diff beyond what the issue names.
