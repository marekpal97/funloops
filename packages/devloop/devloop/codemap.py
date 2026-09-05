"""Architecture rail: committed map.json projected from code (dec-462f4b28).

map.json — one shard per package dir (the nearest ancestor owning a
pyproject.toml, else the repo root) — records module → responsibility →
public symbols + signatures → imports, sorted and byte-deterministic.
ONE producer: the SQLite index a pinned codegraph (``CODEGRAPH_VERSION``)
writes at ``<root>/.codegraph/codegraph.db``, opened through
``index_client.open_ro`` so index_client stays the package's one sqlite3
importer (boundary spec §5's per-database allowlist seam). Signatures are
codegraph's stored raw source text VERBATIM — including its own 102-char
truncation ellipsis; nothing is re-parsed or "repaired" (repairing
truncated text once fabricated signatures), so the bytes are deterministic
per repo and independent of the running interpreter. (The stdlib-ast
fallback producer was dec-462f4b28's falsifier branch; it never fired and
was retired in fix round 5.)

The gate SELF-PROVISIONS the index for generate/check at the default db
path: a missing index runs ``codegraph init -y <root>``, an unopenable one
is reset and re-inited, a stale or query-corrupt one runs ``codegraph
index <root>`` once and re-verifies; the version pin is checked FIRST and
fails without spending a reindex (a wrong-versioned binary cannot cure
it). Freshness is ONE-DIRECTIONAL over the TRACKED files: each must be
present and sha256-matching in ``files.content_hash`` — whatever else
codegraph indexed (untracked scratch; it walks the filesystem, not git)
is ignored, and a tracked file the fresh index still lacks was DECLINED
by codegraph (legacy encoding, broken syntax): its entry carries a
deterministic ``unparsed: true`` marker instead of wedging the gate. The
binary resolves ``--codegraph-bin`` → ``$CODEGRAPH_BIN`` → bare
``codegraph`` on PATH; ``codegraph init`` writes a
``.codegraph/.gitignore`` that keeps the index out of git. An EXPLICIT
``--db`` is never provisioned — its problems fail loudly, and a tracked
file absent from it is stale, not declined (fixture dbs are exact).

Degradation splits on the artifact, and ADOPTION IS HEAD-AWARE (one
``_adopted`` oracle for all three verbs): the rail is adopted when any
healthy shard, HEAD-proven damaged file, or HEAD-committed shard deleted
from the working tree exists — so a ``git rm`` or foreign overwrite of
the committed shard is drift/damage, never silent un-adoption — or when,
absent all proof, a damaged file sits at a path we would render to (a
half-written first generate). An adopted repo with no working codegraph
fails LOUD (MapError, exit 2 — never a silent producer flip); a repo
with no adoption evidence at all no-ops honestly (``check`` exit 0, the
read views say "not adopted" instead of erroring).

The map is git-anchored: the root resolves to ``git rev-parse
--show-toplevel`` (so a subdirectory invocation cannot mint a second key
space) and the file list is ``git ls-files`` — TRACKED files only, a
stated rule: the gate compares against the committed map and the PR
ships tracked content, so an unstaged new module is invisible to both
until ``git add``. Inside generate/check a git failure where a repo
plainly exists (a ``.git`` ancestor) is a MapError, never a silent
fallback keyspace; only a genuinely non-git root falls back to the given
root and a pruned rglob. One pruning rule (``_mappable``) decides what
maps at all: a path under a dot-dir or ``SKIP_DIRS`` is never listed,
never projected from the index, and never scanned for shards — even when
git tracks it.

Responsibility one-liners live beside each shard in map.notes.json,
keyed by module path with the interface hash the note described::

    {"pkg/mod.py": {"hash": "<12 hex>", "note": "one line"}}

Current hashes surface in generate/check reports (``unannotated`` /
``stale_notes``); the hash covers the module path too, so a note pasted
onto the wrong module registers as stale. A note whose hash no longer
matches is stale: ``check`` fails naming the module, and the
responsibility drops to null rather than serve a lie. The sidecar gets
the same provenance heal as shards: an unparseable map.notes.json (merge
conflict — two PRs each adding a note WILL collide) whose HEAD copy
parses serves HEAD's notes, ``check`` reports it under ``damaged_notes``
and ``generate`` restores HEAD's bytes (``healed_notes``); without HEAD
proof the only honest remedy is a hand-fix, and the error says so.

``check`` is PURE: it regenerates in memory and compares against the
bytes on disk, never writing — a red check stays red until ``devloop
map`` (the one mutating entry point) regenerates the shards and the
result is committed. ``generate`` also prunes shards that no longer
have modules and reports sidecar keys with no module (``orphan_notes``).
Every prune, overwrite and read decision goes through the
shard/damaged/foreign trichotomy (``_classify_map_file``), and all
three entry points consume the same sets. A healthy shard of ours is
managed — and healthy includes shape: ``modules`` must be a dict of
dicts, so a null-moduled file never crashes a consumer. A DAMAGED file
(merge conflict, half-write, wrong shape — not valid JSON, or git HEAD's
copy parses as a shard) is healed by ``generate`` where a shard renders;
where none renders, HEAD proof makes it a prunable orphan of ours
(recoverable from git), and WITHOUT that proof it is treated like
foreign — never deleted, never failing the gate (a JSONC tile config
lands here). ``check`` reports the ours-damaged set under ``damaged``,
a committed shard deleted from the working tree under ``missing_shards``
(one no longer rendered is a pending prune-commit, not a failure), and a
foreign file squatting a rendered path under ``squatted`` — naming the
problem instead of fabricating per-module drift; the read views skip any
damaged file with a warning rather than dying — they are how the map
reaches a dispatch. A foreign map.json (valid JSON without our shape) is
left alone, and one squatting a shard path is a refusal, never an
overwrite.

Test modules (under a ``tests`` dir or named ``test_*.py``) carry a
``tests`` count instead of a symbols list: they dominate raw symbol
counts without being interface surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from devloop import index_client

CODEGRAPH_VERSION = "1.6.0"  # pinned; project_metadata.indexed_with_version must match
MAP_NAME = "map.json"
NOTES_NAME = "map.notes.json"
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", "venv"}


class MapError(Exception):
    """Configuration/contract failure (exit 2 territory), never code drift."""


# ---------------------------------------------------------------------------
# Git anchoring


def _git(root, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, errors="replace",
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _resolve_root(root, *, required: bool = False) -> Path:
    """``required`` (generate/check): a git failure where a repo plainly
    exists must not silently mint a different keyspace mid-gate (F4)."""
    top = _git(root, "rev-parse", "--show-toplevel")
    if top:
        return Path(top.strip())
    resolved = Path(root).resolve()
    if required and any((p / ".git").exists() for p in (resolved, *resolved.parents)):
        raise MapError(
            f"git failed (absent, hung or timed out) resolving {resolved} — "
            "the map is git-anchored; fix git and re-run")
    return resolved


def _mappable(rel: str) -> bool:
    """THE one pruning rule: a path under a dot-dir or SKIP_DIRS never maps —
    not listed, not projected from the index, not scanned for shards — even
    when git tracks it. One rule, three consumers, so generate can always
    re-run over its own output."""
    return not any(p.startswith(".") or p in SKIP_DIRS
                   for p in PurePosixPath(rel).parts)


def _py_files(root: Path, *, required: bool = False) -> list[str]:
    # ponytail: TRACKED files only, by choice — the gate compares against the
    # committed map and the PR ships tracked content, so an unstaged new
    # module is invisible here exactly as it is invisible to the PR; `git
    # add` makes it appear in both. The trade: gate stays green until the
    # add. Upgrade path: union --others --exclude-standard.
    out = _git(root, "ls-files", "-z", "--", "*.py")
    if out is not None:
        return sorted(f for f in out.split("\0") if f and _mappable(f))
    if required and (root / ".git").exists():
        raise MapError(
            f"git ls-files failed under {root} — the map is git-anchored; "
            "fix git and re-run")
    return sorted(
        rel for p in root.rglob("*.py")
        if _mappable(rel := str(p.relative_to(root).as_posix()))
    )


# ---------------------------------------------------------------------------
# Shared shape helpers


def _is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return "tests" in parts[:-1] or parts[-1].startswith("test_")


def _raw() -> dict:
    return {"symbols": [], "internal": set(), "external": set(), "tests": 0}


def _finish(raw: dict, path: str) -> dict:
    entry: dict = {"responsibility": None}
    if _is_test(path):
        entry["tests"] = raw["tests"]
    else:
        entry["symbols"] = sorted(raw["symbols"], key=lambda s: (s["name"], s["kind"]))
    entry["imports_internal"] = sorted(raw["internal"] - {path})
    entry["imports_external"] = sorted(raw["external"] - {"__future__"})
    return entry


def _suffix_index(files: list[str]) -> dict[str, str]:
    """Every '/'-boundary suffix of every path → its lexicographically-first
    owner (``files`` is sorted, so first insertion wins — the documented
    tie-break for same-named modules in different packages)."""
    idx: dict[str, str] = {}
    for f in files:
        parts = PurePosixPath(f).parts
        for i in range(len(parts)):
            idx.setdefault("/".join(parts[i:]), f)
    return idx


def _resolve(dotted: str, idx: dict[str, str]) -> str | None:
    """Dotted import name → repo-relative indexed file, or None (external)."""
    cand = dotted.replace(".", "/")
    return idx.get(f"{cand}.py") or idx.get(f"{cand}/__init__.py")


# ---------------------------------------------------------------------------
# The codegraph index: self-provisioning + freshness


def _codegraph_bin(cg_bin: str | None) -> str:
    return cg_bin or os.environ.get("CODEGRAPH_BIN") or "codegraph"


def _run_codegraph(root: Path, cg_bin: str | None, *verb: str) -> None:
    bin_ = _codegraph_bin(cg_bin)
    try:
        r = subprocess.run([bin_, *verb, str(root)], cwd=root,
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MapError(
            f"codegraph unavailable (`{bin_}`: {e}) — install it (or point "
            "--codegraph-bin/$CODEGRAPH_BIN at it), or regenerate the map on "
            "a machine that has it") from None
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
        raise MapError(f"`{bin_} {verb[0]}` failed in {root}: {' '.join(tail)}")


def _check_pin(conn) -> None:
    """The version pin is FATAL, checked before any reindex is spent —
    reindexing with a wrong-versioned binary cannot cure it (F3)."""
    row = conn.execute(
        "SELECT value FROM project_metadata WHERE key='indexed_with_version'"
    ).fetchone()
    found = row[0] if row else "absent"
    if found != CODEGRAPH_VERSION:
        raise MapError(
            f"codegraph index version {found} != pinned {CODEGRAPH_VERSION} "
            "— re-verify the projection contract (test_map.py) before "
            "bumping codemap.CODEGRAPH_VERSION")


def _stale_problem(conn, root: Path, disk_files: list[str],
                   *, allow_missing: bool) -> str | None:
    """Freshness is ONE-DIRECTIONAL over TRACKED files (F2): every tracked
    .py must be present and hash-matching in the index; extra indexed files
    (untracked scratch — real codegraph walks the filesystem, not git) are
    ignored. ``allow_missing`` (after a reindex): a tracked file the fresh
    index still lacks was DECLINED by codegraph — the projection marks it
    ``unparsed`` instead of wedging the gate (F4)."""
    indexed = dict(conn.execute(
        "SELECT path, content_hash FROM files WHERE language='python'"))
    stale = set()
    for p in disk_files:
        if p not in indexed:
            if not allow_missing:
                stale.add(p)
            continue
        try:
            data = (root / p).read_bytes()
        except OSError:
            stale.add(p)  # listed by git but unreadable/gone: the index can't match
            continue
        if hashlib.sha256(data).hexdigest() != indexed[p]:
            stale.add(p)
    if stale:
        names = sorted(stale)
        shown = ", ".join(names[:10]) + (f" +{len(names) - 10} more" if len(names) > 10 else "")
        return f"codegraph index is behind the worktree ({shown})"
    return None


def _open_ro(db_path: Path):
    try:
        return index_client.open_ro(str(db_path))
    except index_client.Error as e:
        raise MapError(f"cannot open codegraph index {db_path}: {e}") from None


def _open_fresh_index(root: Path, db_path: Path, disk_files: list[str],
                      cg_bin: str | None, provision: bool):
    """An open connection to a version-pinned, worktree-fresh index — when
    ``provision`` is set (the DEFAULT db path), running codegraph as needed:
    init for a missing index, a reset + reinit for one that cannot even be
    opened (F10), one full reindex for a stale or query-corrupt one. The
    version pin is checked first and fails without a reindex. An explicit
    --db is never provisioned: its problems raise as-is, and a tracked file
    absent from it is stale, not declined (fixture dbs are exact)."""
    if not db_path.is_file():
        if not provision:
            raise MapError(
                f"codegraph index not found at {db_path} — run `codegraph "
                "init` there or pass --db")
        if db_path.exists():  # a directory (or other non-file) squats the path
            try:
                shutil.rmtree(db_path.parent)
            except OSError as e:
                raise MapError(
                    f"cannot reset broken index dir {db_path.parent}: {e}") from None
        _run_codegraph(root, cg_bin, "init", "-y")
        if not db_path.is_file():
            raise MapError(f"`codegraph init` completed but wrote no index at {db_path}")
    try:
        conn = _open_ro(db_path)
    except MapError:
        if not provision:
            raise
        try:
            shutil.rmtree(db_path.parent)
        except OSError as e:
            raise MapError(
                f"cannot reset broken index dir {db_path.parent}: {e}") from None
        _run_codegraph(root, cg_bin, "init", "-y")
        conn = _open_ro(db_path)
    try:
        _check_pin(conn)
        problem = _stale_problem(conn, root, disk_files, allow_missing=False)
    except index_client.Error as e:  # opens lazily; queries reveal corruption
        problem = f"codegraph index unreadable ({e})"
    except MapError:
        conn.close()
        raise
    if problem and provision:
        conn.close()
        _run_codegraph(root, cg_bin, "index", "-q")
        conn = _open_ro(db_path)
        try:
            _check_pin(conn)
            problem = _stale_problem(conn, root, disk_files, allow_missing=True)
        except index_client.Error as e:
            conn.close()
            raise MapError(f"codegraph index unreadable after reindex ({e})") from None
        except MapError:
            conn.close()
            raise
    if problem:
        conn.close()
        raise MapError(f"{problem} — reindex with `codegraph index`")
    return conn


# ---------------------------------------------------------------------------
# Projection


def _project_codegraph(conn, disk_files: list[str]) -> dict[str, dict]:
    """Index rows → module entries for the TRACKED files only — whatever
    else real codegraph indexed (untracked scratch; it walks the filesystem,
    not git) never maps. The caller guarantees version pin and freshness;
    signatures are the stored raw text, verbatim. A tracked file the fresh
    index lacks was declined by codegraph (legacy encoding, broken syntax):
    it gets a deterministic ``unparsed: true`` marker instead of failing the
    whole map."""
    files = list(disk_files)  # already sorted + pruned
    indexed = {p for (p,) in conn.execute(
        "SELECT path FROM files WHERE language='python'")}
    raw = {p: _raw() for p in files}
    seen: set[tuple[str, str]] = set()
    for path, kind, name, qual, sig in conn.execute(
        "SELECT file_path, kind, name, qualified_name, signature FROM nodes "
        "WHERE kind IN ('function','class','variable') "
        "ORDER BY file_path, start_line, id"
    ):
        if path not in raw or name != qual:  # unindexed file, or nested ('::')
            continue
        if (path, name) in seen:  # duplicate binding: first by line wins
            continue
        seen.add((path, name))
        if _is_test(path):
            if kind == "function" and name.startswith("test_"):
                raw[path]["tests"] += 1
        elif not name.startswith("_"):
            sym = {"kind": kind, "name": name}
            if sig:
                sym["signature"] = sig
            raw[path]["symbols"].append(sym)
    for src, tgt in conn.execute(
        "SELECT source, target FROM edges WHERE kind='imports' "
        "AND source LIKE 'file:%' AND target LIKE 'file:%'"
    ):
        s, t = src[5:], tgt[5:]
        if s in raw and t in raw:
            raw[s]["internal"].add(t)
    suffixes = _suffix_index(files)
    for path, name in conn.execute(
        "SELECT file_path, name FROM nodes WHERE kind='import'"
    ):
        if path in raw and _resolve(name, suffixes) is None:
            raw[path]["external"].add(name.split(".")[0])
    out = {}
    for p, r in raw.items():
        out[p] = _finish(r, p)
        if p not in indexed:
            out[p]["unparsed"] = True
    return out


# ---------------------------------------------------------------------------
# Sharding, sidecar, rendering


def _shard_dir(root: Path, path: str) -> str:
    d = PurePosixPath(path).parent
    while str(d) != ".":
        if (root / d / "pyproject.toml").is_file():
            return str(d)
        d = d.parent
    return "."


# Our shards are tens of KB; anything bigger is someone else's data and is
# never even parsed (a geo tilemap can be tens of MB).
_SHARD_MAX_BYTES = 2_000_000


def _shard_doc(doc) -> bool:
    # any int version >= 1: a future schema bump must still recognize (and
    # be able to regenerate over) the shards its predecessor wrote. Shape
    # counts too: a null/mis-typed 'modules' must never classify healthy —
    # consumers index straight into it (F3).
    return (isinstance(doc, dict) and isinstance(doc.get("version"), int)
            and doc.get("version") >= 1
            and isinstance(doc.get("generator"), dict)
            and "producer" in doc["generator"]
            and isinstance(doc.get("modules"), dict)
            and all(isinstance(e, dict) for e in doc["modules"].values()))


def _head_shard_doc(root: Path, rel: str) -> dict | None:
    """git HEAD's copy of the path, iff it parses as a shard — the proof a
    now-damaged file was ours."""
    head = _git(root, "show", f"HEAD:{rel}")
    if head is None:
        return None
    try:
        doc = json.loads(head)
    except json.JSONDecodeError:
        return None
    return doc if _shard_doc(doc) else None


def _sniff_marker(p: Path) -> bool:
    # sort_keys puts "generator" near the top of every shard we render, so
    # a prefix sniff rescues an oversized shard of OURS from the size guard
    # without ever reading a giant tilemap in full
    with p.open("rb") as f:
        head = f.read(2048)
    return b'"generator"' in head and b'"producer"' in head


def _classify_map_file(p: Path, root: Path):
    """('shard'|'damaged'|'foreign', text, doc, head_doc) — the trichotomy
    behind every prune, overwrite and read decision. map.json is a common
    filename (tilemaps, style files, manifests): a foreign one is never
    touched.

    Damaged evidence, either of: the text is not valid JSON at all (a merge
    conflict or half-write), or git HEAD's copy of the path parses as a
    shard (it WAS ours, whatever state — including wrong-shaped valid JSON —
    it is in now). head_doc is that HEAD shard when it exists — proof of
    ownership; a damaged file WITHOUT it only gets healed where a shard
    renders, and is otherwise treated like foreign (a JSONC tile config must
    never be deleted or fail the gate). Valid JSON that isn't shard-shaped —
    object or array — stays foreign without HEAD proof."""
    rel = str(p.relative_to(root).as_posix())
    try:
        if p.stat().st_size > _SHARD_MAX_BYTES and not _sniff_marker(p):
            return "foreign", None, None, None
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "foreign", None, None, None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return "damaged", text, None, _head_shard_doc(root, rel)
    if _shard_doc(doc):
        return "shard", text, doc, None
    head_doc = _head_shard_doc(root, rel)
    if head_doc is not None:
        return "damaged", text, None, head_doc
    return "foreign", text, doc, None


def _scan_map_files(root: Path):
    """One pass over every candidate map.json — THE adoption/damage oracle
    every entry point consumes: ({rel: (text, doc)} for our shards,
    {rel: head_doc | None} for damaged ones, {rel: head_doc} for shards HEAD
    holds but the working tree lost entirely — a git rm'd or clean-deleted
    committed shard is drift, never silent un-adoption). Foreign files are
    dropped here and never seen again. Adoption itself derives from these:
    a healthy shard, HEAD-proven damage, or a missing HEAD shard each prove
    the rail is adopted."""
    shards, damaged, missing = {}, {}, {}
    on_disk = set()
    for p in sorted(root.rglob(MAP_NAME)):
        rel = str(p.relative_to(root).as_posix())
        if not _mappable(rel):
            continue
        on_disk.add(rel)
        kind, text, doc, head_doc = _classify_map_file(p, root)
        if kind == "shard":
            shards[rel] = (text, doc)
        elif kind == "damaged":
            damaged[rel] = head_doc
    out = _git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    for rel in (out.split("\0") if out else []):
        if (rel and PurePosixPath(rel).name == MAP_NAME and _mappable(rel)
                and rel not in on_disk):
            hd = _head_shard_doc(root, rel)
            if hd is not None:
                missing[rel] = hd
    return shards, damaged, missing


def _adopted(root: Path, shards: dict, damaged: dict, missing: dict,
             *, required: bool = False) -> bool:
    """One adoption predicate for all three verbs (F1/F5): evidence is any
    healthy shard, any HEAD-proven damaged file, any HEAD shard deleted from
    the working tree — or, failing all proof, a damaged file sitting at a
    path we would render to (a half-written first generate is at minimum
    reportable damage; a JSONC tilemap elsewhere is not ours to flag)."""
    if shards or missing or any(hd is not None for hd in damaged.values()):
        return True
    if not damaged:
        return False
    files = _py_files(root, required=required)
    candidates = {MAP_NAME if d == "." else f"{d}/{MAP_NAME}"
                  for d in {_shard_dir(root, f) for f in files}}
    return bool(set(damaged) & candidates)


def _interface_hash(path: str, entry: dict) -> str:
    surface = [path, entry["tests"] if "tests" in entry else entry["symbols"]]
    return hashlib.sha1(
        json.dumps(surface, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def _load_notes(root: Path, shard: str):
    """(notes dict, damaged sidecar rel | None, HEAD text | None). The
    sidecar gets the same provenance heal as shards: unparseable (a merge
    conflict — two PRs each adding a note WILL collide) with a HEAD copy
    that parses as a dict → serve HEAD's notes and report the file damaged
    (generate writes the HEAD bytes back; check goes red naming it). No
    HEAD proof → the only honest remedy is a hand-fix, so say so."""
    p = root / shard / NOTES_NAME
    rel = NOTES_NAME if shard == "." else f"{shard}/{NOTES_NAME}"
    if not p.is_file():
        return {}, None, None
    try:
        notes = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        head = _git(root, "show", f"HEAD:{rel}")
        try:
            head_notes = json.loads(head) if head is not None else None
        except json.JSONDecodeError:
            head_notes = None
        if isinstance(head_notes, dict):
            return head_notes, rel, head
        raise MapError(
            f"{p}: not valid JSON ({e}) and no committed copy to restore "
            "from — fix the file by hand") from None
    return (notes if isinstance(notes, dict) else {}), None, None


def _assemble(root: Path, modules: dict[str, dict]):
    """Fold sidecar notes in; render one deterministic doc per shard.

    Returns ({shard relpath: rendered text}, stale, unannotated, orphans,
    {damaged sidecar rel: HEAD text to restore}).
    """
    gen = {"producer": "codegraph", "codegraph": CODEGRAPH_VERSION}
    shards: dict[str, dict] = {}
    for path, entry in modules.items():
        shards.setdefault(_shard_dir(root, path), {})[path] = entry
    rendered, stale, unannotated, orphans = {}, [], [], []
    damaged_notes: dict[str, str] = {}
    for sdir in sorted(shards):
        notes, damaged_rel, head_text = _load_notes(root, sdir)
        if damaged_rel is not None:
            damaged_notes[damaged_rel] = head_text
        orphans.extend(sorted(set(notes) - set(shards[sdir])))
        folded = {}
        for path in sorted(shards[sdir]):
            entry = shards[sdir][path]
            h = _interface_hash(path, entry)
            note = notes.get(path)
            if isinstance(note, dict) and note.get("hash") == h:
                entry = {**entry, "responsibility": note.get("note")}
            elif isinstance(note, dict):
                stale.append({"module": path, "hash": h, "note_hash": note.get("hash")})
            else:
                unannotated.append({"module": path, "hash": h})
            folded[path] = entry
        doc = {"version": 1, "generator": gen, "modules": folded}
        rel = MAP_NAME if sdir == "." else f"{sdir}/{MAP_NAME}"
        rendered[rel] = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    return rendered, stale, unannotated, sorted(orphans), damaged_notes


def _build(root: Path, db: str | None, cg_bin: str | None,
           scanned=None) -> dict:
    """Project + assemble, no shard writes (self-provisioning may write the
    index). ``root`` must already be strictly resolved."""
    disk_shards, damaged, missing = (scanned if scanned is not None
                                     else _scan_map_files(root))
    files = _py_files(root, required=True)
    db_path = Path(db) if db else root / ".codegraph" / "codegraph.db"
    conn = _open_fresh_index(root, db_path, files, cg_bin, provision=db is None)
    try:
        modules = _project_codegraph(conn, files)
    except index_client.Error as e:
        raise MapError(f"codegraph index unreadable ({db_path}): {e}") from None
    finally:
        conn.close()
    rendered, stale, unannotated, orphans, damaged_notes = _assemble(root, modules)
    return {"rendered": rendered, "stale": stale,
            "unannotated": unannotated, "orphan_notes": orphans,
            "module_count": len(modules), "damaged_notes": damaged_notes,
            "disk_shards": disk_shards, "damaged": damaged, "missing": missing}


# ---------------------------------------------------------------------------
# Public interface


def generate(root, *, db: str | None = None,
             codegraph_bin: str | None = None) -> dict:
    """Regenerate and write every shard, prune orphaned ones; report what an
    annotator needs. The ONE mutating entry point."""
    root = _resolve_root(root, required=True)
    b = _build(root, db, codegraph_bin)
    existing = set(b["disk_shards"])
    damaged = set(b["damaged"])
    healed, foreign = [], []
    for rel in sorted(b["rendered"]):
        if rel in existing or not (root / rel).exists():
            continue
        (healed if rel in damaged else foreign).append(rel)
    if foreign:
        raise MapError(
            f"{', '.join(foreign)}: a file already exists there and is not a "
            "devloop shard — move it, or delete it and re-run")
    for rel, text in b["rendered"].items():
        (root / rel).write_text(text, encoding="utf-8")
    # prune shards WE authored that no longer have modules — including a
    # damaged one whose HEAD copy proves it was ours (recoverable from git);
    # a damaged file WITHOUT that proof is left alone like a foreign one
    orphaned_damaged = {rel for rel, hd in b["damaged"].items()
                        if hd is not None and rel not in b["rendered"]}
    removed = sorted((existing - set(b["rendered"])) | orphaned_damaged)
    for rel in removed:
        (root / rel).unlink()
    # a conflicted sidecar heals from provenance too: restore HEAD's bytes
    # (the notes already folded came from that same HEAD copy)
    for rel, head_text in b["damaged_notes"].items():
        (root / rel).write_text(head_text, encoding="utf-8")
    return {"producer": "codegraph", "shards": sorted(b["rendered"]),
            "modules": b["module_count"], "stale_notes": b["stale"],
            "unannotated": b["unannotated"], "removed_shards": removed,
            "orphan_notes": b["orphan_notes"], "healed": healed,
            "healed_notes": sorted(b["damaged_notes"])}


def check(root, *, db: str | None = None,
          codegraph_bin: str | None = None) -> dict:
    """Regenerate in memory + diff against the bytes on disk; fail naming the
    module. Pure: never writes shards, so a red check cannot self-clear — the
    fix is `devloop map` plus a commit. A repo with NO committed shard (and
    no damaged one of ours) has not adopted the rail: honest no-op, no
    codegraph needed."""
    root = _resolve_root(root, required=True)
    scanned = _scan_map_files(root)
    shards, damaged, missing = scanned
    if not _adopted(root, shards, damaged, missing, required=True):
        return {"ok": True, "adopted": False,
                "note": f"map rail not adopted — no committed {MAP_NAME} "
                        f"under {root}; run `devloop map` to adopt it"}
    b = _build(root, db, codegraph_bin, scanned=scanned)
    old = b["disk_shards"]
    # a damaged shard is reported as such, not as a wall of per-module
    # drift — `devloop map` is the remedy (heal at a rendered path, prune
    # for a HEAD-proven orphan). A damaged file with neither a rendered
    # path nor HEAD proof is not ours to flag (the JSONC-tilemap ponytail).
    damaged_ours = sorted(rel for rel, hd in b["damaged"].items()
                          if rel in b["rendered"] or hd is not None)
    # a committed shard deleted from the working tree is drift, named as
    # such; one still committed but not rendered any more is a pending
    # prune-commit, not a failure
    missing_shards = sorted(set(b["missing"]) & set(b["rendered"]))
    # foreign JSON squatting a rendered path: name the squat itself rather
    # than fabricate per-module drift with a remedy that dead-ends in
    # generate's refusal
    squatted = sorted(rel for rel in b["rendered"]
                      if rel not in old and rel not in b["damaged"]
                      and rel not in b["missing"] and (root / rel).exists())
    drifted, generator_changed = [], []
    for rel, text in b["rendered"].items():
        if (rel in damaged_ours or rel in missing_shards or rel in squatted
                or old.get(rel, (None,))[0] == text):
            continue
        before = old[rel][1]["modules"] if rel in old else {}
        after = json.loads(text)["modules"]
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        if changed:
            drifted.extend(changed)
        else:
            generator_changed.append(rel)
    removed_shards = sorted(set(old) - set(b["rendered"]))
    damaged_notes = sorted(b["damaged_notes"])
    # orphan_notes is advisory, not part of ok: an orphaned sidecar entry is
    # inert (nothing folds it into map.json), unlike a stale note which
    # would describe a live module wrongly — surfacing without gating spares
    # a sidecar-edit commit for every file deletion.
    ok = not (drifted or generator_changed or removed_shards or damaged_ours
              or missing_shards or squatted or damaged_notes or b["stale"])
    return {"ok": ok, "adopted": True, "producer": "codegraph",
            "shards": sorted(b["rendered"]),
            "drifted": drifted, "generator_changed": generator_changed,
            "removed_shards": removed_shards, "damaged": damaged_ours,
            "missing_shards": missing_shards, "squatted": squatted,
            "damaged_notes": damaged_notes,
            "stale_notes": b["stale"], "orphan_notes": b["orphan_notes"]}


# ---------------------------------------------------------------------------
# Read views


_NOT_ADOPTED = ("map rail not adopted — no committed " + MAP_NAME +
                "; run `devloop map` to adopt it")


def _committed_modules(root: Path):
    """(modules, damaged rels) — or (None, skipped) where the rail is not
    adopted (same `_adopted` oracle as the gate: a never-adopted repo's
    proofless JSONC map.json warns, never raises). A damaged file degrades
    the read views to a warning — they are how the map reaches a dispatch
    and must not die on a state `devloop map` (or nothing, for a JSONC
    tilemap) can clear — except when the rail IS adopted and nothing
    healthy remains, where an error is the only honest output."""
    shards, damaged, missing = _scan_map_files(root)
    skipped = sorted(damaged)
    if not shards:
        if _adopted(root, shards, damaged, missing):
            gone = sorted(set(skipped) | set(missing))
            raise MapError(
                f"{', '.join(gone)}: damaged or deleted and no healthy "
                f"{MAP_NAME} remains — run `devloop map`")
        return None, skipped
    modules: dict[str, dict] = {}
    for _, doc in shards.values():
        modules.update(doc["modules"])
    return modules, skipped


def _norm_path(p: str) -> str:
    """One normalization for every user-given focus/slice path (F9)."""
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _under(path: str, prefix: str) -> bool:
    """THE equal-or-under predicate — the only path-prefix match (F9)."""
    return path == prefix or path.startswith(prefix + "/")


def _focus_dirs(focus: list[str], modules: dict) -> list[str]:
    """A focus naming a module means its directory; anything else is a dir."""
    return [str(PurePosixPath(f).parent) if f in modules else f
            for f in (_norm_path(x) for x in focus)]


def _module_line(path: str, entry: dict) -> str:
    if "tests" in entry:
        return f"{path} - {entry['tests']} tests"
    resp = entry.get("responsibility")
    n = f"({len(entry.get('symbols', []))} symbols)"
    return f"{path} - {resp} {n}" if resp else f"{path} {n}"


def catalog(root, *, budget_lines: int, focus: list[str]) -> str:
    """Tier-1 rollup over the committed shards.

    Everything expands when it fits the budget; over budget, only the
    directories containing a focus path stay expanded and the rest roll up
    to one line each. ponytail: single-level dirname fold — a focus dir
    alone exceeding the budget still overflows; upgrade path is a
    recursive tree fold.
    """
    modules, skipped = _committed_modules(_resolve_root(root))
    if modules is None:
        return "\n".join([_NOT_ADOPTED] + [
            f"! {rel}: unparseable map.json skipped (no committed shard "
            "proves it ours)" for rel in skipped])
    warn = [f"! {rel}: damaged map.json skipped — run `devloop map`"
            for rel in skipped]
    full = [_module_line(p, modules[p]) for p in sorted(modules)]
    if len(full) <= budget_lines:
        return "\n".join(full + warn)
    groups: dict[str, dict] = {}
    for path, entry in modules.items():
        groups.setdefault(str(PurePosixPath(path).parent), {})[path] = entry
    focus_dirs = _focus_dirs(focus, modules)
    warn += [f"! focus '{fd}' matched nothing" for fd in focus_dirs
             if not any(_under(d, fd) for d in groups)]
    lines = []
    for d in sorted(groups):
        if any(_under(d, fd) for fd in focus_dirs):
            lines.extend(_module_line(p, groups[d][p]) for p in sorted(groups[d]))
        else:
            syms = sum(len(e["symbols"]) for e in groups[d].values() if "symbols" in e)
            tests = sum(e["tests"] for e in groups[d].values() if "tests" in e)
            parts = [f"{len(groups[d])} modules"]
            if syms:
                parts.append(f"{syms} symbols")
            if tests:
                parts.append(f"{tests} tests")
            lines.append(f"{d}/ - " + ", ".join(parts))
    return "\n".join(lines + warn)


def slice_modules(root, paths: list[str]) -> dict:
    """Tier-2 detail: full entries for modules under the given paths."""
    modules, skipped = _committed_modules(_resolve_root(root))
    if modules is None:
        out = {"modules": {}, "note": _NOT_ADOPTED}
        if skipped:
            out["skipped_damaged"] = skipped
        return out
    prefixes = [_norm_path(p) for p in paths]
    keep = {m: e for m, e in modules.items()
            if any(_under(m, p) for p in prefixes)}
    out = {"modules": keep}
    unmatched = [p for p in prefixes
                 if not any(_under(m, p) for m in modules)]
    if unmatched:
        out["unmatched"] = unmatched
    if skipped:
        out["skipped_damaged"] = skipped
    return out
