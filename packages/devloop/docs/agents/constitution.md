<!--
  devloop's packaged constitution — what holds in the code, for any repo
  devloop installs into. `devloop pack --role implementer` inserts the body
  below at the persona's constitution marker line (ponytail-persona.md), so
  it rides every implementer and fix-round dispatch. An installing repo
  inherits this file with zero authoring and EXTENDS it via its own
  docs/agents/constitution.md, resolved upward like loop.toml and inserted
  after these rules — never substituted. Resolution: cli.find_constitution.

  A rule is packaged iff it holds for any repo and its text names the failure
  it prevents in general terms, needing no citation. Provenance is a vault
  decision id cited in the PR that adds the rule; the text carries none. A
  truth enters a repo's overlay by a human's PR once it has bitten more than
  once, and moves here only when a second repo's overlay carries it too. The
  loop never writes to either layer; triage's watched_paths flags this path.
  One screen, at most eight rules; a rule leaves when it graduates into a
  deterministic check. The constitution speaks about the code — rules about
  how the loop runs live in the loop's own doc.
-->

# The constitution

1. **Depth over spread** — deepen the module you are in before minting a neighbour; a module with one consumer belongs inside that consumer. Spread is how a codebase becomes a strand of micro-modules nobody can navigate.
2. **Compose an existing surface before building one** — "does it already exist?" is asked of the whole system: CLI verbs, tool catalogs, hook layers, sibling packages. A borrowed shape carries its constraints; say why they hold here before transplanting it. A parallel re-implementation is the largest diff that still passes review.
3. **Code owns determinism, the model owns judgement** — in a system that composes code with agent judgement, everything reproducible (parsing, ordering, thresholds, formatting) is code, and an editorial choice is never encoded as arithmetic: a score standing in for a decision hides a judgement where nobody can revise it.
4. **A convention stated in prose gets a schema, declaration, or test** — a state or record that crosses a function boundary is a named type, not a string, tuple, or dict; docstrings say what and decisions say why. A prose contract drifts silently; a declared one fails loudly.
5. **Never fail open** — a degraded path announces itself. A swallowed error that reads as a clean empty result is a bug, not a fallback.
6. **One family, one signature** — siblings that force special-case dispatch at the call site are a defect; make the surface uniform.
7. **Data another stage consumes travels as a declared shape** — a structured channel, never prose the next stage has to re-parse.
