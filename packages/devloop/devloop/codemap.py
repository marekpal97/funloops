"""Architecture rail: committed map.json projected from code (dec-462f4b28).

map.json — one shard per package dir (the nearest ancestor owning a
pyproject.toml, else the repo root) — records module → responsibility →
public symbols + signatures → imports, sorted and byte-deterministic.
Two producers, one shape:

- ``codegraph`` (primary): project the SQLite index a pinned codegraph
  (``CODEGRAPH_VERSION``) wrote at ``<root>/.codegraph/codegraph.db``.
  Opened through ``index_client.open_ro`` so index_client stays the
  package's one sqlite3 importer (boundary spec §5's allowlist seam).
  The projection refuses a stale index: every mapped file's sha256 must
  match ``files.content_hash``, and a .py on disk missing from the index
  (or vice versa) fails loudly — the map never describes old code.
- ``ast`` (fallback): stdlib ast over the tree — for repos without Node
  or an index (a fresh loop worktree never carries one). A file that
  cannot be read or parsed gets a deterministic ``"unparsed": true``
  entry instead of aborting the map.

Producer parity is by construction: every signature is normalized
through the same functions (`_function_sig` over parsed arg nodes;
`_variable_sig_from_text`, which re-parses the value expression and
unparses it, omitting anything unparseable or longer than
``VAR_SIG_MAX`` — with the omission decided on the RAW source length,
codegraph's own truncation input — rather than cutting mid-token), only
module-level imports count on either side, and ``__future__`` never
counts as an external import. The one known residual is import-edge
granularity: codegraph's file-level edge lands on a package's
__init__.py where the ast side's per-alias resolution reaches the
defining module. The committed ``generator`` pin keeps any flip
explicit.

The map is git-anchored: the root resolves to ``git rev-parse
--show-toplevel`` (so a subdirectory invocation cannot mint a second key
space) and the file list is ``git ls-files`` — TRACKED files only, a
stated rule: the gate compares against the committed map and the PR
ships tracked content, so an unstaged new module is invisible to both
until ``git add``. Outside a git repo both fall back to the given root
and a pruned rglob. A tracked file deleted from the working tree drops
out of the map (codegraph producer: it fails freshness instead).

The committed artifact pins its producer: generate/check reuse the
``generator`` recorded in existing shards, so a machine that happens to
have an index never silently flips a repo committed with the fallback
(or vice versa); switching producers is an explicit ``--producer`` act.

Responsibility one-liners live beside each shard in map.notes.json,
keyed by module path with the interface hash the note described::

    {"pkg/mod.py": {"hash": "<12 hex>", "note": "one line"}}

Current hashes surface in generate/check reports (``unannotated`` /
``stale_notes``); the hash covers the module path too, so a note pasted
onto the wrong module registers as stale. A note whose hash no longer
matches is stale: ``check`` fails naming the module, and the
responsibility drops to null rather than serve a lie.

``check`` is PURE: it regenerates in memory and compares against the
bytes on disk, never writing — a red check stays red until ``devloop
map`` (the one mutating entry point) regenerates the shards and the
result is committed. ``generate`` also prunes shards that no longer
have modules and reports sidecar keys with no module (``orphan_notes``).
Every prune, overwrite and read decision goes through the
shard/damaged/foreign trichotomy (``_classify_map_file``), and all
three entry points consume the same sets. A healthy shard of ours is
managed. A DAMAGED file (merge conflict, half-write — not valid JSON,
or git HEAD's copy parses as a shard) is healed by ``generate`` where a
shard renders; where none renders, HEAD proof makes it a prunable
orphan of ours (recoverable from git), and WITHOUT that proof it is
treated like foreign — never deleted, never failing the gate (a JSONC
tile config lands here). ``check`` reports the ours-damaged set under
``damaged``; the read views skip any damaged file with a warning
rather than dying — they are how the map reaches a dispatch. A HEAD
copy that parses as a shard also carries the producer pin through a
heal, so healing never flips the generator just because an index
exists. A foreign map.json (valid JSON without our marker) is left
alone, and one squatting a shard path is a refusal, never an
overwrite.

Test modules (under a ``tests`` dir or named ``test_*.py``) carry a
``tests`` count instead of a symbols list: they dominate raw symbol
counts without being interface surface.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

from devloop import index_client

CODEGRAPH_VERSION = "1.6.0"  # pinned; project_metadata.indexed_with_version must match
MAP_NAME = "map.json"
NOTES_NAME = "map.notes.json"
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", "venv"}
# Variable values longer than this omit the signature entirely (never cut
# mid-token). 102 is codegraph 1.6.0's own truncation point for stored
# variable signatures (measured on a live index): a matching cap keeps the
# producers aligned across the whole range — anything codegraph would
# truncate, the ast side omits too.
VAR_SIG_MAX = 102


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
        return None  # a hung git (network fs, stale lock) degrades to the fallback
    return r.stdout if r.returncode == 0 else None


def _resolve_root(root) -> Path:
    top = _git(root, "rev-parse", "--show-toplevel")
    return Path(top.strip()) if top else Path(root).resolve()


def _pruned(rel: PurePosixPath) -> bool:
    return any(p.startswith(".") or p in SKIP_DIRS for p in rel.parts)


def _py_files(root: Path) -> list[str]:
    # ponytail: TRACKED files only, by choice — the gate compares against the
    # committed map and the PR ships tracked content, so an unstaged new
    # module is invisible here exactly as it is invisible to the PR; `git
    # add` makes it appear in both. The trade: gate stays green until the
    # add. Upgrade path: union --others --exclude-standard.
    out = _git(root, "ls-files", "-z", "--", "*.py")
    if out is not None:
        return sorted(f for f in out.split("\0") if f)
    return sorted(
        str(p.relative_to(root).as_posix())
        for p in root.rglob("*.py")
        if not _pruned(PurePosixPath(p.relative_to(root).as_posix()))
    )


# ---------------------------------------------------------------------------
# Shared shape helpers — the parity layer both producers go through


def _is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return "tests" in parts[:-1] or parts[-1].startswith("test_")


def _collapse(sig: str) -> str:
    return " ".join(sig.split())


def _function_sig(args: ast.arguments, returns: ast.expr | None) -> str:
    sig = f"({ast.unparse(args)})"
    if returns is not None:
        sig += f" -> {ast.unparse(returns)}"
    return sig


def _function_sig_from_text(text: str) -> str | None:
    try:
        fn = ast.parse(f"def _f{_collapse(text)}: pass").body[0]
    except SyntaxError:
        return None  # unparseable (e.g. truncated) text: omit, never emit fragments
    return _function_sig(fn.args, fn.returns)


def _class_sig(bases: list[ast.expr], keywords) -> str | None:
    parts = [ast.unparse(b) for b in bases] + [ast.unparse(k) for k in keywords]
    return f"({', '.join(parts)})" if parts else None


def _class_sig_from_text(text: str) -> str | None:
    try:
        cls = ast.parse(f"class _C{_collapse(text)}: pass").body[0]
    except SyntaxError:
        return None  # unparseable text: omit, never emit fragments
    return _class_sig(cls.bases, cls.keywords)


def _variable_sig_from_text(text: str) -> str | None:
    """One rule for both producers: re-parse the value (or annotation),
    unparse it, omit anything unparseable (codegraph's pre-truncated long
    values) or over VAR_SIG_MAX (the ast side's full text for the same
    values)."""
    text = _collapse(text)
    prefix = text[:1]
    if prefix not in ("=", ":"):
        return None
    try:
        value = ast.unparse(ast.parse(text[1:].strip(), mode="eval"))
    except (SyntaxError, ValueError):
        return None
    sig = f"{prefix} {value}"
    return sig if len(sig) <= VAR_SIG_MAX else None


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


def _resolve(dotted: str, files: list[str]) -> str | None:
    """Dotted import name → repo-relative indexed file, or None (external).

    ponytail: suffix match — two same-named modules in different packages
    resolve to the lexicographically first; upgrade path is anchoring on
    declared package roots.
    """
    cand = dotted.replace(".", "/")
    for suffix in (f"{cand}.py", f"{cand}/__init__.py"):
        hit = next((f for f in files if f == suffix or f.endswith("/" + suffix)), None)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------------------
# Producer: codegraph (primary)


def _check_freshness(conn, root: Path, disk_files: list[str]) -> None:
    indexed = {p: h for p, h in conn.execute(
        "SELECT path, content_hash FROM files WHERE language='python'")}
    stale = set(disk_files) ^ set(indexed)
    for p in set(disk_files) & set(indexed):
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
        raise MapError(
            f"codegraph index is behind the worktree ({shown}) — "
            "reindex or use --producer ast")


def _project_codegraph(conn, root: Path, disk_files: list[str]) -> dict[str, dict]:
    row = conn.execute(
        "SELECT value FROM project_metadata WHERE key='indexed_with_version'"
    ).fetchone()
    found = row[0] if row else "absent"
    if found != CODEGRAPH_VERSION:
        raise MapError(
            f"codegraph index version {found} != pinned {CODEGRAPH_VERSION} — "
            "re-verify the projection contract (test_map.py) before bumping "
            "codemap.CODEGRAPH_VERSION"
        )
    _check_freshness(conn, root, disk_files)
    files = sorted(p for (p,) in conn.execute(
        "SELECT path FROM files WHERE language='python'"))
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
            norm = {"function": _function_sig_from_text,
                    "class": _class_sig_from_text}.get(kind, _variable_sig_from_text)
            sym = {"kind": kind, "name": name}
            normalized = norm(sig) if sig else None
            if normalized:
                sym["signature"] = normalized
            raw[path]["symbols"].append(sym)
    for src, tgt in conn.execute(
        "SELECT source, target FROM edges WHERE kind='imports' "
        "AND source LIKE 'file:%' AND target LIKE 'file:%'"
    ):
        s, t = src[5:], tgt[5:]
        if s in raw and t in raw:
            raw[s]["internal"].add(t)
    for path, name in conn.execute(
        "SELECT file_path, name FROM nodes WHERE kind='import'"
    ):
        if path in raw and _resolve(name, files) is None:
            raw[path]["external"].add(name.split(".")[0])
    return {p: _finish(r, p) for p, r in raw.items()}


# ---------------------------------------------------------------------------
# Producer: ast (fallback)

_NESTING = (ast.If, ast.Try, ast.With) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ())


def _module_stmts(body):
    """Module-level statements, descending into if/try/with blocks — a
    version-guarded def is public surface too. The block set is deliberate,
    not exhaustive: match/for/while/async-with definitions stay invisible."""
    for node in body:
        yield node
        if isinstance(node, _NESTING):
            blocks = [node.body, getattr(node, "orelse", []),
                      getattr(node, "finalbody", [])]
            blocks += [h.body for h in getattr(node, "handlers", [])]
            for block in blocks:
                yield from _module_stmts(block)


def _ast_var_sig(src: str, prefix: str, value_node) -> str | None:
    """codegraph truncates the RAW source text at VAR_SIG_MAX before storing
    it, so the omission decision must be made on the raw segment length —
    normalization shortens text (collapsed lines, single quotes) and a
    normalized-only cap would keep values codegraph drops."""
    seg = ast.get_source_segment(src, value_node)
    if seg is None or len(prefix) + 1 + len(seg) > VAR_SIG_MAX:
        return None
    return _variable_sig_from_text(f"{prefix} {ast.unparse(value_node)}")


def _ast_module(tree: ast.Module, path: str, files: list[str], src: str) -> dict:
    raw = _raw()
    seen: set[str] = set()

    def add(name: str, kind: str, sig: str | None) -> None:
        if name in seen:  # duplicate binding (reassignment, if/else twin)
            return
        seen.add(name)
        if _is_test(path):
            if kind == "function" and name.startswith("test_"):
                raw["tests"] += 1
        elif not name.startswith("_"):
            sym = {"kind": kind, "name": name}
            if sig:
                sym["signature"] = sig
            raw["symbols"].append(sym)

    for node in _module_stmts(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # module-level only (guarded blocks included): codegraph's
            # file-level import view never sees a function-local import,
            # so counting them here would break producer parity.
            _add_imports(node, path, files, raw)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, "function", _function_sig(node.args, node.returns))
        elif isinstance(node, ast.ClassDef):
            add(node.name, "class", _class_sig(node.bases, node.keywords))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    add(t.id, "variable", _ast_var_sig(src, "=", node.value))
                elif isinstance(t, (ast.Tuple, ast.List)):
                    vals = (node.value.elts
                            if isinstance(node.value, (ast.Tuple, ast.List))
                            and len(node.value.elts) == len(t.elts)
                            else [None] * len(t.elts))
                    for e, v in zip(t.elts, vals):
                        if isinstance(e, ast.Name):
                            add(e.id, "variable",
                                _ast_var_sig(src, "=", v) if v is not None else None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            add(node.target.id, "variable",
                _ast_var_sig(src, "=", node.value) if node.value is not None
                else _ast_var_sig(src, ":", node.annotation))

    return _finish(raw, path)


def _add_imports(node, path: str, files: list[str], raw: dict) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            hit = _resolve(alias.name, files)
            if hit:
                raw["internal"].add(hit)
            else:
                raw["external"].add(alias.name.split(".")[0])
    elif node.level == 0:
        for alias in node.names:
            hit = (_resolve(f"{node.module}.{alias.name}", files)
                   or _resolve(node.module or "", files))
            if hit:
                raw["internal"].add(hit)
            elif node.module:
                raw["external"].add(node.module.split(".")[0])
    else:  # relative: anchored at the file's own package, never external
        pkg = list(PurePosixPath(path).parent.parts)
        base = pkg if node.level == 1 else pkg[: len(pkg) - (node.level - 1)]
        anchor = "/".join(base + (node.module or "").split("."))
        anchor = anchor.strip("/")
        for alias in node.names:
            for cand in (f"{anchor}/{alias.name}.py",
                         f"{anchor}/{alias.name}/__init__.py",
                         f"{anchor}.py", f"{anchor}/__init__.py"):
                if cand.strip("/") in files:
                    raw["internal"].add(cand.strip("/"))
                    break


def _project_ast(root: Path, files: list[str]) -> dict[str, dict]:
    out = {}
    for path in files:
        try:
            text = (root / path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue  # tracked but deleted from the working tree: no source, no module
        except (OSError, UnicodeDecodeError):
            text = None
        try:
            tree = ast.parse(text, filename=path) if text is not None else None
        except (SyntaxError, ValueError):
            tree = None
        if tree is None:
            # present but unreadable/unparseable: deterministic marker (no
            # message text — it varies by Python version) instead of
            # aborting the whole map.
            marker = _finish(_raw(), path)
            marker["unparsed"] = True
            out[path] = marker
            continue
        out[path] = _ast_module(tree, path, files, text)
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
    # be able to regenerate over) the shards its predecessor wrote
    return (isinstance(doc, dict) and isinstance(doc.get("version"), int)
            and doc.get("version") >= 1
            and isinstance(doc.get("generator"), dict)
            and "producer" in doc["generator"])


def _head_shard_doc(root: Path, rel: str) -> dict | None:
    """git HEAD's copy of the path, iff it parses as a shard — the proof a
    now-damaged file was ours, and the survivor of its producer pin."""
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
    shard (it WAS ours, whatever state it is in now). head_doc is that HEAD
    shard when it exists — proof of ownership AND the surviving producer
    pin; a damaged file WITHOUT it only gets healed where a shard renders,
    and is otherwise treated like foreign (a JSONC tile config must never
    be deleted or fail the gate). Valid JSON without our marker — object or
    array — stays foreign."""
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
    """One pass over every candidate map.json: ({rel: (text, doc)} for our
    shards, {rel: head_doc | None} for damaged ones). Foreign files are
    dropped here and never seen again."""
    shards, damaged = {}, {}
    for p in sorted(root.rglob(MAP_NAME)):
        rel = str(p.relative_to(root).as_posix())
        if _pruned(PurePosixPath(rel)):
            continue
        kind, text, doc, head_doc = _classify_map_file(p, root)
        if kind == "shard":
            shards[rel] = (text, doc)
        elif kind == "damaged":
            damaged[rel] = head_doc
    return shards, damaged


def _interface_hash(path: str, entry: dict) -> str:
    surface = [path,
               entry["tests"] if "tests" in entry else entry["symbols"],
               entry.get("unparsed", False)]
    return hashlib.sha1(
        json.dumps(surface, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def _load_notes(root: Path, shard: str) -> dict:
    p = root / shard / NOTES_NAME
    if not p.is_file():
        return {}
    try:
        notes = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise MapError(f"{p}: not valid JSON ({e})") from None
    return notes if isinstance(notes, dict) else {}


def _assemble(root: Path, modules: dict[str, dict], producer: str):
    """Fold sidecar notes in; render one deterministic doc per shard.

    Returns ({shard relpath: rendered text}, stale, unannotated, orphans).
    """
    gen = ({"producer": "codegraph", "codegraph": CODEGRAPH_VERSION}
           if producer == "codegraph" else {"producer": "ast"})
    shards: dict[str, dict] = {}
    for path, entry in modules.items():
        shards.setdefault(_shard_dir(root, path), {})[path] = entry
    rendered, stale, unannotated, orphans = {}, [], [], []
    for sdir in sorted(shards):
        notes = _load_notes(root, sdir)
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
    return rendered, stale, unannotated, sorted(orphans)


def _resolve_producer(root: Path, shards: dict, damaged: dict,
                      producer: str | None, db: str | None):
    db_path = Path(db) if db else root / ".codegraph" / "codegraph.db"
    if producer is None:
        # a damaged shard's pin survives in its HEAD copy — healing must
        # never flip the producer just because an index happens to exist
        gens = ({doc["generator"].get("producer") for _, doc in shards.values()}
                | {hd["generator"].get("producer")
                   for hd in damaged.values() if hd is not None}) - {None}
        if len(gens) > 1:
            raise MapError(
                "committed shards disagree on generator.producer — regenerate "
                "with an explicit --producer")
        producer = gens.pop() if gens else (
            "codegraph" if db_path.is_file() else "ast")
    if producer not in ("codegraph", "ast"):
        raise MapError(f"unknown producer '{producer}' (codegraph | ast)")
    return producer, db_path


def _build(root: Path, producer: str | None, db: str | None) -> dict:
    """Project + assemble, no writes. ``root`` must already be resolved."""
    disk_shards, damaged = _scan_map_files(root)
    producer, db_path = _resolve_producer(root, disk_shards, damaged, producer, db)
    files = _py_files(root)
    if producer == "codegraph":
        if not db_path.is_file():
            raise MapError(
                f"codegraph index not found at {db_path} — run `codegraph init` "
                "there, pass --db, or regenerate with --producer ast")
        try:
            conn = index_client.open_ro(str(db_path))
        except index_client.Error as e:
            raise MapError(f"cannot open codegraph index {db_path}: {e}") from None
        try:
            modules = _project_codegraph(conn, root, files)
        except index_client.Error as e:
            raise MapError(f"codegraph index unreadable ({db_path}): {e}") from None
        finally:
            conn.close()
    else:
        modules = _project_ast(root, files)
    rendered, stale, unannotated, orphans = _assemble(root, modules, producer)
    return {"producer": producer, "rendered": rendered, "stale": stale,
            "unannotated": unannotated, "orphan_notes": orphans,
            "module_count": len(modules),
            "disk_shards": disk_shards, "damaged": damaged}


# ---------------------------------------------------------------------------
# Public interface


def generate(root, *, producer: str | None = None, db: str | None = None) -> dict:
    """Regenerate and write every shard, prune orphaned ones; report what an
    annotator needs. The ONE mutating entry point."""
    root = _resolve_root(root)
    b = _build(root, producer, db)
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
    return {"producer": b["producer"], "shards": sorted(b["rendered"]),
            "modules": b["module_count"], "stale_notes": b["stale"],
            "unannotated": b["unannotated"], "removed_shards": removed,
            "orphan_notes": b["orphan_notes"], "healed": healed}


def check(root, *, producer: str | None = None, db: str | None = None) -> dict:
    """Regenerate in memory + diff against the bytes on disk; fail naming the
    module. Pure: never writes, so a red check cannot self-clear — the fix is
    `devloop map` plus a commit."""
    root = _resolve_root(root)
    b = _build(root, producer, db)
    old = b["disk_shards"]
    # a damaged shard is reported as such, not as a wall of per-module
    # drift — `devloop map` is the remedy (heal at a rendered path, prune
    # for a HEAD-proven orphan). A damaged file with neither a rendered
    # path nor HEAD proof is not ours to flag (the JSONC-tilemap ponytail).
    damaged = sorted(rel for rel, hd in b["damaged"].items()
                     if rel in b["rendered"] or hd is not None)
    drifted, generator_changed = [], []
    for rel, text in b["rendered"].items():
        if rel in damaged or old.get(rel, (None,))[0] == text:
            continue
        before = old[rel][1].get("modules", {}) if rel in old else {}
        after = json.loads(text)["modules"]
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        if changed:
            drifted.extend(changed)
        else:
            generator_changed.append(rel)
    removed_shards = sorted(set(old) - set(b["rendered"]))
    # orphan_notes is advisory, not part of ok: an orphaned sidecar entry is
    # inert (nothing folds it into map.json), unlike a stale note which
    # would describe a live module wrongly — surfacing without gating spares
    # a sidecar-edit commit for every file deletion.
    ok = not (drifted or generator_changed or removed_shards or damaged
              or b["stale"])
    return {"ok": ok, "producer": b["producer"], "shards": sorted(b["rendered"]),
            "drifted": drifted, "generator_changed": generator_changed,
            "removed_shards": removed_shards, "damaged": damaged,
            "stale_notes": b["stale"], "orphan_notes": b["orphan_notes"]}


def _committed_modules(root: Path):
    """(modules, damaged rels). A damaged file degrades the read views to a
    warning — they are how the map reaches a dispatch and must not die on
    a state `devloop map` (or nothing, for a JSONC tilemap) can clear —
    except when NO healthy shard remains, where an error is the only
    honest output."""
    shards, damaged = _scan_map_files(root)
    skipped = sorted(damaged)
    if not shards:
        if skipped:
            raise MapError(
                f"{', '.join(skipped)}: damaged (unparseable or conflicted) "
                f"and no healthy {MAP_NAME} remains — run `devloop map`")
        raise MapError(f"no {MAP_NAME} under {root} — run `devloop map` first")
    modules: dict[str, dict] = {}
    for _, doc in shards.values():
        modules.update(doc.get("modules", {}))
    return modules, skipped


def _focus_dirs(focus: list[str], modules: dict) -> list[str]:
    """A focus naming a module means its directory; anything else is a dir."""
    return [str(PurePosixPath(f).parent) if f.rstrip("/") in modules else f.rstrip("/")
            for f in (x.rstrip("/") for x in focus)]


def _in_focus(dirname: str, focus_dirs: list[str]) -> bool:
    return any(dirname == fd or dirname.startswith(fd + "/") for fd in focus_dirs)


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
    warn = [f"! {rel}: damaged map.json skipped — run `devloop map`"
            for rel in skipped]
    full = [_module_line(p, modules[p]) for p in sorted(modules)]
    if len(full) <= budget_lines:
        return "\n".join(full + warn)
    groups: dict[str, dict] = {}
    for path, entry in modules.items():
        groups.setdefault(str(PurePosixPath(path).parent), {})[path] = entry
    focus_dirs = _focus_dirs(focus, modules)
    lines = []
    for d in sorted(groups):
        if _in_focus(d, focus_dirs):
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
    prefixes = [p.rstrip("/") for p in paths]
    keep = {m: e for m, e in modules.items()
            if any(m == p or m.startswith(p + "/") for p in prefixes)}
    out = {"modules": keep}
    if skipped:
        out["skipped_damaged"] = skipped
    return out
