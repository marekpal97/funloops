"""Seams for the map rail (issue #27, dec-462f4b28): committed map.json
projected from codegraph's SQLite (primary) or Python ast (fallback).

The seams, one per acceptance criterion:
- byte-determinism: two runs on an unchanged repo → identical bytes
- ``map --check`` failure names the drifted module
- sidecar (map.notes.json) staleness names its module
- the codegraph version pin fails loudly on a bump; the fixture db doubles
  as the schema contract (projection queries run against a 1.6.0-shaped db)
- ast fallback produces the same map.json shape — asserted against a FIXED
  hand-written expected entry, never projection A == projection A
- catalog respects --budget-lines by expanding only the focus subtree
"""

from __future__ import annotations

import json
import sqlite3
import textwrap

import pytest

from devloop import codemap
from devloop.cli import main

# ---------------------------------------------------------------------------
# Fixture repo (ast producer) and fixture codegraph db (codegraph producer).
# Both describe the SAME tiny source tree, so the shape-parity expectations
# below are one hand-written dict, not a cross-projection comparison.

CORE_SRC = textwrap.dedent(
    """\
    import json
    from fx import helper

    LIMIT = 5

    def run(x: int) -> str:
        return helper.fmt(x)

    def _private() -> None:
        pass
    """
)

# Hand-written expected entries (the independent source of truth).
EXPECTED_CORE = {
    "responsibility": None,
    "symbols": [
        {"kind": "variable", "name": "LIMIT", "signature": "= 5"},
        {"kind": "function", "name": "run", "signature": "(x: int) -> str"},
    ],
    "imports_internal": ["fx/helper.py"],
    "imports_external": ["json"],
}
EXPECTED_HELPER = {
    "responsibility": None,
    "symbols": [{"kind": "function", "name": "fmt", "signature": "(x: int) -> str"}],
    "imports_internal": [],
    "imports_external": [],
}
EXPECTED_TESTS = {
    "responsibility": None,
    "tests": 2,
    "imports_internal": [],
    "imports_external": [],
}


def make_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fx"\n')
    pkg = tmp_path / "fx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(CORE_SRC)
    (pkg / "helper.py").write_text("def fmt(x: int) -> str:\n    return str(x)\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n"
    )
    return tmp_path


# CREATE statements copied from a real codegraph 1.6.0 index — the schema
# contract: if the projection queries columns 1.6.0 does not have, these
# fixtures fail before any funloops reindex would.
CODEGRAPH_SCHEMA = """
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
    qualified_name TEXT NOT NULL, file_path TEXT NOT NULL, language TEXT NOT NULL,
    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    start_column INTEGER NOT NULL, end_column INTEGER NOT NULL,
    docstring TEXT, signature TEXT, visibility TEXT,
    is_exported INTEGER DEFAULT 0, is_async INTEGER DEFAULT 0,
    is_static INTEGER DEFAULT 0, is_abstract INTEGER DEFAULT 0,
    decorators TEXT, type_parameters TEXT, return_type TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
    target TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT,
    line INTEGER, col INTEGER, provenance TEXT DEFAULT NULL
);
CREATE TABLE files (
    path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, language TEXT NOT NULL,
    size INTEGER NOT NULL, modified_at INTEGER NOT NULL, indexed_at INTEGER NOT NULL,
    node_count INTEGER DEFAULT 0, errors TEXT, generated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE project_metadata (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
);
"""


def make_codegraph_db(path, version: str):
    db = sqlite3.connect(path)
    db.executescript(CODEGRAPH_SCHEMA)
    db.execute("INSERT INTO project_metadata VALUES ('indexed_with_version', ?, 0)", (version,))
    for f in ("fx/__init__.py", "fx/core.py", "fx/helper.py", "tests/test_core.py"):
        db.execute("INSERT INTO files VALUES (?, 'h', 'python', 1, 0, 0, 0, NULL, 0)", (f,))
    node = ("INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, "
            "start_line, end_line, start_column, end_column, signature, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'python', 1, 1, 0, 0, ?, 0)")
    db.execute(node, ("n1", "function", "run", "run", "fx/core.py", "(x: int) -> str"))
    db.execute(node, ("n2", "variable", "LIMIT", "LIMIT", "fx/core.py", "= 5"))
    db.execute(node, ("n3", "function", "_private", "_private", "fx/core.py", "() -> None"))
    # nested symbol: '::'-qualified, must be excluded from the public surface
    db.execute(node, ("n4", "function", "inner", "run::inner", "fx/core.py", "()"))
    db.execute(node, ("n5", "import", "json", "json", "fx/core.py", None))
    db.execute(node, ("n6", "import", "fx.helper", "fx.helper", "fx/core.py", None))
    db.execute(node, ("n7", "function", "fmt", "fmt", "fx/helper.py", "(x: int) -> str"))
    db.execute(node, ("n8", "function", "test_a", "test_a", "tests/test_core.py", "()"))
    db.execute(node, ("n9", "function", "test_b", "test_b", "tests/test_core.py", "()"))
    db.execute("INSERT INTO edges (source, target, kind) VALUES "
               "('file:fx/core.py', 'file:fx/helper.py', 'imports')")
    db.commit()
    db.close()
    return path


def read_map(root):
    return json.loads((root / "map.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC: byte-identical across two runs on an unchanged repo


def test_map_bytes_identical_across_two_runs(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    first = (root / "map.json").read_bytes()
    codemap.generate(root)
    assert (root / "map.json").read_bytes() == first


# ---------------------------------------------------------------------------
# AC: ast fallback produces the map.json shape (fixed expected, no Node)


def test_ast_projection_matches_expected_shape(tmp_path):
    root = make_repo(tmp_path)
    report = codemap.generate(root)
    assert report["producer"] == "ast"
    doc = read_map(root)
    assert doc["generator"] == {"producer": "ast"}
    assert doc["modules"]["fx/core.py"] == EXPECTED_CORE
    assert doc["modules"]["fx/helper.py"] == EXPECTED_HELPER
    assert doc["modules"]["tests/test_core.py"] == EXPECTED_TESTS


def test_codegraph_projection_matches_same_expected_shape(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(tmp_path / "cg.db", codemap.CODEGRAPH_VERSION)
    report = codemap.generate(root, producer="codegraph", db=db)
    assert report["producer"] == "codegraph"
    doc = read_map(root)
    assert doc["generator"] == {"producer": "codegraph", "codegraph": codemap.CODEGRAPH_VERSION}
    assert doc["modules"]["fx/core.py"] == EXPECTED_CORE
    assert doc["modules"]["fx/helper.py"] == EXPECTED_HELPER
    assert doc["modules"]["tests/test_core.py"] == EXPECTED_TESTS


# ---------------------------------------------------------------------------
# AC: contract test pins the codegraph version; a bump fails loudly


def test_codegraph_version_bump_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(tmp_path / "cg.db", "9.9.9")
    with pytest.raises(codemap.MapError, match=r"9\.9\.9.*1\.6\.0|1\.6\.0.*9\.9\.9"):
        codemap.generate(root, producer="codegraph", db=db)


def test_codegraph_producer_without_index_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(codemap.MapError, match="codegraph"):
        codemap.generate(root, producer="codegraph")


# ---------------------------------------------------------------------------
# AC: --check fails naming the drifted module


def test_check_green_on_fresh_map(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    report = codemap.check(root)
    assert report["ok"] is True
    assert report["drifted"] == []


def test_check_names_the_drifted_module(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    helper = root / "fx" / "helper.py"
    helper.write_text(helper.read_text() + "\n\ndef extra() -> int:\n    return 1\n")
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["drifted"] == ["fx/helper.py"]


def test_check_exit_codes_via_cli(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert main(["map", "--root", str(root)]) == 0
    assert main(["map", "--check", "--root", str(root)]) == 0
    helper = root / "fx" / "helper.py"
    helper.write_text(helper.read_text() + "\n\ndef extra() -> int:\n    return 1\n")
    capsys.readouterr()
    assert main(["map", "--check", "--root", str(root)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert "fx/helper.py" in out["drifted"]


# ---------------------------------------------------------------------------
# AC: sidecar staleness names the module


def test_fresh_note_lands_as_responsibility(tmp_path):
    root = make_repo(tmp_path)
    report = codemap.generate(root)
    current = {h["module"]: h["hash"] for h in report["unannotated"]}
    (root / "map.notes.json").write_text(json.dumps({
        "fx/helper.py": {"hash": current["fx/helper.py"], "note": "formats things"},
    }))
    codemap.generate(root)
    assert read_map(root)["modules"]["fx/helper.py"]["responsibility"] == "formats things"


def test_stale_note_names_its_module(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    (root / "map.notes.json").write_text(json.dumps({
        "fx/helper.py": {"hash": "000000000000", "note": "formats things"},
    }))
    report = codemap.check(root)
    assert report["ok"] is False
    assert [s["module"] for s in report["stale_notes"]] == ["fx/helper.py"]
    # a stale note never serves: the responsibility drops out rather than lie
    assert read_map(root)["modules"]["fx/helper.py"]["responsibility"] is None


# ---------------------------------------------------------------------------
# AC: catalog respects --budget-lines, expanding only the focus subtree


def make_two_tree_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "two"\n')
    for d in ("alpha", "beta"):
        (tmp_path / d).mkdir()
        for m in ("one", "two", "three"):
            (tmp_path / d / f"{m}.py").write_text(f"def {m}_{d}() -> None:\n    pass\n")
    return tmp_path


def test_catalog_expands_only_the_focus_subtree(tmp_path):
    root = make_two_tree_repo(tmp_path)
    codemap.generate(root)
    out = codemap.catalog(root, budget_lines=5, focus=["alpha/one.py"])
    lines = out.splitlines()
    assert len(lines) <= 5
    assert any(line.startswith("alpha/one.py") for line in lines)
    assert not any(line.startswith("beta/one.py") for line in lines)
    assert any(line.startswith("beta/ ") for line in lines)  # rolled up


def test_catalog_expands_everything_within_budget(tmp_path):
    root = make_two_tree_repo(tmp_path)
    codemap.generate(root)
    lines = codemap.catalog(root, budget_lines=100, focus=[]).splitlines()
    assert any(line.startswith("alpha/one.py") for line in lines)
    assert any(line.startswith("beta/one.py") for line in lines)


# ---------------------------------------------------------------------------
# --slice: tier-2 detail for the named subtree only


def test_slice_emits_tier2_for_named_paths_only(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    out = codemap.slice_modules(root, ["fx/"])
    assert set(out["modules"]) == {"fx/__init__.py", "fx/core.py", "fx/helper.py"}
    assert out["modules"]["fx/core.py"]["symbols"] == EXPECTED_CORE["symbols"]


# ---------------------------------------------------------------------------
# Monorepo: one map.json shard per package dir (dir owning a pyproject.toml)


def test_monorepo_shards_per_package_dir(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "ws"\n')
    (tmp_path / "rootmod.py").write_text("def top() -> None:\n    pass\n")
    p1 = tmp_path / "packages" / "p1"
    p1.mkdir(parents=True)
    (p1 / "pyproject.toml").write_text('[project]\nname = "p1"\n')
    (p1 / "src.py").write_text("def inner() -> None:\n    pass\n")
    report = codemap.generate(tmp_path)
    assert sorted(report["shards"]) == ["map.json", "packages/p1/map.json"]
    assert "rootmod.py" in read_map(tmp_path)["modules"]
    p1_doc = json.loads((p1 / "map.json").read_text())
    assert "packages/p1/src.py" in p1_doc["modules"]
