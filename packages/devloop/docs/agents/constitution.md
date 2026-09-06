<!--
  devloop's packaged constitution — the universal, incident-backed rules the
  issue-loop splices into every implementer dispatch (dec-1746aec3; evidence:
  Guarding the Loop rev 3.1). An installing repo inherits this file with zero
  authoring and may EXTEND it via its own docs/agents/constitution.md,
  resolved upward like loop.toml and appended after these rules — never
  substituted. Resolution contract: cli.find_constitution.

  AMENDMENTS — to either layer — are run past a human: a PR a person reviews.
  No machinery beyond that; triage's watched_paths already flags this path.
  No rule without an incident: cite the SHA/PR/issue/decision or don't add
  the rule. A rule leaves when it graduates into a deterministic check.

  The issue-loop orchestrator (issue-loop.command.md §1b) splices the body
  below this header; `devloop pack` absorbs the splice when it lands.
-->

# The constitution

1. **Depth over spread** — deepen an existing module before minting a new one; fold a single-consumer module into its consumer (PR #193's folds).
2. **Compose existing surfaces first** — "does it already exist?" applies system-wide: CLI verbs, MCP catalog, hook layer (85c4859: −1,080 lines of parallel retrieval).
3. **Interfaces are designed at plan altitude** — fill the shapes the issue declares; deviate only with a stated reason in your return, and escalate when the spec itself is the over-abstraction (d03646c).
4. **Python owns determinism, the model owns judgment** — never encode an editorial choice as arithmetic (render_plan / focus.rank rework, PR #193).
5. **A borrowed pattern carries its constraints** — state why they hold here before transplanting its shape (the /wrap-shape borrow, PR #193).
6. **Contracts live in code, not prose** — a convention stated in prose gets a schema, declaration, or test (a9af4bc: declare worker dispatch instead of inferring it).
7. **Never fail open** — a degraded path announces itself; a swallowed error that reads as a clean empty result is a bug (5266f6f).
8. **Uniform public surfaces** — one family, one signature; siblings that force special-case dispatch are a defect (0ec61f7).
9. **Structured channels over free text** — data another stage consumes travels as a declared shape, not prose to re-parse (funloops#5).
10. **Check what the substrate already records before adding a recorder** — the hook layer, git, and the tracker are already recording (138149a).
11. **No platform assumptions in tests** — paths, line endings, and casing differ off Linux (PR #158: 62 failures, ~38 of them assumptions).
12. **Every struggle mints a guard** — a fix-round's cause is a candidate rule, gate, or constitution line (dec-696bacfb, minted mid-review).

**Reading the ladder** — the ponytail ladder governs implementation bodies, not interface design: shapes an issue's Interfaces block declares are part of the explicit request. Rung 2 ("does it already exist?") is system-wide, not file-local. "Fewest files possible" means consolidate, not scatter (PR #193's strand of orphan micro-modules).
