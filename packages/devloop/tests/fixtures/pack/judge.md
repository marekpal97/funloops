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

## Diff

`git diff base...HEAD`:

```diff
diff --git a/fx/core.py b/fx/core.py
new file mode 100644
index 0000000..e540ea0
--- /dev/null
+++ b/fx/core.py
@@ -0,0 +1,14 @@
+"""Core: runs things.
+
+More detail that must not reach the catalog.
+"""
+
+import json
+
+from fx import helper
+
+LIMIT = 5
+
+
+def run(x: int) -> str:
+    return helper.fmt(json.dumps(x))
diff --git a/fx/helper.py b/fx/helper.py
new file mode 100644
index 0000000..e0ee6f2
--- /dev/null
+++ b/fx/helper.py
@@ -0,0 +1,2 @@
+def fmt(x: int) -> str:
+    return str(x)
```

## Judge brief

You are the judge: a fresh reader of one slice. The contract is the issue's
acceptance criteria, the Interfaces block's intent, and rules 3, 6, 7 and 8
of the Rules section. Judge the diff and the touched modules against that
contract and nothing else. Work in the worktree the last lines name; do not
edit code.

- Return one verdict per criterion, `"met"` or `"not-met"`, each with one
  line of evidence. A criterion is not met when the code does not do what it
  says.
- A `verify:` criterion takes its verdict from the Check output section
  below: cite that output, never re-run the command. Where a criterion is
  prose and no verify line ran it, run it yourself and cite the output.
  Evidence for a prose criterion is something you ran against the code,
  never only a test the diff adds.
- A `demo:` criterion is scored from the evidence directory only; never
  re-run the demo. Its `demo.md` opens with `sha: <commit>`. The steps it
  records must match the demo text, and that SHA must be the implementation
  tip (`git rev-parse HEAD` in the worktree). Rest the verdict on the
  artifacts you inspect yourself (output, screenshots, DOM dumps), never on
  the implementer's own assessment, and cite the artifact file. No evidence,
  or evidence on another SHA, is `not-met`. Evidence that cannot settle the
  demo's observable is `not-met` too: the evidence line names what the
  evidence lacks.
- Code the diff changes that no longer works as the issue intends is
  `not-met` too, even when no criterion names the case: return it as one
  more criterion under the reserved id `intent`, and only when you have the
  failure in hand.
- A violation of rule 3, 6, 7 or 8 in code the diff adds or changes is
  `not-met` under the reserved id `rule:<n>`, one entry per violation. Its
  evidence cites the `file:line` from the Touched modules section and says
  what breaks the rule. New code that copies an existing rule-breaking
  pattern is a `rule:<n>` violation: an older precedent excuses nothing.
  Code the diff does not touch never blocks. Rules 1 and 4 are not yours
  here, and no other rule blocks.
- Every `not-met` names a command, a test, or output; without one it is not
  a `not-met`, it is a finding.
- Everything else you notice (a risk, a smell, a better design, an edge
  case outside the contract) is a finding with one of two severities:
  `"problem"` (the code is wrong or fragile in a way you can describe but did
  not demonstrate; a human reads it before merge) or `"note"` (style,
  naming, a cleaner shape). A finding is one sentence naming the rule in the
  Rules section it violates (by number) or the exercised path it breaks (the
  documented verb and the normal state that reaches it); a finding that
  names neither is dropped, not listed. The observation first; no clause
  that withdraws it. Findings never block, whatever their severity. Do not
  search for problems the contract does not name.

Write exactly this object, as JSON, to the return file the last lines name.
`evidence` and `finding` are never blank; `findings` may be empty, not
absent.

```json
{"criteria": [{"id": "AC1", "verdict": "met", "evidence": "<one line>"},
              {"id": "intent", "verdict": "not-met",
               "evidence": "<the command or test you ran and its output>"}],
 "findings": [{"severity": "note", "finding": "<prose>"}]}
```
