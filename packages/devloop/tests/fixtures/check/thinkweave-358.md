## Parent

#334

## What

**Owner: the Claude session executes every step of this ticket, including creating the private GitHub repo. The user does not run any of it.** Not a devloop issue: it touches the home directory and a second repo, and produces no thinkweave PR.

A private GitHub repo `marekpal97/rsi-rail`, cloned beside the other projects, holds everything the user tunes at harness level. One folder per reader: `thinkweave/`, `devloop/`, `claude/`, `skills/`. The `agent-skills` repo folds in under `skills/` with its history. The user's global rules file and the personal skills directory become symlinks into the repo. A session loads `/retro` from the repo without any thinkweave change.

## Why

Every value and text the rail can change must live in one repo the user owns, so a tweak is one commit and a round can witness one commit. Knowledge-layer preferences (`sources.yaml`, `PRIORITIES.yaml`, `api.yaml`, the ontology) are not policy and stay in the vault. No framework changes here: only what symlinks alone can move.

## Acceptance criteria

- [ ] verify: `git -C ~/python_projects/rsi-rail log --oneline | grep -q .`
- [ ] verify: `test -d ~/python_projects/rsi-rail/thinkweave -a -d ~/python_projects/rsi-rail/devloop -a -d ~/python_projects/rsi-rail/claude -a -d ~/python_projects/rsi-rail/skills`
- [ ] verify: `readlink -f ~/.agents/skills | grep -q rsi-rail && readlink -f ~/.claude/CLAUDE.md | grep -q rsi-rail`
- [ ] verify: `git -C ~/python_projects/rsi-rail log --oneline -- skills | grep -qi retro`
- [ ] demo: open a Claude Code session and run `/retro` on the current session. Observable: the skill runs and its file resolves under the repo path. Artifact: the session's round.

## Decisions

dec-6d5f1913, dec-db6baaab
