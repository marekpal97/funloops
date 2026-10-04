# devloop boundaries — planes, module map, Gate protocol, host overlay

Where the seams of the `devloop` package go and what each interface promises.
A redesign inside these modules follows this map. *Deep* means much
behaviour behind a small interface; the constitution's rules 1, 4 and 7 set
the goals.

## 1. The two planes

- **Judgment plane**: the `/issue-loop` command (`issue-loop.command.md`). An
  LLM orchestrator that dispatches agents, applies labels and opens PRs.
- **Deterministic plane**: the `devloop/` package. Graph math, gate
  execution, classification, pack and payload assembly. Stdlib only, no LLM
  call, never imports its host.

The seam between them is the **CLI subcommand surface** (JSON on stdout, exit
codes): `config · plan · claim · release · check · validate · prime · triage
· trajectory · board · pack`. The orchestrator knows nothing else. The
`devloop` console script and `python -m devloop` are one entry point.

Below the CLI, modules receive resolved config sections and plain dicts. No
module below `cli` reads `loop.toml`, calls `gh` or resolves paths itself.

## 2. Package map

```
packages/devloop/
  pyproject.toml       package metadata + the `devloop` console script
  devloop/
    __init__.py        (empty)
    __main__.py        `python -m devloop` → cli.main
    cli.py             entry point: argparse, config resolution, dispatch
    dag.py             tracker-as-DAG math over native dependencies
    board.py           board hygiene: the grammar dag.py reads, as checks + sweep ops
    pack.py            the dispatch pack: one role's whole dispatch file
    gates.py           Gate protocol + deterministic executors
    triage.py          risk-lane classification of shipped PRs
    paths.py           leaf util: the three-form path matcher
    github.py          gh plumbing: issue snapshot + tracker mutations
    index_client.py    the read-only seam into the derived index
    trajectory/
      __init__.py      re-exports the module's public interface
      mint.py          write face: trajectory-note payload assembly
      prime.py         read face: claim-time prior-run context
  docs/agents/         the judgment plane: command doc, constitution, persona, loop.toml
  tests/
```

- **`cli.py`** owns config: `DEFAULT_CONFIG`, `load_config`, the `--set`
  overrides and unknown-key rejection (one check for `--set` and the file, so
  a deleted knob is refused by name). Gates are file-only. It is also the
  imperative shell: git reads and file arguments feed the pure modules.
  `find_config` and `find_constitution` walk up from the cwd to the repo
  root; a host's `docs/agents/constitution.md` extends the packaged one.
- **`dag.py`** is pure over issue-snapshot dicts. Edges come from native
  dependency keys only; it reads no body metadata.
- **`github.py`** is the one subprocess seam to `gh`. It owns the
  issue-snapshot shape `{number, title, state, labels, assignees, body,
  native_blocked_count, native_blockers?}` and, for `board`, the
  board-snapshot shape and `apply_op`.
- **`board.py`** checks the board grammar (`board-hygiene.md`) and turns
  mechanical fixes into sweep ops; judgment calls get findings, never ops.
- **`pack.py`** composes one role's dispatch: the issue, the rules, the
  persona (writer only), codegraph's repo map, the touched modules and diff
  (reader), the stack's module edges (shape), the posture's brief and the
  dispatch lines. A role is a `loop.toml [dispatch.<role>]` entry; its
  `posture` picks the sections and its `transport` the orchestrator's recipe.
- **`gates.py`**: §3. **`triage.py`** classifies a shipped PR from its gate
  results, judge return, baseline line and diff; missing safety signals fail
  closed.
- **`paths.py`** is the only leaf util. A leaf util exists only when two
  modules need the same semantics (here `triage` and the diff gate). There is
  no `utils.py`, `config.py` or `git.py`.

## 3. The Gate protocol

A gate is a `loop.toml [[gates]]` entry with a `kind`. **The gate split**:
every kind has exactly one verb, and the verb says which plane runs it.

```python
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"judge": validate_judge}
```

- **Deterministic kinds** (`command`, `diff`): `check` executes them. Any
  other kind gets `gate kind '<k>' is LLM-judged — run it from the
  /issue-loop command, not the script`. `check --issue N` runs every command
  gate, then the issue's `verify:` lines as ad-hoc command gates, as one
  result list.
- **Judgment kinds** (`judge`): the orchestrator dispatches an agent and
  `validate` checks its return. The judge return carries `criteria[]`
  verdicts and `findings[]`; only a `not-met` criterion fails it, and a
  `rule:<n>` criterion (rules 3, 6, 7, 8) must cite `file:line`. The shape
  posture returns `{verdict, flow, owns[], options[]}`.

Both verbs emit one `GateResult` shape, `{id, kind, passed, summary,
detail}`, so consumers never care which plane produced it. A judgment result
adds `reasons`: empty is a verdict, non-empty is a schema rejection naming
each field path. `validate` exits `0` passed, `1` failed, `2` re-ask. A gate
entry carries only the keys its kind reads (`GATE_KEYS`). No class
hierarchy: a dict registry and one result shape state the split.

## 4. The trajectory module and the host overlay

The trajectory note is one primitive with a write face (`mint.py`) and a read
face (`prime.py`). One module owns both because they share one vocabulary:
the frontmatter keys (`outcome`, `primed`, `served`, `builds_on`, `trace`),
the `loop-run` tag and the trajectory→insight `builds_on` link.

Public interface, re-exported by `trajectory/__init__.py`:

- `build_trajectory(issue, *, branch, commits, numstat, gates, fix_rounds,
  outcome, ...) -> dict`: pure; the weave_create-shaped payload.
- `build_prime_payload(issue_number, run_id, concepts, *, conn, limit,
  budget_chars, decisions, query) -> dict`: the claim-time payload. Concepts
  and the issue's text are two retrieval legs; decision ids resolve to title
  and summary line. It always serves what it finds.
- `append_served_event(...)` + `LOOP_PRIME_TOOL`: the served-context write
  to the session buffer.

Invariants:

- **Prime never crashes the loop.** Any index problem degrades to
  `primed=false` with a `note`.
- **Prime writes nothing to the index.** Its one side effect is the buffer
  append.
- **`_normalize_trace` is a backstop, not the validation seam.** Return
  enforcement lives at `validate`; the normalizer only shapes degraded input.
- **`skills[]` is the stage-dispatch log**, the stages this loop dispatched,
  not every Skill invocation. Capture-all is parked until a consumer needs
  invocations the loop did not dispatch.

**The host overlay.** How these notes reach a memory host is host-side. For a
Thinkweave host it is `docs/agents/issue-loop-memory.md` in the thinkweave
repo; this is the one place that locates it. A finished issue writes four
surfaces, and no field has two owners:

| Surface | Owns |
|---|---|
| tracker comments | run history, claims, the PR link |
| PR body | the change and its gate evidence; findings in its one comment |
| trajectory note | how it went |
| session note (`/wrap`) | cross-issue synthesis, decisions, insights |

The trajectory note is a plain `note`: observable facts only, never a
decision field; the loop never mints a decision. Its `trace` is the
machine-readable half of the PR's gate evidence. The register test sorts
every artifact: run-bound semantic trace → the trajectory's `trace`; portable
lesson → an insight note, linked by `builds_on`; enumerable fact → a
frontmatter key. The loop never runs `/wrap`; the host's catch-up rail wraps
the run's session.

## 5. The index_client contract

`index_client.py` is the single seam into the derived SQLite index:
stdlib only, read-only, never imports the host.

- `resolve_db_path(db, vault)`: `--db`, else the vault's `weave_dir`
  override, else `THINKWEAVE_INDEX_DB`; `None` when nothing resolves.
- `open_ro(db_path)`: URI `mode=ro`, `Row` factory.
- `Error`, `Connection`: aliases, so no other module imports `sqlite3`.
- `trajectory_candidates(conn, concepts, query, scan_cap)`: the concept leg
  and the FTS leg over `notes_fts`, fused by RRF (`RRF_K = 60`). FTS failure
  is tolerated only while the concept leg returns something.
- `note_rows(conn, ids, note_type='note')`: ids → `{title, body}` of one type.

Every SQL string devloop issues lives here, and the query surface speaks
index vocabulary (tags, concepts, FTS, ids). Trajectory judgment (the
`loop-run` tag, outcome ranking, budgets) stays in `trajectory/prime.py`.
`test_devloop_boundaries.py` pins the `sqlite3` importer set, the SQL-speaker
set and the no-host rule. The schema pin against a real indexer runs
host-side, where both sides of the seam exist.
