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

Producer parity is by construction, not by luck: every signature is
normalized through the same functions (`_function_sig` over parsed arg
nodes; `_variable_sig_from_text`, which re-parses the value expression
and unparses it, omitting anything unparseable or longer than
``VAR_SIG_MAX`` rather than truncating mid-token), and ``__future__``
never counts as an external import.

The map is git-anchored: the root resolves to ``git rev-parse
--show-toplevel`` (so a subdirectory invocation cannot mint a second key
space) and the file list comes from ``git ls-files`` (so untracked
scratch files never enter the committed artifact); outside a git repo
both fall back to the given root and a pruned rglob.

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
# mid-token): codegraph pre-truncates long values into unparseable text, so
# omission is the only rule both producers can agree on.
VAR_SIG_MAX = 120


class MapError(Exception):
    """Configuration/contract failure (exit 2 territory), never code drift."""


# ---------------------------------------------------------------------------
# Git anchoring


def _git(root, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def _resolve_root(root) -> Path:
    top = _git(root, "rev-parse", "--show-toplevel")
    return Path(top.strip()) if top else Path(root).resolve()


def _pruned(rel: PurePosixPath) -> bool:
    return any(p.startswith(".") or p in SKIP_DIRS for p in rel.parts)


def _py_files(root: Path) -> list[str]:
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
        return _collapse(text) or None
    return _function_sig(fn.args, fn.returns)


def _class_sig(bases: list[ast.expr], keywords) -> str | None:
    parts = [ast.unparse(b) for b in bases] + [ast.unparse(k) for k in keywords]
    return f"({', '.join(parts)})" if parts else None


def _class_sig_from_text(text: str) -> str | None:
    try:
        cls = ast.parse(f"class _C{_collapse(text)}: pass").body[0]
    except SyntaxError:
        return _collapse(text) or None
    return _class_sig(cls.bases, cls.keywords)


def _variable_sig_from_text(text: str) -> str | None:
    """One rule for both producers: re-parse the value, unparse it, omit
    anything unparseable (codegraph's pre-truncated long values) or over
    VAR_SIG_MAX (the ast side's full text for the same values)."""
    text = _collapse(text)
    if text.startswith("="):
        try:
            value = ast.unparse(ast.parse(text[1:].strip(), mode="eval"))
        except (SyntaxError, ValueError):
            return None
        sig = f"= {value}"
        return sig if len(sig) <= VAR_SIG_MAX else None
    return (text or None) if len(text) <= VAR_SIG_MAX else None


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
        if hashlib.sha256((root / p).read_bytes()).hexdigest() != indexed[p]:
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
    version-guarded def is public surface too."""
    for node in body:
        yield node
        if isinstance(node, _NESTING):
            blocks = [node.body, getattr(node, "orelse", []),
                      getattr(node, "finalbody", [])]
            blocks += [h.body for h in getattr(node, "handlers", [])]
            for block in blocks:
                yield from _module_stmts(block)


def _ast_module(tree: ast.Module, path: str, files: list[str]) -> dict:
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, "function", _function_sig(node.args, node.returns))
        elif isinstance(node, ast.ClassDef):
            add(node.name, "class", _class_sig(node.bases, node.keywords))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    add(t.id, "variable",
                        _variable_sig_from_text(f"= {ast.unparse(node.value)}"))
                elif isinstance(t, (ast.Tuple, ast.List)):
                    vals = (node.value.elts
                            if isinstance(node.value, (ast.Tuple, ast.List))
                            and len(node.value.elts) == len(t.elts)
                            else [None] * len(t.elts))
                    for e, v in zip(t.elts, vals):
                        if isinstance(e, ast.Name):
                            add(e.id, "variable",
                                _variable_sig_from_text(f"= {ast.unparse(v)}")
                                if v is not None else None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            text = (f"= {ast.unparse(node.value)}" if node.value is not None
                    else f": {ast.unparse(node.annotation)}")
            add(node.target.id, "variable", _variable_sig_from_text(text))

    pkg = list(PurePosixPath(path).parent.parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = _resolve(alias.name, files)
                if hit:
                    raw["internal"].add(hit)
                else:
                    raw["external"].add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                for alias in node.names:
                    hit = (_resolve(f"{node.module}.{alias.name}", files)
                           or _resolve(node.module or "", files))
                    if hit:
                        raw["internal"].add(hit)
                    elif node.module:
                        raw["external"].add(node.module.split(".")[0])
            else:  # relative: anchored at the file's own package, never external
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
    return _finish(raw, path)


def _project_ast(root: Path, files: list[str]) -> dict[str, dict]:
    out = {}
    for path in files:
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError, OSError):
            # deterministic marker (no message text: it varies by Python
            # version) — the map records the file exists and says why it
            # carries no surface, instead of aborting the whole map.
            marker = _finish(_raw(), path)
            marker["unparsed"] = True
            out[path] = marker
            continue
        out[path] = _ast_module(tree, path, files)
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


def _map_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob(MAP_NAME)
        if not _pruned(PurePosixPath(p.relative_to(root).as_posix()))
    )


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


def _resolve_producer(root: Path, producer: str | None, db: str | None):
    db_path = Path(db) if db else root / ".codegraph" / "codegraph.db"
    if producer is None:
        gens = set()
        for f in _map_files(root):
            try:
                gens.add(json.loads(f.read_text(encoding="utf-8"))
                         .get("generator", {}).get("producer"))
            except json.JSONDecodeError:
                pass  # corrupt shard: drift detection reports it
        gens -= {None}
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
    producer, db_path = _resolve_producer(root, producer, db)
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
            "module_count": len(modules)}


# ---------------------------------------------------------------------------
# Public interface


def generate(root, *, producer: str | None = None, db: str | None = None) -> dict:
    """Regenerate and write every shard, prune orphaned ones; report what an
    annotator needs. The ONE mutating entry point."""
    root = _resolve_root(root)
    existing = {str(p.relative_to(root).as_posix()) for p in _map_files(root)}
    b = _build(root, producer, db)
    for rel, text in b["rendered"].items():
        (root / rel).write_text(text, encoding="utf-8")
    removed = sorted(existing - set(b["rendered"]))
    for rel in removed:
        (root / rel).unlink()
    return {"producer": b["producer"], "shards": sorted(b["rendered"]),
            "modules": b["module_count"], "stale_notes": b["stale"],
            "unannotated": b["unannotated"], "removed_shards": removed,
            "orphan_notes": b["orphan_notes"]}


def check(root, *, producer: str | None = None, db: str | None = None) -> dict:
    """Regenerate in memory + diff against the bytes on disk; fail naming the
    module. Pure: never writes, so a red check cannot self-clear — the fix is
    `devloop map` plus a commit."""
    root = _resolve_root(root)
    old = {str(p.relative_to(root).as_posix()): p.read_text(encoding="utf-8")
           for p in _map_files(root)}
    b = _build(root, producer, db)
    drifted, generator_changed = [], []
    for rel, text in b["rendered"].items():
        if old.get(rel) == text:
            continue
        try:
            before = json.loads(old.get(rel, "{}")).get("modules", {})
        except json.JSONDecodeError:
            before = {}
        after = json.loads(text)["modules"]
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        if changed:
            drifted.extend(changed)
        else:
            generator_changed.append(rel)
    removed_shards = sorted(set(old) - set(b["rendered"]))
    ok = not (drifted or generator_changed or removed_shards or b["stale"])
    return {"ok": ok, "producer": b["producer"], "shards": sorted(b["rendered"]),
            "drifted": drifted, "generator_changed": generator_changed,
            "removed_shards": removed_shards, "stale_notes": b["stale"]}


def _committed_modules(root: Path) -> dict[str, dict]:
    files = _map_files(root)
    if not files:
        raise MapError(f"no {MAP_NAME} under {root} — run `devloop map` first")
    modules: dict[str, dict] = {}
    for f in files:
        try:
            modules.update(json.loads(f.read_text(encoding="utf-8")).get("modules", {}))
        except json.JSONDecodeError as e:
            raise MapError(f"{f}: not valid JSON ({e}) — regenerate with `devloop map`") from None
    return modules


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
    modules = _committed_modules(_resolve_root(root))
    full = [_module_line(p, modules[p]) for p in sorted(modules)]
    if len(full) <= budget_lines:
        return "\n".join(full)
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
    return "\n".join(lines)


def slice_modules(root, paths: list[str]) -> dict:
    """Tier-2 detail: full entries for modules under the given paths."""
    modules = _committed_modules(_resolve_root(root))
    prefixes = [p.rstrip("/") for p in paths]
    keep = {m: e for m, e in modules.items()
            if any(m == p or m.startswith(p + "/") for p in prefixes)}
    return {"modules": keep}
