## Parent

#334

## What

Every round carries a `manifest`: the `rsi-rail` commit and dirty flag, the harness id, and the reader versions (thinkweave commit, funloops pin, plugin versions). SessionStart stamps it for interactive sessions and `weave task record-run` for devloop runs. The event buffer records what the rail reads: a Skill tool row on Claude Code, a prompt-declared skill mention on Codex, Pi and OpenCode in the same row shape, subagent start and stop rows, a `nudge` row counted by the deterministic fold, and a feedback entry point `/tweak` can call mid-session. Devloop's stage log keeps its `skill` key when it lands on the round.

## Why

The manifest says what policy a round ran under; events say what happened in it. Neither is useful alone, and both reuse what exists: the Round shape, the session buffer, the deterministic fold. No new store, no new schema beyond one Round field and new row tags.

## Acceptance criteria

- [ ] verify: `uv run pytest tests/test_hooks.py tests/test_harness_conformance.py -q`
- [ ] verify: `grep -q '"Skill' hooks/hooks.json`
- [ ] verify: `uv run python -c "from thinkweave.core.task_contract import Round; Round.from_dict({'manifest': {'rsi_rail': {'repo': 'x', 'commit': 'a'*40, 'dirty': False}, 'harness': 'claude-code'}}, grain='work')"`
- [ ] demo: open a Claude Code session, run `/wrap`, and read the round. Observable: `manifest.rsi_rail.commit` equals the repo HEAD, `dirty` is false, the buffer holds a Skill row naming `wrap`. Artifact: the task note.
- [ ] demo: in a Pi session mention a skill by its slash form and let it run. Observable: the buffer holds a row of the same shape as the Claude Code Skill row, tagged as prompt-declared. Artifact: the session's events file.
- [ ] demo: a session that fans out one subagent. Observable: subagent start and stop rows on the round. Artifact: the events file.
- [ ] A test proves a `nudge` row is counted by `extract_deterministic` and lands on the round, and that a devloop payload's stage log keeps its `skill` key through `record_run`.

## Interfaces

- `src/thinkweave/core/manifest.py` (new): owns the manifest. Exposes `collect(cfg, cwd) -> dict` running `git rev-parse HEAD` and `git status --porcelain` on the repo, plus the reader versions.
- `src/thinkweave/core/task_contract.py`: `Round` gains `manifest`; `_ROUND_CHECKERS` validates the shape.
- `src/thinkweave/surfaces/hooks/handler.py`: `_record_base` stamps the manifest beside `base`; prompt events record declared skill mentions per profile; subagent and nudge rows enter the buffer.
- `src/thinkweave/core/events.py`: `extract_deterministic` counts `nudge` rows and skill rows.
- `src/thinkweave/operations/tasks.py`: `record_run` stamps the manifest and keeps the stage log's `skill` key.
- `hooks/hooks.json`: PostToolUse matcher gains the Skill tool and the subagent events.
- Flow: hook → buffer row → Stop archives → fold → round fields; `record_run` → round fields.

## Decisions

dec-6d5f1913, dec-db6baaab, dec-39626511
