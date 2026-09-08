# Dispatch pack — issue #7 (judge)

## Issue

fx: annotate the catalog with docstrings

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
