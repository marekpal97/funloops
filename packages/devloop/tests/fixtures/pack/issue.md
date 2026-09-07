## What to build
Append each module's docstring first line to the catalog. `fx/helper.py` has no docstring and stays bare; `fx/nope.py` does not exist and `codegraph files` is a verb, so neither is a named file.

## Interfaces
- `fmt(x: int) -> str` in `fx/helper.py` keeps its signature.

## Acceptance criteria
- [ ] AC1: `fx/core.py` still calls `helper.fmt`
- [ ] AC2: verify: `! test -f fx/nope.py`
