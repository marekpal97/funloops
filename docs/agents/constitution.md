<!--
  funloops' constitution overlay — this repo's own rules, extending devloop's
  packaged constitution (packages/devloop/docs/agents/constitution.md), never
  replacing it. `devloop config` lists both paths, packaged first, and the
  pack inserts this body right after the packaged rules, so the numbering
  continues from theirs and the implementer reads one list.

  A truth lands here by a human's PR once it has bitten more than once in
  this repo, citing its vault decision id in the PR; it moves to the packaged
  layer only when a second repo's overlay carries it too. The loop never
  writes here.
-->

8. **Check what the substrate already records before adding a recorder** — the hook layer, git, and the tracker are already recording; a second recorder of the same event is a parallel surface with its own drift.
9. **No platform assumptions in tests** — paths, line endings, and casing differ off Linux; a test that bakes them in fails on the first machine that is not the author's.
