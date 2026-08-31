"""Architecture rail: committed map.json projected from code (dec-462f4b28).

map.json — one shard per package dir (the nearest ancestor owning a
pyproject.toml, else the repo root) — records module → responsibility →
public symbols + signatures → imports, sorted and byte-deterministic.
Two producers, one shape:

- ``codegraph`` (primary): project the SQLite index a pinned codegraph
  (``CODEGRAPH_VERSION``) wrote at ``<root>/.codegraph/codegraph.db``.
  Opened through ``index_client.open_ro`` so index_client stays the
  package's one sqlite3 importer (boundary spec §5's allowlist seam).
- ``ast`` (fallback): stdlib ast over the tree — for repos without Node
  or an index (a fresh loop worktree never carries one).

The committed artifact pins its producer: generate/check reuse the
``generator`` recorded in existing shards, so a machine that happens to
have an index never silently flips a repo committed with the fallback
(or vice versa); switching producers is an explicit ``--producer`` act.

Responsibility one-liners live beside each shard in map.notes.json,
keyed by module path with the interface hash the note described::

    {"pkg/mod.py": {"hash": "<12 hex>", "note": "one line"}}

Current hashes surface in generate/check reports (``unannotated`` /
``stale_notes``). A note whose hash no longer matches its module's
public surface is stale: ``check`` fails naming the module, and the
responsibility drops to null rather than serve a lie.

``check`` regenerates in place and compares against the bytes that were
on disk — on a clean worktree exactly "regenerate + git diff
--exit-code", the gate shape the decision names — so a red check leaves
the corrected map.json behind to inspect and commit.

Test modules (under a ``tests`` dir or named ``test_*.py``) carry a
``tests`` count instead of a symbols list: they dominate raw symbol
counts without being interface surface.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath

from devloop import index_client

CODEGRAPH_VERSION = "1.6.0"  # pinned; project_metadata.indexed_with_version must match
MAP_NAME = "map.json"
NOTES_NAME = "map.notes.json"
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", "venv"}


class MapError(Exception):
    """Configuration/contract failure (exit 2 territory), never code drift."""


def _pruned(rel: PurePosixPath) -> bool:
    return any(p.startswith(".") or p in SKIP_DIRS for p in rel.parts)


def _is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return "tests" in parts[:-1] or parts[-1].startswith("test_")


def _collapse(sig: str) -> str:
    return " ".join(sig.split())


def _raw() -> dict:
    return {"symbols": [], "internal": set(), "external": set(), "tests": 0}


def _finish(raw: dict, path: str) -> dict:
    entry: dict = {"responsibility": None}
    if _is_test(path):
        entry["tests"] = raw["tests"]
    else:
        entry["symbols"] = sorted(raw["symbols"], key=lambda s: (s["name"], s["kind"]))
    entry["imports_internal"] = sorted(raw["internal"] - {path})
    entry["imports_external"] = sorted(raw["external"])
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


def _project_codegraph(conn) -> dict[str, dict]:
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
    files = sorted(p for (p,) in conn.execute(
        "SELECT path FROM files WHERE language='python'"))
    raw = {p: _raw() for p in files}
    for path, kind, name, qual, sig in conn.execute(
        "SELECT file_path, kind, name, qualified_name, signature FROM nodes "
        "WHERE kind IN ('function','class','variable')"
    ):
        if path not in raw or name != qual:  # unindexed file, or nested ('::')
            continue
        if _is_test(path):
            if kind == "function" and name.startswith("test_"):
                raw[path]["tests"] += 1
        elif not name.startswith("_"):
            sym = {"kind": kind, "name": name}
            if sig:
                sym["signature"] = _collapse(sig)
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


def _ast_signature(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        sig = f"({ast.unparse(node.args)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
        return _collapse(sig)
    if isinstance(node, ast.ClassDef) and node.bases:
        return _collapse(f"({', '.join(ast.unparse(b) for b in node.bases)})")
    if isinstance(node, ast.Assign):
        return _collapse(f"= {ast.unparse(node.value)}")[:120]
    if isinstance(node, ast.AnnAssign):
        if node.value is not None:
            return _collapse(f"= {ast.unparse(node.value)}")[:120]
        return _collapse(f": {ast.unparse(node.annotation)}")
    return None


def _ast_module(tree: ast.Module, path: str, files: list[str]) -> dict:
    raw = _raw()
    for node in tree.body:
        names: list[tuple[str, str]] = []  # (name, kind)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [(node.name, "function")]
        elif isinstance(node, ast.ClassDef):
            names = [(node.name, "class")]
        elif isinstance(node, ast.Assign):
            names = [(t.id, "variable") for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [(node.target.id, "variable")]
        for name, kind in names:
            if _is_test(path):
                if kind == "function" and name.startswith("test_"):
                    raw["tests"] += 1
            elif not name.startswith("_"):
                sym = {"kind": kind, "name": name}
                sig = _ast_signature(node)
                if sig:
                    sym["signature"] = sig
                raw["symbols"].append(sym)
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


def _project_ast(root: Path) -> dict[str, dict]:
    files = sorted(
        str(p.relative_to(root).as_posix())
        for p in root.rglob("*.py")
        if not _pruned(PurePosixPath(p.relative_to(root).as_posix()))
    )
    out = {}
    for path in files:
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
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


def _interface_hash(entry: dict) -> str:
    surface = entry["tests"] if "tests" in entry else entry["symbols"]
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

    Returns ({shard relpath: rendered bytes-as-str}, stale, unannotated).
    """
    gen = ({"producer": "codegraph", "codegraph": CODEGRAPH_VERSION}
           if producer == "codegraph" else {"producer": "ast"})
    shards: dict[str, dict] = {}
    for path, entry in modules.items():
        shards.setdefault(_shard_dir(root, path), {})[path] = entry
    rendered, stale, unannotated = {}, [], []
    for sdir in sorted(shards):
        notes = _load_notes(root, sdir)
        folded = {}
        for path in sorted(shards[sdir]):
            entry = shards[sdir][path]
            h = _interface_hash(entry)
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
    return rendered, stale, unannotated


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


def _build(root: Path, producer: str | None, db: str | None):
    producer, db_path = _resolve_producer(root, producer, db)
    if producer == "codegraph":
        if not db_path.is_file():
            raise MapError(
                f"codegraph index not found at {db_path} — run `codegraph init` "
                "there, pass --db, or regenerate with --producer ast")
        conn = index_client.open_ro(str(db_path))
        try:
            modules = _project_codegraph(conn)
        finally:
            conn.close()
    else:
        modules = _project_ast(root)
    rendered, stale, unannotated = _assemble(root, modules, producer)
    return producer, rendered, stale, unannotated


# ---------------------------------------------------------------------------
# Public interface


def generate(root: Path, *, producer: str | None = None, db: str | None = None) -> dict:
    """Regenerate and write every shard; report what an annotator needs."""
    root = Path(root)
    producer, rendered, stale, unannotated = _build(root, producer, db)
    for rel, text in rendered.items():
        (root / rel).write_text(text, encoding="utf-8")
    n = sum(len(json.loads(t)["modules"]) for t in rendered.values())
    return {"producer": producer, "shards": sorted(rendered), "modules": n,
            "stale_notes": stale, "unannotated": unannotated}


def check(root: Path, *, producer: str | None = None, db: str | None = None) -> dict:
    """Regenerate + diff against what was on disk; fail naming the module."""
    root = Path(root)
    old = {str(p.relative_to(root).as_posix()): p.read_text(encoding="utf-8")
           for p in _map_files(root)}
    producer, rendered, stale, _ = _build(root, producer, db)
    drifted = []
    for rel, text in rendered.items():
        if old.get(rel) == text:
            continue
        try:
            before = json.loads(old.get(rel, "{}")).get("modules", {})
        except json.JSONDecodeError:
            before = {}
        after = json.loads(text)["modules"]
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        drifted.extend(changed or [f"{rel} (generator)"])
        (root / rel).write_text(text, encoding="utf-8")
    for rel in sorted(set(old) - set(rendered)):
        drifted.append(f"{rel} (removed shard)")
    ok = not drifted and not stale
    return {"ok": ok, "producer": producer, "shards": sorted(rendered),
            "drifted": drifted, "stale_notes": stale}


def _committed_modules(root: Path) -> dict[str, dict]:
    root = Path(root)
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
        return f"{path} — {entry['tests']} tests"
    resp = entry.get("responsibility")
    n = f"({len(entry['symbols'])} symbols)"
    return f"{path} — {resp} {n}" if resp else f"{path} {n}"


def catalog(root: Path, *, budget_lines: int, focus: list[str]) -> str:
    """Tier-1 rollup over the committed shards.

    Everything expands when it fits the budget; over budget, only the
    directories containing a focus path stay expanded and the rest roll up
    to one line each. ponytail: single-level dirname fold — a focus dir
    alone exceeding the budget still overflows; upgrade path is a
    recursive tree fold.
    """
    modules = _committed_modules(root)
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
            lines.append(f"{d}/ — " + ", ".join(parts))
    return "\n".join(lines)


def slice_modules(root: Path, paths: list[str]) -> dict:
    """Tier-2 detail: full entries for modules under the given paths."""
    modules = _committed_modules(root)
    prefixes = [p.rstrip("/") for p in paths]
    keep = {m: e for m, e in modules.items()
            if any(m == p or m.startswith(p + "/") for p in prefixes)}
    return {"modules": keep}
