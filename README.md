# funloops

An umbrella for **agentic loops** — each loop a deterministic rail with an LLM
orchestrator on top of it. `devloop` is the first member; autoresearch and a
data-science loop are anticipated.

The umbrella is a **name and a workspace layout, not an abstraction.** There is
no `shared/` or `core/` package here and there will not be one until a second
loop demonstrates the second caller — leaf-util doctrine at package scale. A
speculative common layer written for one member is a junk drawer with a
`packages/` in front of it.

```
funloops/
  pyproject.toml          virtual workspace root (ships nothing)
  packages/
    devloop/              loop #1 — the /issue-loop deterministic rail
      devloop/            the package: 8 modules, stdlib only
      docs/agents/        the judgment plane: command docs + loop.toml
      tests/
```

## devloop

The deterministic half of an issue-to-PR loop. The GitHub issue tracker *is* the
DAG: blocking edges live as native issue dependencies, the graph advances
through GitHub's own state machine, and nothing here stores state — every run
re-reads the tracker and computes the current frontier. LLM judgment lives in
the `/issue-loop` command doc; everything schedulable is plain graph math.

```bash
uv sync
uv run devloop config          # resolved config (defaults + docs/agents/loop.toml)
uv run devloop plan --limit 3  # the runnable frontier + DAG components
uv run devloop check --gate tests
```

`python -m devloop` is the same entry point. Zero runtime dependencies, Python
3.11+. The subcommand surface — `config · plan · claim · release · check ·
validate · prime · triage · trajectory`, JSON on stdout, exit codes — is the
package's one external interface; see
[`packages/devloop/docs/agents/devloop-boundaries.md`](packages/devloop/docs/agents/devloop-boundaries.md)
for the module map and where each seam goes.

devloop was carved out of [thinkweave](https://github.com/marekpal97/thinkweave)
with history preserved (`git log --follow` on any module reaches back through
its whole thinkweave life). The carve-out is behavior-frozen: nothing changed in
the move except packaging.

## Deferred joint utils

Named here so the second loop knows where to look rather than reinventing —
**none of these are built, and the first one gets extracted when a second caller
actually exists**, not before.

- **Gate protocol + `GateResult` shape** — currently `packages/devloop/devloop/gates.py`.
- **The `gh` subprocess seam** — currently `packages/devloop/devloop/github.py`.
- **The config-override pattern** — currently `packages/devloop/devloop/cli.py`.
- **The trajectory / memory-feed shape** — currently `packages/devloop/devloop/trajectory/` and `index_client.py`.

The memory feed is an **optional host extension**, not a dangling stub: a loop
runs fully without one, and `prime` degrades to `primed=false` rather than
failing when no index resolves.

## Pocock skills: the mint side, and the fork that serves the loop

devloop ships no planning skill. Tickets are minted by Matt Pocock's agent
skills (`mattpocock-skills` 1.2.3 in `claude-plugins-official`, MIT, Copyright
(c) 2026 Matt Pocock). The loop reads what they publish and nothing more.

**The fork.** On the owner's machine ten of those skills are forked in a
separate git repo outside this workspace (`~/.agents/skills`, symlinked into
`~/.claude/skills` so the bare names shadow the plugin). Its first commit is
the pristine upstream; every later commit is the delta. The fork is not part of
funloops, is not needed to run the loop, and is never edited from this repo. It
changes the skills in five ways, each one something the loop consumes:

1. **Context goes through the thinkweave vault.** The glossary is the concept
   ontology and an ADR is a vault decision; no `CONTEXT.md`, `docs/adr/` or
   `.out-of-scope/` is written. `grilling` mints a decision at each real fork;
   `to-spec` cites decisions and never mints. The loop's prime step resolves the
   cited ids into the dispatch pack.
2. **One ticket shape.** `## What` (at most five sentences), `## Why` (at most
   three), `## Acceptance criteria`, `## Interfaces`, `## Blocked by`,
   `## Decisions`. `to-tickets` defines the verify grammar the rail runs:
   `verify: <command>` passes on exit 0, and ` => <substring>` also requires
   that text in stdout. A criterion is a verify line wherever a command can
   check it, and an observable the judge can cite where none can. A backticked
   repo-relative path under Interfaces is what puts that file in the pack's map.
3. **The board grammar.** Blocking edges are native issue dependencies, tickets
   are native sub-issues of their spec, and a spec carries the `epic` label,
   never a runnable rung. `devloop board doctor` enforces the same grammar.
4. **Briefs in the same shape.** `triage` writes its agent brief into the issue
   body, in the ticket shape above, because the loop reads the body and never
   the comments. `tdd` names the loop's simplify stage and lets the acceptance
   criteria name the seams.
5. **Model-invocable.** The upstream `disable-model-invocation` flag is removed
   so the skills can be called from inside a larger prompt.

**Without the fork** the upstream skills still feed the loop: native edges
have been published since v1.1.0, prose criteria are scored by the judge, an
issue with no verify lines passes that rail, and an issue with no decision ids
dispatches unprimed. The fork adds enforcement, not compatibility.

**Maintenance.** On a plugin upgrade, diff the two plugin cache versions and
three-way merge onto the fork. The `/issue-loop` command doc stays free of any
vault or skill dependency; a test pins that. The ponytail persona and review
skill are a separate vendored dependency with their own provenance headers
under `packages/devloop/docs/agents/`.

## License

MIT.
