# Issue tracker: GitHub

Issues and PRDs live as GitHub issues in whichever repo you are working in — `gh` infers it from the clone's `origin` remote, so nothing here is repo-specific. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## The ticket contract: acceptance criteria

Each acceptance criterion is one line: a `verify:` command, a `demo:` scenario, or one prose sentence. A criterion is never a paragraph, and a verify line is never restated in prose.

- **`verify: <command>`** is one shell command, run from the worktree root; exit 0 passes. `devloop check --issue <N>` runs every verify line after the configured command gates. To match output, pipe it: `<command> | grep -q '<text>'`. A verify line never calls `devloop check`: the rail would run itself, and only the gate timeout would stop it.
- **`demo: <scenario>`** names what the implementer runs, what a reader observes, and the artifact it saves. The judge scores it from that evidence.
- **A prose criterion** is one sentence the judge checks against the code.
