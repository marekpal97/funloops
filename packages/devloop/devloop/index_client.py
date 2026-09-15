"""The package's single seam into the derived SQLite index.

Stdlib only, strictly read-only, and never imports ``thinkweave``. Every SQL
statement devloop issues lives here; ``trajectory.prime`` composes over the
rows and never speaks sqlite. Trajectory candidates come from a concept leg
and a full-text leg, fused by reciprocal rank fusion.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from pathlib import Path

# Re-exported so no caller imports sqlite3 itself.
Error = sqlite3.Error
Connection = sqlite3.Connection


def open_ro(db_path: str) -> sqlite3.Connection:
    """Open the derived index strictly read-only; it is never mutated."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_weave_dir_override(vault_root: Path) -> Path | None:
    """The vault's ``weave_dir`` override from its config.toml, else ``None``.
    A vault may keep its derived state off the vault path, so the live index
    is then ``<weave_dir>/index.db``. ``~`` expands and a relative value
    anchors at ``vault_root``; an unreadable config never raises."""
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

# The rail reads no vault config, so the fusion constant is fixed here.
RRF_K = 60

# The note body is never served, only its linked insights are.
_CANDIDATE_COLS = "n.id, n.title, n.date, n.frontmatter"

# fts5 treats `-` and `_` as word chars; mirror that so a term the index
# holds is a term we can ask for.
_FTS_TERM = re.compile(r"[0-9A-Za-z_-]{3,}")


def _fts_match_expr(text: str) -> str:
    """Encode free text as an OR-of-terms fts5 MATCH, ``''`` when no term
    survives. Only tokenizer-legal runs of 3+ chars are kept, so fts5
    operator syntax is unreachable; terms dedupe and cap at 24.

    ponytail: no stopword filter, so the leg can touch most of the index,
    about 100ms at 6k notes, paid once at claim time.
    """
    terms = list(dict.fromkeys(_FTS_TERM.findall(text)))[:24]
    return " OR ".join(f'"{t}"' for t in terms)


def _by_concepts(conn: sqlite3.Connection, concepts: list[str], scan_cap: int) -> list[dict]:
    """Leg 1: ``[loop-run]`` notes carrying ANY of the concepts, recency first."""
    placeholders = ",".join("?" * len(concepts))
    return [dict(r) for r in conn.execute(
        f"""SELECT DISTINCT {_CANDIDATE_COLS}
            FROM notes n
            JOIN note_tags t ON t.note_id = n.id AND t.tag = 'loop-run'
            JOIN note_concepts c ON c.note_id = n.id
            WHERE c.concept IN ({placeholders})
            ORDER BY n.date DESC, n.id DESC
            LIMIT ?""",
        [*concepts, scan_cap],
    )]


def _by_fts(conn: sqlite3.Connection, match: str, scan_cap: int) -> list[dict]:
    """Leg 2: ``[loop-run]`` notes matching the text, fts5 relevance order."""
    return [dict(r) for r in conn.execute(
        f"""SELECT {_CANDIDATE_COLS}
            FROM notes_fts f
            JOIN notes n ON n.rowid = f.rowid
            WHERE notes_fts MATCH ?
              AND EXISTS (SELECT 1 FROM note_tags t
                          WHERE t.note_id = n.id AND t.tag = 'loop-run')
            ORDER BY f.rank
            LIMIT ?""",
        [match, scan_cap],
    )]


def _rrf(rankings: list[list[dict]]) -> list[dict]:
    """Reciprocal rank fusion: ``score[id] = Σ 1/(RRF_K + rank_i)``, 1-indexed.
    Ties keep first-seen order, so a single ranking fuses to itself."""
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(row["id"], row)
    return sorted(rows.values(), key=lambda r: -scores[r["id"]])


def trajectory_candidates(
    conn: sqlite3.Connection, concepts: list[str], query: str = "",
    scan_cap: int = 40,
) -> list[dict]:
    """``[{id, title, date, frontmatter}]`` of ``[loop-run]`` notes from the
    concept and text legs, fused, at most ``scan_cap`` per leg. An empty leg
    is skipped; both empty gives ``[]``. A broken ``notes_fts`` raises only
    when no other leg retrieved anything."""
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
    return _rrf(rankings)


def note_rows(
    conn: sqlite3.Connection, ids: list[str], note_type: str = "note",
) -> dict[str, dict]:
    """``{id: {title, body}}`` for the given ids of ``note_type``; an id that
    does not resolve is absent. The type guard matters: a ``builds_on`` list
    may name a decision or session id, and prime serves only note bodies."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, body_text FROM notes WHERE type = ? AND id IN ({placeholders})",
        [note_type, *ids],
    ).fetchall()
    return {r["id"]: {"title": r["title"] or "", "body": (r["body_text"] or "").strip()}
            for r in rows}


def resolve_db_path(db: str | None, vault: str | None) -> str | None:
    """The index db path: ``--db``, else the vault's ``<weave_dir>/index.db``
    or ``<vault>/.weave/index.db``, else ``THINKWEAVE_INDEX_DB``. Returns
    ``None`` when nothing resolves; a path is never guessed."""
    if db:
        return db
    if vault:
        vault_root = Path(vault)
        weave_dir = _read_weave_dir_override(vault_root) or (vault_root / ".weave")
        return str(weave_dir / "index.db")
    return os.environ.get("THINKWEAVE_INDEX_DB") or None
