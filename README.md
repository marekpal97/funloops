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

## License

MIT.
