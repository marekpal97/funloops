# Dispatch pack — issue #7 (implementer)

## Issue

fx: run fmt over the catalog

## What to build
Append each module's docstring first line to the catalog. `fx/helper.py` has no docstring and stays bare; `fx/nope.py` does not exist and `codegraph files` is a verb, so neither is a named file.

## Interfaces
- `fmt(x: int) -> str` in `fx/helper.py` keeps its signature.

## Acceptance criteria
- [ ] AC1: `fx/core.py` still calls `helper.fmt`
- [ ] AC2: verify: `! test -f fx/nope.py`

## Rules

# Fixture constitution

1. **One rule** — cited once (#1).

## Persona

# Fixture persona

Be lazy: reuse before you write.

## Repo map — tier 1: catalog

Project Structure (3 files):

fx/ (3 files, 8 symbols) — Package fx: the pack fixture.

## Repo map — tier 2: issue slice

fx/ (3 files, 8 symbols) — Package fx: the pack fixture.
├── __init__.py (python, 1 symbols) — Package fx: the pack fixture.
├── core.py (python, 5 symbols) — Core: runs things.
└── helper.py (python, 2 symbols)

## Code Context

**Query:** fx: run fmt over the catalog

### Entry Points

- **run** (function) - fx/core.py:13
  `(x: int) -> str`
- **fmt** (function) - fx/helper.py:1
  `(x: int) -> str`


### ⚠️ Low-confidence match

This query matched mostly on common words, so the entry points above may be off-target — treat them as a starting point, not a complete answer. For a reliable result:
- `codegraph_explore` with the **exact symbol names** you are after (class / function / method names), or
- `codegraph_search <name>` for one specific symbol
- `codegraph_files` a likely area: `fx`

Do not assume the list above is comprehensive.

**fx/helper.py** — 1 symbol, used by 1 file: fx/core.py

**Symbols**
- `fmt` (function) (x: int) -> str — :1

> Drop `symbolsOnly` (or pass `offset`/`limit`) to read the source, like Read.

**fx/core.py** — 2 symbols, no other indexed file depends on it

**Symbols**
- `LIMIT` (variable) = 5 — :10
- `run` (function) (x: int) -> str — :13

> Drop `symbolsOnly` (or pass `offset`/`limit`) to read the source, like Read.

## Standing orders

The dispatch is this pack plus two lines the orchestrator adds: the branch
name and the baseline verdict (`green` or `red`, the tests gate on the pristine
worktree). Nothing else instructs you; a rule stated here is stated once.

- Read the repo's architecture and design docs for the areas you touch before
  editing them; a documented standard overrides your instinct.
- TDD per the baseline line. `green`: TDD is enforced — for each acceptance
  criterion with a code-testable seam write the failing test FIRST, watch it
  fail, then implement to green. `red`: the whole-suite guarantee is off; still
  add tests for your slice. Either way you may reshape internals behind the
  seams the criteria name; apply your own cut (the persona's ladder) before
  returning, so the diff you hand back is the shortest one you understand.
  `verify:` lines are the issue author's — never add, edit, or satisfy one by
  changing what it checks.
- **Test at seams.** Test only at the seams the issue names (its acceptance
  criteria / named interfaces); if it names none, choose them and declare the
  choice in your return so it lands in the PR body — never scatter tests across
  internals. **No tautological tests.** Expected values come from an independent
  source of truth (the issue's criteria, a hand-computed value, a fixture) —
  never recomputed the same way the code under test computes them.
- Commit in slice-sized increments on the branch named in the dispatch.
  Do NOT push, do NOT open a PR, do NOT close or label anything — the
  orchestrator owns the control plane.
- Return: worktree path, branch, files touched, test commands run, any deviation
  from the issue's declared shapes with its reason, and any acceptance criterion
  you believe is NOT yet met (honesty over green-washing). A report longer than
  a screen is a file in the worktree; return its path.

**Drill down with codegraph's CLI.** The catalog and slice above are already
spliced; do not re-derive them. Before writing, look at what exists:
`codegraph explore "<area>"` (an area's symbols and call paths), `codegraph
node <symbol>` / `codegraph node -f <file>` (one symbol or file with its
dependents), `codegraph impact <symbol>` and `codegraph callers` / `codegraph
callees <symbol>` (who is affected by a change).
