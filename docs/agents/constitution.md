<!--
  funloops' overlay: inserted after devloop's packaged rules
  (packages/devloop/docs/agents/constitution.md), so the numbering continues from theirs.
-->

8. **Check what the substrate already records before you add a recorder.** The hook layer, git, and the tracker already record most events. A second recorder of the same event is a parallel surface with its own drift.

9. **No platform assumptions in tests.** Paths, line endings, and casing differ off Linux. A test that bakes them in fails on the first machine that is not the author's.
