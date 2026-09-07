"""Enforcing seams for the devloop/ boundary spec (docs/agents/devloop-boundaries.md).

Three blast-radius contracts the spec states in prose and this file makes
falsifiable:

1. **Importer allowlist** — which devloop modules may import ``sqlite3``:
   ``{index_client}`` since #100 moved prime's SQL into the seam, so the
   singleton is enforced not remembered.
2. **No thinkweave** — the deterministic plane never imports the host it was
   carved out of (boundary spec §1). Enforced here because funloops CI has no
   thinkweave installed; without this seam the coupling would only surface as a
   confusing ImportError in some later slice.
3. **SQL home** — which modules contain SQL at all: the same singleton.
   codegraph is invoked as a CLI and its text spliced (#28, dec-d2de831e),
   so the thinkweave index is the only database devloop speaks to.

The spec's third seam, the schema pin, stayed behind in thinkweave: it builds
its fixture index by importing the *real thinkweave indexer*, so it can only run
where thinkweave does (carve-out import rule, thinkweave#148).
"""

from __future__ import annotations

import ast
from pathlib import Path

import devloop

PKG_ROOT = Path(devloop.__file__).resolve().parent


def _modules() -> list[tuple[str, ast.AST]]:
    """Every module in the package as (dotted name, parsed tree)."""
    out = []
    for py in sorted(PKG_ROOT.rglob("*.py")):
        rel = py.relative_to(PKG_ROOT.parent).with_suffix("")
        name = ".".join(p for p in rel.parts if p != "__init__")
        out.append((name, ast.parse(py.read_text(encoding="utf-8"))))
    return out


def _imports_root(tree: ast.AST, root: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == root for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == root:
            return True
    return False


def test_index_client_is_the_only_sqlite3_importer():
    importers = {name for name, tree in _modules() if _imports_root(tree, "sqlite3")}
    # #100 moved prime's SQL into index_client — the seam is a singleton.
    assert importers == {"devloop.index_client"}


def test_no_module_imports_thinkweave():
    assert {name for name, tree in _modules() if _imports_root(tree, "thinkweave")} == set()


def test_sql_home_invariant():
    """§5: every SQL string devloop issues lives in index_client. A SELECT
    appearing anywhere else is a new database seam nobody designed."""
    speakers = {
        name for name, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "SELECT " in node.value and " FROM " in node.value
    }
    assert speakers == {"devloop.index_client"}
