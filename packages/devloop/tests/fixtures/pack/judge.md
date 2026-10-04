# Dispatch pack — issue #7 (judge)

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

## Touched modules

Every file `base...HEAD` touches, in full, its lines numbered for `file:line` citations.

### fx/core.py

```
   1  """Core: runs things.
   2
   3  More detail that must not reach the catalog.
   4  """
   5
   6  import json
   7
   8  from fx import helper
   9
  10  LIMIT = 5
  11
  12
  13  def run(x: int) -> str:
  14      return helper.fmt(json.dumps(x))
```

### fx/helper.py

```
   1  def fmt(x: int) -> str:
   2      return str(x)
```
