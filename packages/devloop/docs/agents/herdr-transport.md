# The herdr transport — one dispatch recipe

`issue-loop.command.md` §1b dispatches a role by its entry's `transport`.
This doc is the recipe for `transport = "herdr"`; the worktree (§0.5), the
dispatch file and the return file are the command doc's.

herdr opens a workspace on the worktree you made, and the agent is a named
herdr session; every
command answers in JSON, and the ids come from those answers, never from a
guess. `harness` is required on this transport (`--kind` has no default): an
entry without one is a config error you surface, not a guess. The entry's
`args` must settle who answers the harness's approval prompts (the template
shows a working tail per harness): a prompt no one answers reads as
`blocked`. Append `--add-dir <return-dir>` to the tail when the harness
sandboxes writes, so the return file is writable.

```bash
# implementer: a workspace on the existing worktree, the agent in its root pane
herdr worktree open --cwd <repo-root> --path <worktree> --label <branch> --no-focus
#   → .result.workspace.workspace_id, .result.root_pane.pane_id
# gate role (the judge): no new worktree; a new pane beside the implementer's
herdr pane split <implementer-pane-id> --direction right --cwd <worktree>
#   → .result.pane.pane_id
herdr agent start <role>-<N> --kind <harness> --pane <pane-id> -- <args>
#   the entry's harness and args (the literal argv tail); returns once the agent is ready
herdr agent prompt <role>-<N> "Your dispatch is <dispatch-file>: read it whole and follow it. Write your return to <return-file>." --wait --timeout <ms>
```

`--cwd <repo-root>` is required: without it herdr resolves the repository
from the focused workspace, which may be another repo. A herdr worktree lives
outside the repo tree, but a harness that trusts by repository (Codex) keys
that trust on `<repo-root>`, never on `<worktree>`.

`<dispatch-file>` is the path `devloop pack --out` printed. `--wait` returns
the first settled state:

- `idle` or `done`: read the return file. **A settled agent with no return
  file failed** — a harness error (an unsupported model, an API refusal)
  settles as `idle` too.
- **`blocked`** — herdr recognised an approval or question UI. Never answer
  the dialog yourself.
- `agent_prompt_stalled` or a timeout.

Every exit but a return file routes the issue to human (§1c's route-to-human
block). The evidence is `herdr agent read <role>-<N> --source
recent-unwrapped --lines 120`, or the error itself.

A fix round or a re-ask is `herdr agent prompt` to the **same agent name**
with the evidence, a freshly written dispatch file and a fresh return path: it
keeps its context as SendMessage does. `herdr agent get <role>-<N>` carries
the session facts §3 records. Teardown: `herdr worktree remove --workspace
<workspace-id>` is this transport's `git worktree remove` (§1d, same
`--force` rule); it closes the agent's pane with the workspace.
