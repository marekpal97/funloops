# Dispatch pack — issue #7 (implementer)

## Issue

fx: annotate the catalog with docstrings

## What to build
Append each module's docstring first line to the catalog. `fx/helper.py` has no docstring and stays bare; `fx/nope.py` does not exist and `codegraph files` is a verb, so neither is a named file.

## Interfaces
- `fmt(x: int) -> str` in `fx/helper.py` keeps its signature.

## Acceptance criteria
- [ ] AC1: `fx/core.py` still calls `helper.fmt`
- [ ] AC2: verify: `! test -f fx/nope.py`

## Persona

# Fixture persona

Be lazy: reuse before you write.

# Fixture constitution

1. **One rule** — cited once (#1).

## Repo map — tier 1: catalog

Project Structure (3 files):

└── fx
    ├── __init__.py (python, 1 symbols) — Package fx: the pack fixture.
    ├── core.py (python, 5 symbols) — Core: runs things.
    └── helper.py (python, 2 symbols)

## Repo map — tier 2: issue slice

## Code Context

**Query:** fx: annotate the catalog with docstrings

**fx/helper.py** — 1 symbol, used by 1 file: fx/core.py

**Symbols**
- `fmt` (function) (x: int) -> str — :1

> Drop `symbolsOnly` (or pass `offset`/`limit`) to read the source, like Read.

**fx/core.py** — 2 symbols, no other indexed file depends on it

**Symbols**
- `LIMIT` (variable) = 5 — :10
- `run` (function) (x: int) -> str — :13

> Drop `symbolsOnly` (or pass `offset`/`limit`) to read the source, like Read.

## Standing orders — drill down with codegraph's CLI

The catalog and slice above are already spliced; do not re-derive them. Before
writing, look at what exists: `codegraph explore "<area>"` (an area's symbols
and call paths), `codegraph node <symbol>` / `codegraph node -f <file>` (one
symbol or file with its dependents), `codegraph impact <symbol>` and
`codegraph callers` / `codegraph callees <symbol>` (who is affected by a
change).
