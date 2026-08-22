#!/usr/bin/env bash
# Periodic board hygiene across every repo of one owner (funloops#9 scope C).
#
#   1. discover repos with open issues (gh)
#   2. `devloop board doctor` over all of them → JSON report on disk
#   3. `devloop board sweep --apply` — the mechanical ops only
#   4. keep ONE tracking issue's body current with the findings a human must
#      decide (rungs, tracks, delivered epics); created on first run,
#      edited in place after — never a comment per run, never a new issue.
#
# Deterministic, no LLM: the doctor's op vocabulary is the whole blast radius.
# Install: `crontab -e` →
#   30 7 * * 1  /home/marekpal97/python_projects/funloops/scripts/board-sweep.sh >> ~/.cache/funloops/board-sweep.log 2>&1
# Run by hand: scripts/board-sweep.sh [--dry-run]
set -euo pipefail

OWNER="${BOARD_OWNER:-marekpal97}"
TRACKER_REPO="${BOARD_TRACKER_REPO:-$OWNER/funloops}"
TRACKER_TITLE="Board doctor — open findings"
WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/funloops"
DRY=${1:-}
mkdir -p "$CACHE"
cd "$WORKSPACE"

echo "== board-sweep $(date -u +%FT%TZ)"

# 1. repos with open issues
mapfile -t REPOS < <(
  gh repo list "$OWNER" --limit 200 --json nameWithOwner,isArchived,hasIssuesEnabled \
    -q '.[] | select((.isArchived|not) and .hasIssuesEnabled) | .nameWithOwner' |
  while read -r r; do
    n=$(gh api "repos/$r/issues?state=open&per_page=1" --jq 'map(select(.pull_request|not))|length' 2>/dev/null || echo 0)
    [ "$n" -gt 0 ] && echo "$r"
  done)
[ "${#REPOS[@]}" -gt 0 ] || { echo "no repos with open issues"; exit 0; }
FLAGS=(); for r in "${REPOS[@]}"; do FLAGS+=(--repo "$r"); done
echo "repos: ${REPOS[*]}"

# 2. + 3. sweep first (mechanical), then doctor for the post-sweep picture
if [ "$DRY" = "--dry-run" ]; then
  uv run devloop board sweep "${FLAGS[@]}" | tee "$CACHE/board-sweep-plan.json" | python3 -c \
    'import json,sys; print("would apply", len(json.load(sys.stdin)["ops"]), "ops")'
else
  uv run devloop board sweep "${FLAGS[@]}" --apply > "$CACHE/board-sweep-applied.json" || true
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("applied", len(r["applied"]), "failed", len(r["failed"]))' "$CACHE/board-sweep-applied.json"
fi
uv run devloop board doctor "${FLAGS[@]}" > "$CACHE/board-doctor.json" || true

# 4. one tracking issue, body rewritten each run
BODY=$(python3 - "$CACHE/board-doctor.json" <<'PY'
import json, sys
from collections import defaultdict
r = json.load(open(sys.argv[1]))
print(f"_Auto-maintained by `scripts/board-sweep.sh` — last run covered: {', '.join(r['repos'])}._")
print(f"\n**{r['counts']['error']} errors · {r['counts']['warn']} warnings · {r['counts']['info']} info** "
      "after the mechanical sweep. Everything below needs a human verdict; the sweep never decides these.\n")
by = defaultdict(list)
for f in r["findings"]:
    if f["severity"] == "info" or "op" in f:
        continue          # info is noise here; ops are already applied
    by[f["check"]].append(f)
if not by:
    print("Nothing outstanding. 🎉")
for check, fs in sorted(by.items()):
    print(f"### `{check}` ({len(fs)})")
    for f in fs:
        ref = f"{f['repo']}#{f['number']}" if f["number"] else f["repo"]
        print(f"- {ref} — {f['message']}")
    print()
print("Conventions: `packages/devloop/docs/agents/issue-loop.command.md` §5.")
PY
)
if [ "$DRY" = "--dry-run" ]; then echo "$BODY"; exit 0; fi

NUM=$(gh issue list -R "$TRACKER_REPO" --state open --search "in:title \"$TRACKER_TITLE\"" \
        --json number,title -q ".[] | select(.title == \"$TRACKER_TITLE\") | .number" | head -1)
if [ -z "$NUM" ]; then
  gh issue create -R "$TRACKER_REPO" --title "$TRACKER_TITLE" --label "ready-for-human" --body "$BODY"
else
  gh issue edit "$NUM" -R "$TRACKER_REPO" --body "$BODY" >/dev/null && echo "updated $TRACKER_REPO#$NUM"
fi
