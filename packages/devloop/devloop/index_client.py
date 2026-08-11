"""The package's single seam into the derived SQLite index.

Stdlib only, strictly read-only, never imports ``thinkweave`` (the rail may
run where the package is not installed). Every SQL statement devloop issues
lives here — ``trajectory.prime`` composes over the rows this module returns
and never speaks sqlite (the importer-allowlist test in
tests/test_devloop_boundaries.py enforces the singleton).

Retrieval shape (#100): trajectory candidates come from two independent
retrievers — concept match and full-text match over the issue's own words —
fused by reciprocal rank fusion. One leg alone was dead by construction: the
write side tags notes with ontology concepts while the read side was handed
GitHub labels, so the concept join matched nothing on the live index.

Third leg (funloops#2): semantic similarity, through a *composition seam*
rather than a query of our own. Vectors live in the host's embeddings.db and
embedding a query needs the host's provider stack, which a stdlib-only package
cannot have — so the leg shells out to ``weave search --mode similar`` (the
same subprocess posture as the ``gh`` and ``git`` seams) and fuses the ranked
ids that come back. No host, no embeddings, no key → the leg is skipped and
said so; it never degrades into silence.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tomllib
from pathlib import Path

# The seam's error type, re-exported so callers can degrade on an index
# problem without importing sqlite3 themselves (see the importer-allowlist
# test in tests/test_devloop_boundaries.py).
Error = sqlite3.Error
# Aliases so no caller ever imports sqlite3 itself (cli's degrade guard now,
# prime's annotations post-#100) — keeps the importer-allowlist seam tight.
Connection = sqlite3.Connection


def open_ro(db_path: str) -> sqlite3.Connection:
    """Open the derived index strictly read-only (never mutate derived state)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_weave_dir_override(vault_root: Path) -> Path | None:
    """Honor a top-level ``weave_dir`` in the vault's config.toml.

    PR #10 relocates derived state (index.db, embeddings.db, buffer/) off the
    vault path — on 9P-mounted vaults the live index is ``<weave_dir>/index.db``,
    NOT ``<vault>/.weave/index.db``. Mirror ``core.config``'s resolution: ``~``
    expands, a relative value anchors at ``vault_root``, absolute passes
    through. Read ``config/config.toml`` first, then the legacy
    ``.weave/config.toml``. Malformed/unreadable config or an absent key →
    ``None`` (fall back to the legacy layout; never crash).
    """
    for rel in ("config/config.toml", ".weave/config.toml"):
        path = vault_root / rel
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        value = data.get("weave_dir")
        if isinstance(value, str) and value.strip():
            resolved = Path(value).expanduser()
            return resolved if resolved.is_absolute() else vault_root / resolved
    return None


# ---------------------------------------------------------------------------
# Trajectory retrieval — two legs, fused

# The retrieval doctrine's fusion constant (the main package's config knob is
# `retrieval.rrf_k`, same default). The rail reads no vault config, so it is a
# constant here rather than a knob nobody would turn.
RRF_K = 60

# The candidate columns prime composes over. `frontmatter` carries builds_on /
# outcome / issue; the note body itself is never served — only its insights are.
_CANDIDATE_COLS = "n.id, n.title, n.date, n.frontmatter"

# fts5 tokenizes on `-` and `_` as word chars (indexer.py's tokenchars); mirror
# that so a term the index holds is a term we can ask for.
_FTS_TERM = re.compile(r"[0-9A-Za-z_-]{3,}")


def _fts_match_expr(text: str) -> str:
    """Encode free text (an issue title/body) as an OR-of-terms fts5 MATCH.

    The query is user-shaped, so nothing but indexable terms survives: keeping
    only tokenizer-legal runs of 3+ chars makes fts5 operator soup (``*``,
    ``NEAR``, unbalanced quotes) structurally unreachable rather than caught
    after the fact. Each term is quoted so a leading ``-`` stays a literal.
    Terms dedupe (order-preserving) and cap at 24 — a long issue body must not
    become an unbounded query. No terms → ``''``, the caller's signal to skip
    the leg entirely (``MATCH ''`` is itself a syntax error).

    ponytail: no stopword filter, so common issue-prose words ("the", "with")
    are real OR terms and the leg can touch most of the index — ~100ms at 6k
    notes, paid once at claim time. Upgrade path when it stops being cheap:
    drop terms whose document frequency exceeds a threshold.
    """
    terms = list(dict.fromkeys(_FTS_TERM.findall(text)))[:24]
    return " OR ".join(f'"{t}"' for t in terms)


def _ranked(rows) -> list[tuple[int, dict]]:
    """A leg's rows as the 1-indexed ranking :func:`_rrf` consumes."""
    return list(enumerate((dict(r) for r in rows), start=1))


def _by_concepts(conn: sqlite3.Connection, concepts: list[str],
                 scan_cap: int) -> list[tuple[int, dict]]:
    """Leg 1: ``[loop-run]`` notes carrying ANY of the concepts, recency first."""
    placeholders = ",".join("?" * len(concepts))
    return _ranked(conn.execute(
        f"""SELECT DISTINCT {_CANDIDATE_COLS}
            FROM notes n
            JOIN note_tags t ON t.note_id = n.id AND t.tag = 'loop-run'
            JOIN note_concepts c ON c.note_id = n.id
            WHERE c.concept IN ({placeholders})
            ORDER BY n.date DESC, n.id DESC
            LIMIT ?""",
        [*concepts, scan_cap],
    ))


def _by_fts(conn: sqlite3.Connection, match: str,
            scan_cap: int) -> list[tuple[int, dict]]:
    """Leg 2: ``[loop-run]`` notes matching the text, fts5 relevance order."""
    return _ranked(conn.execute(
        f"""SELECT {_CANDIDATE_COLS}
            FROM notes_fts f
            JOIN notes n ON n.rowid = f.rowid
            WHERE notes_fts MATCH ?
              AND EXISTS (SELECT 1 FROM note_tags t
                          WHERE t.note_id = n.id AND t.tag = 'loop-run')
            ORDER BY f.rank
            LIMIT ?""",
        [match, scan_cap],
    ))


# Leg 3 lives outside sqlite: the vectors are the host's and embedding a query
# needs the host's provider, so the ranking is asked for over a subprocess.

# How deep to ask the host to rank. Similar mode ranks the WHOLE vault — its
# `--tags` filter is wired to fts mode only — so the leg over-fetches and scopes
# to [loop-run] on hydration (`_by_semantic`).
#
# ponytail: 200 is a depth, not a tuned constant. Trajectories are a fraction of
# a percent of a live vault; on the host index two synonym-only queries put the
# intended trajectory at ranks 1 and 30. Cosine is computed over every vector
# whatever the limit, so extra depth costs only parsing. Upgrade path when a
# vault outgrows it: a tag filter on the host's similar mode, then ask for
# exactly `scan_cap`.
SEMANTIC_FETCH = 200

# A claim-time step must not hang on a host that wedged.
SEMANTIC_TIMEOUT = 60

# `weave search` has no JSON mode; its one stable line shape is
# ``  [<type>] <title> (<id>)`` optionally followed by `` [tag, tag]``, with
# continuation lines (snippet, project) indented four. The id is the LAST
# parenthesized group — titles carry parentheses of their own.
_SEARCH_LINE = re.compile(r"^ {2}\[[^\]]*\] ")
_SEARCH_ID = re.compile(r"\(([^()]+)\)")


def semantic_ranking(
    vault: str | None, query: str, limit: int = SEMANTIC_FETCH,
) -> list[str] | None:
    """Note ids ranked by embedding similarity to ``query``, via the host CLI.

    The composition seam: ``weave search --mode similar`` scoped to ``vault``,
    parsed off its stable line shape. Returns ids in the host's rank order.

    ``None`` means **the leg did not run** — no vault to scope to, no query text
    to embed, no ``weave`` on PATH, or the host refusing (embeddings unbuilt or
    keyless exits 1). That is a different fact from ``[]``, "ran and matched
    nothing", and callers surface it: a silently dead leg is the failure #100
    was filed to fix. Never raises.

    The vault is pinned per call rather than inherited: the leg must rank the
    same vault the index came from, not whatever ``THINKWEAVE_VAULT`` an ambient
    shell carries (ids from another vault would hydrate to nothing).

    Similar mode does not fall back to full text — the host raises
    ``SemanticSearchUnavailable`` and its CLI exits 1 — so a served ranking is
    always semantic, and the FTS leg is never double-counted through this one.
    """
    if not vault or not query.strip():
        return None
    try:
        proc = subprocess.run(
            ["weave", "search", query, "--mode", "similar", "--type", "note",
             "--limit", str(limit)],
            capture_output=True, text=True, timeout=SEMANTIC_TIMEOUT, check=False,
            env={**os.environ, "THINKWEAVE_VAULT": vault},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ids = []
    for line in proc.stdout.splitlines():
        if _SEARCH_LINE.match(line):
            groups = _SEARCH_ID.findall(line)
            if groups:
                ids.append(groups[-1])
    return ids


def _by_semantic(conn: sqlite3.Connection, ids: list[str],
                 scan_cap: int) -> list[tuple[int, dict]]:
    """Leg 3: the host's ranked ids hydrated to ``[loop-run]`` candidate rows.

    Scoping is this join, not the host call: similar mode ranks the whole vault,
    so an insight note, a source, or an id this index does not hold simply fails
    to hydrate.

    **Each survivor keeps the position the host gave it.** Trajectories are a
    fraction of a percent of a vault, so nearly the whole ranking drops out
    here; re-basing the handful that survive to 1..N would enter a 180th-place
    cosine match at 1/61 — the largest score any leg can contribute — and the
    leg would promote something on every single query. Carrying the original
    position is what makes a weak semantic match score like a weak match.
    """
    ids = list(dict.fromkeys(ids))  # a repeat would count its rank twice
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"""SELECT {_CANDIDATE_COLS}
            FROM notes n
            JOIN note_tags t ON t.note_id = n.id AND t.tag = 'loop-run'
            WHERE n.id IN ({placeholders})""",
        ids,
    )}
    return [(pos, rows[i]) for pos, i in enumerate(ids, start=1)
            if i in rows][:scan_cap]


def _rrf(rankings: list[list[tuple[int, dict]]]) -> list[dict]:
    """Reciprocal rank fusion: ``score[id] = Σ 1/(RRF_K + rank_i)``, 1-indexed.

    Each leg contributes ``(rank, row)`` pairs, and a leg's ranks need not be
    contiguous: the semantic leg hands over the host's own positions, most of
    which never survive its filter (:func:`_by_semantic`). Ranks are explicit
    rather than re-derived from list position precisely so that filtering a
    ranking cannot silently promote what is left.

    Ties keep first-seen order (dict insertion + a stable sort), so a single
    ranking fuses to itself byte-for-byte — concept-only retrieval is unchanged
    from before the FTS leg existed.
    """
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranking in rankings:
        for rank, row in ranking:
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(row["id"], row)
    return sorted(rows.values(), key=lambda r: -scores[r["id"]])


def trajectory_candidates(
    conn: sqlite3.Connection, concepts: list[str], query: str = "",
    scan_cap: int = 40, semantic: list[str] | None = None,
) -> list[dict]:
    """Read-only: ``[loop-run]`` note rows for every available leg, fused.

    Returns ``[{id, title, date, frontmatter}]`` in fused rank order, at most
    ``scan_cap`` per leg. Each leg degrades independently: empty ``concepts``,
    an empty ``query``, and a ``semantic`` ranking of ``None`` (the leg did not
    run — :func:`semantic_ranking`) each just drop out of the fusion; all
    absent gives ``[]``. ``semantic`` is appended last so a run without it
    fuses byte-identically to the two-leg (#100) rail, ties included.

    The FTS leg is best-effort only while another leg is carrying: a broken
    ``notes_fts`` with nothing else *retrieved* raises to the caller's
    degrade-to-unprimed guard. The semantic leg does not rescue it — a broken
    index is a loud fact, not something a working third leg papers over.
    """
    rankings = []
    if concepts:
        rankings.append(_by_concepts(conn, concepts, scan_cap))
    match = _fts_match_expr(query)
    if match:
        try:
            rankings.append(_by_fts(conn, match, scan_cap))
        except sqlite3.Error:
            if not any(rankings):
                raise
    if semantic:
        rankings.append(_by_semantic(conn, semantic, scan_cap))
    return _rrf(rankings)


def note_bodies(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """Read-only: ``{id: body_text}`` for the given ids that are ``type='note'``.

    The type guard is load-bearing: a ``builds_on`` list may name a decision or
    session id, and prime serves only insight-note bodies as color. Ids that
    don't resolve are simply absent from the mapping.
    """
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, body_text FROM notes WHERE type = 'note' AND id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["id"]: (r["body_text"] or "").strip() for r in rows}


def resolve_db_path(db: str | None, vault: str | None) -> str | None:
    """Resolve the read-only index db path without importing thinkweave.

    ``--db`` wins; else derive from ``--vault``: ``<weave_dir>/index.db`` when
    the vault's config.toml overrides ``weave_dir`` (PR #10), otherwise the
    legacy ``<vault>/.weave/index.db``; else ``THINKWEAVE_INDEX_DB``. Returns
    None when nothing resolves — the prime then serves an empty (unprimed)
    block rather than guessing a path (never touch an ambient real vault).
    """
    if db:
        return db
    if vault:
        vault_root = Path(vault)
        weave_dir = _read_weave_dir_override(vault_root) or (vault_root / ".weave")
        return str(weave_dir / "index.db")
    return os.environ.get("THINKWEAVE_INDEX_DB") or None
