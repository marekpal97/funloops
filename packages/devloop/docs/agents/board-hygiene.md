# Board hygiene — `devloop board doctor` / `sweep`

The tracker is the loop's input contract (`issue-loop.command.md` §0 reads it
as a DAG), so its grammar is enforced by the same rail — a read-only lint plus
a mechanical sweep, run across every repo that installs devloop (funloops#9).

**Conventions, in one table** — each row is a `doctor` check; the last column says whether the fix is
mechanical (a sweep op) or a human verdict (finding only):

| convention | check | fix |
| --- | --- | --- |
| Epic membership = native **sub-issue**; anything with sub-issues carries the `epic` label | `epic-unlabelled` | op `add_label` |
| Epics group, they never run — no runnable rung on an epic | `epic-runnable` | op `remove_label` |
| The epic is **blocked-by every open child** (anchor: `plan --dag <epic>` scopes to the tree, epic closes last) | `epic-unanchored` | op `add_blocker` |
| Epic closes when its last child closes, or gets an explicit re-scope comment | `epic-delivered` | human |
| Ordering = native **blocked-by**; a body `Blocked-by: #N` header with no native twin is the mint-time gap | `text-only-blocker` | op `add_blocker` (never when #N is the issue's own parent — that is the inverted-root error, flagged only) |
| Titles describe the work; `W1a:` / `A3:` / `S5:` prefixes are retired once a native edge carries the order, or the issue is closed (no order left to encode); `EPIC:` / `PRD:` prefixes go once the `epic` label is on | `title-order-prefix` | op `retitle` |
| Exactly **one** triage rung per open non-epic issue (`triage-labels.md` table) | `rung-contradictory` (error) / `rung-missing` (warn) | human / op `add_label needs-triage` (the rung that asserts only "no verdict yet" — it queues the issue for `/triage`) |
| `track:*` = subsystem lane, on every open issue where the repo uses lanes | `track-missing` | human |
| The repo's label set carries the whole triage table + `epic` + the loop's `[labels]`, and none of GitHub's boilerplate five | `label-missing` / `label-boilerplate` | op `create_label` / `delete_label` |
| Cross-repo edges are legal (multi-repo DAGs share one substrate) but a single-repo `plan` cannot see them | `cross-repo-edge` | info |
| Runnable, unblocked, unassigned and untouched for 14 days — re-verify it | `runnable-idle` | info |

**Mint-time rule** (the other half of the contract): any route that mints a DAG
— `/to-tickets`, `/wayfinder`, an interactive session —
publishes native edges + the runnable label at creation; `doctor` catches a
route that forgot.

```bash
uv run devloop board doctor --repo owner/a --repo owner/b   # JSON report; exit 1 on any error
uv run devloop board sweep  --repo owner/a                  # print the op plan (dry run)
uv run devloop board sweep  --repo owner/a --apply [--only create_label,add_label]
```

`--repo` is repeatable and defaults to the cwd's clone. The sweep only ever
executes ops the pure layer emitted; it cannot close an issue, pick a rung, or
choose a track — those stay findings for a human (or a `/triage` session).
Safe to run unattended: the weekly slow loop runs `doctor` across all boards
and `sweep --apply` for the op kinds listed in its cron line.
