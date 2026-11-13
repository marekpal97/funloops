<!--
  devloop's packaged `ship` text: the shape of what a loop run writes for
  people. `devloop text ship` prints it, and the /issue-loop command doc
  points here from its ship, stacked and report steps. A policy directory's
  ship.md replaces it whole (pack.TEXTS).
-->
**The PR body carries each fact once:** `Closes #<N>`; one sentence on what
the code now does (not the issue title); the gate table; the demo line; the
findings line; the attribution block. Nothing else.

- The gate table: gate | verdict | summary, one row per gate. A summary
  carries only what varies (`133 lines`, `274 passed`, `5/5`, the failure).
- The demo line: `demo: <short sha>`, the SHA `demo.md` names; none without a
  `demo:` criterion.
- A `human demo` row, `deferred`, only with a `demo (human):`
  criterion.
- The findings line: `Findings: see comment` or `Findings: none`.

**The findings comment** is one per PR, posted right after `gh pr create`:
a `###` heading per severity present (`problem`, `note`), one bullet per
finding, the judge's sentence verbatim. Each deviation the implementer names
is a bullet under a first heading, `### deviation`. A `demo (human):`
scenario is quoted under `### human demo`; each shape seam is a bullet under
`### seam`. Drop a finding that hedges or names
neither a rule nor an exercised path. A finding already filed as an issue is
its number alone. No findings and no deviations: no comment, and the body
reads `Findings: none`.

**A stacked PR** carries the PR body above per issue: a `Closes` line and one
sentence each, one gate table (a row per issue, a column per gate), a demo
line per demo, and a `Not included` line when part of the DAG remains. One
findings comment covers the stack.

**The report.** Per issue: number, outcome (PR opened, awaiting approval,
routed to human), gate results, findings, fix rounds used. Then the deferred
and blocked issues, the issues newly waiting on a human, and every worktree
left behind (path, branch, why). If nothing shipped, say what unblocks the
DAG.
