"""Seams for the map rail (issue #27, dec-462f4b28): committed map.json
projected from codegraph's SQLite (primary) or Python ast (fallback).

The seams, one per acceptance criterion plus the fix-round-1 hardening:
- byte-determinism: two runs on an unchanged repo → identical bytes
- ``map --check`` is PURE and its failure names the drifted module — it never
  writes, so it cannot self-clear on a second run
- sidecar (map.notes.json) staleness names its module; the interface hash is
  module-identifying (two empty modules never share a hash)
- the codegraph version pin fails loudly on a bump; the fixture db doubles as
  the schema contract (projection queries run against a 1.6.0-shaped db);
  a stale or corrupt index fails loudly instead of mapping old code
- ast fallback produces the same map.json shape — asserted against a FIXED
  hand-written expected entry shared with the codegraph fixture, which
  deliberately exercises the divergence-prone syntaxes: a string literal
  (quote normalization), a ``__future__`` import, a truncated long value
- publics under module-level if/try/with and tuple-unpacked constants are
  visible to the ast producer
- unparseable files get a deterministic marker, never a traceback
- the map is git-anchored: root resolves to the repo toplevel, untracked
  files stay out
- catalog respects --budget-lines by expanding only the focus subtree
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import textwrap

import pytest

from devloop import codemap
from devloop.cli import main

# ---------------------------------------------------------------------------
# Fixture repo (ast producer) and fixture codegraph db (codegraph producer).
# Both describe the SAME tiny source tree, so the shape-parity expectations
# below are one hand-written dict, not a cross-projection comparison. The
# tree deliberately includes the syntaxes the producers can diverge on:
# a double-quoted string constant, a __future__ import, a value long enough
# to exceed the signature cap.

CORE_SRC = textwrap.dedent(
    """\
    from __future__ import annotations

    import json
    from fx import helper

    GREETING = "hi"
    LIMIT = 5

    def run(x: int) -> str:
        import os as _os  # function-local: not part of the module's import surface
        return helper.fmt(x)

    def _private() -> None:
        pass
    """
)

# BAND is board.RUNG_ROLES's shape: RAW source over codegraph's 102-char
# truncation point (a two-line tuple: 106 chars with the '= ' prefix) while
# the NORMALIZED text is under it (98 chars) — the raw-vs-normalized band
# review round 3 measured live. Both producers must omit its signature.
BAND_VALUE = (
    '("bbbbbbbbbbbbbbbbbbbb", "cccccccccccccccccccc",\n'
    '        "dddddddddddddddddddd", "eeeeeeeeeeeeeeeeeeee")'
)
HELPER_SRC = (
    f"BAND = {BAND_VALUE}\n"
    'TRUNC = ("aaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbb", '
    '"cccccccccccccccccccc", "dddddddddddddddddddd", "eeeeeeeeeeeeeeeeeeee", '
    '"ffffffffffffffffffff")\n'
    "\n\n"
    "def fmt(x: int) -> str:\n"
    "    return str(x)\n"
)

# Hand-written expected entries (the independent source of truth). Note:
# GREETING normalizes to single quotes on BOTH producers; TRUNC's value is
# over the cap so its signature is omitted on BOTH; __future__ never lands
# in imports_external.
EXPECTED_CORE = {
    "responsibility": None,
    "symbols": [
        {"kind": "variable", "name": "GREETING", "signature": "= 'hi'"},
        {"kind": "variable", "name": "LIMIT", "signature": "= 5"},
        {"kind": "function", "name": "run", "signature": "(x: int) -> str"},
    ],
    "imports_internal": ["fx/helper.py"],
    "imports_external": ["json"],
}
EXPECTED_HELPER = {
    "responsibility": None,
    "symbols": [
        {"kind": "variable", "name": "BAND"},
        {"kind": "variable", "name": "TRUNC"},
        {"kind": "function", "name": "fmt", "signature": "(x: int) -> str"},
    ],
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
    (pkg / "helper.py").write_text(HELPER_SRC)
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


def make_codegraph_db(path, version: str, root):
    """1.6.0-shaped index of make_repo's tree, content hashes real (sha256 of
    the on-disk bytes, codegraph's own convention) so freshness passes."""
    db = sqlite3.connect(path)
    db.executescript(CODEGRAPH_SCHEMA)
    db.execute("INSERT INTO project_metadata VALUES ('indexed_with_version', ?, 0)", (version,))
    for f in ("fx/__init__.py", "fx/core.py", "fx/helper.py", "tests/test_core.py"):
        h = hashlib.sha256((root / f).read_bytes()).hexdigest()
        db.execute("INSERT INTO files VALUES (?, ?, 'python', 1, 0, 0, 0, NULL, 0)", (f, h))

    def node(id, kind, name, qual, fpath, sig, line=1):
        db.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, "
            "start_line, end_line, start_column, end_column, signature, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'python', ?, ?, 0, 0, ?, 0)",
            (id, kind, name, qual, fpath, line, line, sig))

    # codegraph stores raw source text: double quotes stay double here.
    node("n1", "variable", "GREETING", "GREETING", "fx/core.py", '= "hi"', 6)
    node("n2", "variable", "LIMIT", "LIMIT", "fx/core.py", "= 5", 7)
    # duplicate module-level binding: first (by line) wins, deterministically
    node("n2b", "variable", "LIMIT", "LIMIT", "fx/core.py", "= 6", 99)
    node("n3", "function", "run", "run", "fx/core.py", "(x: int) -> str", 9)
    node("n4", "function", "_private", "_private", "fx/core.py", "() -> None", 12)
    # nested symbol: '::'-qualified, must be excluded from the public surface
    node("n5", "function", "inner", "run::inner", "fx/core.py", "()", 10)
    node("n6", "import", "json", "json", "fx/core.py", None, 3)
    node("n7", "import", "fx.helper", "fx.helper", "fx/core.py", None, 4)
    # NO __future__ import node: real codegraph never records one.
    # codegraph stores RAW source and truncates it at 102 chars + ellipsis
    # (the measured 1.6.0 behavior) — BAND's stored text is its raw two-line
    # source cut at 102, unparseable; TRUNC is far over every cap.
    node("n8", "variable", "TRUNC", "TRUNC", "fx/helper.py",
         '= ("aaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbb", "...', 3)
    node("n8b", "variable", "BAND", "BAND", "fx/helper.py",
         ("= " + BAND_VALUE)[:102] + "...", 1)
    node("n9", "function", "fmt", "fmt", "fx/helper.py", "(x: int) -> str", 5)
    node("n10", "function", "test_a", "test_a", "tests/test_core.py", "()", 1)
    node("n11", "function", "test_b", "test_b", "tests/test_core.py", "()", 5)
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
    db = make_codegraph_db(tmp_path / "cg.db", codemap.CODEGRAPH_VERSION, root)
    report = codemap.generate(root, producer="codegraph", db=db)
    assert report["producer"] == "codegraph"
    doc = read_map(root)
    assert doc["generator"] == {"producer": "codegraph", "codegraph": codemap.CODEGRAPH_VERSION}
    assert doc["modules"]["fx/core.py"] == EXPECTED_CORE
    assert doc["modules"]["fx/helper.py"] == EXPECTED_HELPER
    assert doc["modules"]["tests/test_core.py"] == EXPECTED_TESTS


# ---------------------------------------------------------------------------
# AC: contract test pins the codegraph version; a bump fails loudly.
# A stale or corrupt index fails just as loudly, before any projection.


def test_codegraph_version_bump_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(tmp_path / "cg.db", "9.9.9", root)
    with pytest.raises(codemap.MapError, match=r"9\.9\.9.*1\.6\.0|1\.6\.0.*9\.9\.9"):
        codemap.generate(root, producer="codegraph", db=db)


def test_codegraph_producer_without_index_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(codemap.MapError, match="codegraph"):
        codemap.generate(root, producer="codegraph")


def test_stale_codegraph_index_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(tmp_path / "cg.db", codemap.CODEGRAPH_VERSION, root)
    helper = root / "fx" / "helper.py"
    helper.write_text(helper.read_text() + "\n\ndef extra() -> int:\n    return 1\n")
    with pytest.raises(codemap.MapError, match=r"behind the worktree.*fx/helper\.py"):
        codemap.generate(root, producer="codegraph", db=db)


def test_file_missing_from_index_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(tmp_path / "cg.db", codemap.CODEGRAPH_VERSION, root)
    (root / "fx" / "extra.py").write_text("def novel() -> None:\n    pass\n")
    with pytest.raises(codemap.MapError, match=r"behind the worktree.*fx/extra\.py"):
        codemap.generate(root, producer="codegraph", db=db)


def test_corrupt_codegraph_index_raises_maperror(tmp_path):
    root = make_repo(tmp_path)
    bad = tmp_path / "cg.db"
    bad.write_bytes(b"this is not a sqlite database.......")
    with pytest.raises(codemap.MapError):
        codemap.generate(root, producer="codegraph", db=bad)


# ---------------------------------------------------------------------------
# AC: --check fails naming the drifted module — and is PURE: it never writes,
# so a second run cannot self-clear the gate.


def test_check_green_on_fresh_map(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    report = codemap.check(root)
    assert report["ok"] is True
    assert report["drifted"] == []


def test_check_is_pure_and_names_the_drifted_module(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    committed = (root / "map.json").read_bytes()
    helper = root / "fx" / "helper.py"
    helper.write_text(helper.read_text() + "\n\ndef extra() -> int:\n    return 1\n")
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["drifted"] == ["fx/helper.py"]
    assert (root / "map.json").read_bytes() == committed
    again = codemap.check(root)
    assert again["ok"] is False
    assert again["drifted"] == ["fx/helper.py"]


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


def test_check_refuses_read_view_combination(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert main(["map", "--check", "--slice", "fx/", "--root", str(root)]) == 2
    assert main(["map", "--check", "--catalog", "--root", str(root)]) == 2


# ---------------------------------------------------------------------------
# AC: sidecar staleness names the module; the hash is module-identifying


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


def test_interface_hash_is_module_identifying(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "e"\n')
    (tmp_path / "e1.py").write_text("")
    (tmp_path / "e2.py").write_text("")
    report = codemap.generate(tmp_path)
    hashes = {u["module"]: u["hash"] for u in report["unannotated"]}
    assert hashes["e1.py"] != hashes["e2.py"]


# ---------------------------------------------------------------------------
# ast producer coverage: guarded defs, tuple constants, duplicate bindings,
# unparseable files


GUARDED_SRC = textwrap.dedent(
    """\
    import sys

    if sys.version_info >= (3, 11):
        def compat() -> int:
            return 1
    else:
        def compat() -> int:
            return 2

    try:
        import tomllib
    except ImportError:
        def loads(s): ...

    A, B = 1, 2
    """
)


def test_guarded_defs_and_tuple_constants_are_visible(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "g"\n')
    (tmp_path / "guarded.py").write_text(GUARDED_SRC)
    codemap.generate(tmp_path)
    syms = read_map(tmp_path)["modules"]["guarded.py"]["symbols"]
    assert [(s["kind"], s["name"]) for s in syms] == [
        ("variable", "A"), ("variable", "B"),
        ("function", "compat"), ("function", "loads"),
    ]
    by_name = {s["name"]: s for s in syms}
    assert by_name["A"]["signature"] == "= 1"
    assert by_name["B"]["signature"] == "= 2"
    assert by_name["compat"]["signature"] == "() -> int"


def test_duplicate_toplevel_assignment_yields_one_entry(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "d"\n')
    (tmp_path / "d.py").write_text("X = 1\nX = 2\n")
    codemap.generate(tmp_path)
    syms = read_map(tmp_path)["modules"]["d.py"]["symbols"]
    assert syms == [{"kind": "variable", "name": "X", "signature": "= 1"}]


def test_unparseable_files_are_marked_not_fatal(tmp_path):
    root = make_repo(tmp_path)
    (root / "bad.py").write_text("def broken(:\n")
    (root / "legacy.py").write_bytes(b"# caf\xe9\nX = 1\n")
    codemap.generate(root)
    doc = read_map(root)
    assert doc["modules"]["bad.py"]["unparsed"] is True
    assert doc["modules"]["legacy.py"]["unparsed"] is True
    assert codemap.check(root)["ok"] is True


# ---------------------------------------------------------------------------
# Shard identity: map.json is a common filename (tilemaps, style files) —
# a foreign one is never pruned, never overwritten, never read as ours

FOREIGN_MAP = json.dumps({"version": 8, "tiles": [[1, 2], [3, 4]]})


def test_foreign_map_json_is_never_pruned_or_read(tmp_path):
    root = make_repo(tmp_path)
    assets = root / "assets"
    assets.mkdir()
    (assets / "map.json").write_text(FOREIGN_MAP)
    report = codemap.generate(root)
    assert report["removed_shards"] == []
    assert (assets / "map.json").read_text() == FOREIGN_MAP
    check = codemap.check(root)
    assert check["ok"] is True
    assert check["removed_shards"] == []
    # and a foreign file never contributes modules to the read views
    assert "tiles" not in codemap.slice_modules(root, [""])["modules"]


def test_generate_refuses_to_overwrite_foreign_map_json(tmp_path):
    root = make_repo(tmp_path)
    (root / "map.json").write_text(FOREIGN_MAP)  # squats the root shard's path
    with pytest.raises(codemap.MapError, match="not a devloop shard"):
        codemap.generate(root)
    assert (root / "map.json").read_text() == FOREIGN_MAP


def test_json_array_tilemap_is_foreign_too(tmp_path):
    # valid JSON that isn't an object — still someone else's data, never ours
    root = make_repo(tmp_path)
    (root / "map.json").write_text("[[1, 2], [3, 4]]")
    with pytest.raises(codemap.MapError, match="not a devloop shard"):
        codemap.generate(root)
    assert (root / "map.json").read_text() == "[[1, 2], [3, 4]]"


# ---------------------------------------------------------------------------
# A DAMAGED shard of ours (merge conflict, half-write) is healed by generate,
# never mistaken for a foreign file — the repair command must repair

CONFLICTED = (
    "<<<<<<< HEAD\n"
    '{\n "version": 1,\n'
    "=======\n"
    '{\n "version": 1, "generator": {"producer": "ast"},\n'
    ">>>>>>> other\n"
)


def test_conflicted_shard_is_healed_not_refused(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    (root / "map.json").write_text(CONFLICTED)
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["damaged"] == ["map.json"]
    assert (root / "map.json").read_text() == CONFLICTED  # check stays pure
    heal = codemap.generate(root)
    assert heal["healed"] == ["map.json"]
    assert read_map(root)["modules"]["fx/core.py"] == EXPECTED_CORE
    assert codemap.check(root)["ok"] is True


def test_half_written_shard_is_healed(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    (root / "map.json").write_text('{"version": 1, "generator": {"produ')
    report = codemap.generate(root)
    assert report["healed"] == ["map.json"]
    assert codemap.check(root)["ok"] is True


def test_future_version_shard_is_still_ours(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    doc = read_map(root)
    doc["version"] = 2
    (root / "map.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    report = codemap.generate(root)  # no refusal: a schema bump can heal its past
    assert report["healed"] == []
    assert read_map(root)["version"] == 1


def make_two_shard_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "ws"\n')
    (tmp_path / "rootmod.py").write_text("def top() -> None:\n    pass\n")
    p1 = tmp_path / "packages" / "p1"
    p1.mkdir(parents=True)
    (p1 / "pyproject.toml").write_text('[project]\nname = "p1"\n')
    (p1 / "src.py").write_text("def inner() -> None:\n    pass\n")
    return tmp_path


def test_read_views_skip_damaged_shard_with_a_warning(tmp_path):
    # the read views are how the map reaches a dispatch — a damaged file
    # must never kill them; it degrades to a warning naming the remedy
    root = make_two_shard_repo(tmp_path)
    codemap.generate(root)
    (root / "packages" / "p1" / "map.json").write_text(CONFLICTED)
    out = codemap.catalog(root, budget_lines=40, focus=[])
    assert any(line.startswith("rootmod.py") for line in out.splitlines())
    assert "packages/p1/map.json" in out and "damaged" in out and "devloop map" in out
    sl = codemap.slice_modules(root, ["rootmod.py"])
    assert "rootmod.py" in sl["modules"]
    assert sl["skipped_damaged"] == ["packages/p1/map.json"]


def test_only_damaged_maps_is_an_error_naming_the_damage(tmp_path):
    # zero healthy shards: nothing to show, so the error names the damage
    root = make_repo(tmp_path)
    codemap.generate(root)
    (root / "map.json").write_text(CONFLICTED)
    with pytest.raises(codemap.MapError, match=r"map\.json.*damaged"):
        codemap.catalog(root, budget_lines=40, focus=[])


# ---------------------------------------------------------------------------
# The three entry points agree about a damaged file OUTSIDE the rendered set
# (the round-4 repro): with HEAD proof it is ours — check flags it, generate
# prunes it; without proof it is treated like foreign — untouched, non-fatal


def test_damaged_orphan_with_head_proof_is_flagged_and_pruned(tmp_path):
    root = _git_repo(make_two_shard_repo(tmp_path))
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "healthy"], cwd=root, check=True)
    p1_map = root / "packages" / "p1" / "map.json"
    (root / "packages" / "p1" / "src.py").unlink()
    p1_map.write_text(CONFLICTED)  # HEAD still holds the healthy shard
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["damaged"] == ["packages/p1/map.json"]
    heal = codemap.generate(root)
    assert "packages/p1/map.json" in heal["removed_shards"]
    assert not p1_map.exists()
    assert codemap.check(root)["ok"] is True


def test_damaged_orphan_without_head_proof_is_left_like_foreign(tmp_path):
    # e.g. a JSONC tilemap, or a conflict committed before we ever saw it:
    # no evidence it was ours — never delete, never fail the gate, and the
    # read views still work (skip + warning)
    root = make_two_shard_repo(tmp_path)
    codemap.generate(root)
    jsonc = root / "assets"
    jsonc.mkdir()
    (jsonc / "map.json").write_text("// tile config\n{\"tiles\": [1]}\n")
    assert codemap.check(root)["ok"] is True
    report = codemap.generate(root)
    assert report["removed_shards"] == []
    assert (jsonc / "map.json").exists()
    assert "rootmod.py" in codemap.slice_modules(root, ["rootmod.py"])["modules"]


# ---------------------------------------------------------------------------
# Healing never flips the committed producer: the pin survives in HEAD's copy


def test_heal_preserves_the_committed_producer(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    codemap.generate(root)  # producer ast (no index)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "ast map"], cwd=root, check=True)
    # a codegraph index appears at the default location...
    cg = root / ".codegraph"
    cg.mkdir()
    (cg / "codegraph.db").write_bytes(b"garbage that must never be opened")
    # ...and the shard gets conflicted: the pin now lives only in HEAD
    (root / "map.json").write_text(CONFLICTED)
    heal = codemap.generate(root)
    assert heal["producer"] == "ast"
    assert heal["healed"] == ["map.json"]
    assert read_map(root)["generator"] == {"producer": "ast"}


# ---------------------------------------------------------------------------
# The size guard never fires on our own shard


def test_oversized_own_shard_is_still_ours(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    codemap.generate(root)
    monkeypatch.setattr(codemap, "_SHARD_MAX_BYTES",
                        (root / "map.json").stat().st_size - 1)
    assert codemap.check(root)["ok"] is True
    codemap.generate(root)  # no refusal about its own output


# ---------------------------------------------------------------------------
# A tracked file deleted from the working tree: dropped by the ast producer,
# named as stale by the codegraph producer — never a raw traceback


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def test_deleted_tracked_file_is_stale_not_a_traceback(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    db = make_codegraph_db(tmp_path / "cg.db", codemap.CODEGRAPH_VERSION, root)
    (root / "fx" / "helper.py").unlink()  # rm without git rm: still in the index
    with pytest.raises(codemap.MapError, match=r"behind the worktree.*fx/helper\.py"):
        codemap.generate(root, producer="codegraph", db=db)


def test_deleted_tracked_file_drops_from_ast_map(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    codemap.generate(root)
    (root / "fx" / "helper.py").unlink()
    report = codemap.check(root)
    assert report["ok"] is False
    assert "fx/helper.py" in report["drifted"]
    codemap.generate(root)
    assert "fx/helper.py" not in read_map(root)["modules"]


# ---------------------------------------------------------------------------
# check surfaces orphan notes (advisory: an orphan can't mislead the map,
# unlike a stale note, so it reports without failing the gate)


def test_check_reports_orphan_notes_without_failing(tmp_path):
    root = make_repo(tmp_path)
    codemap.generate(root)
    (root / "map.notes.json").write_text(json.dumps(
        {"gone.py": {"hash": "x", "note": "orphan"}}))
    report = codemap.check(root)
    assert report["ok"] is True
    assert report["orphan_notes"] == ["gone.py"]


# ---------------------------------------------------------------------------
# Git anchoring: root resolves to the repo toplevel; only tracked files map


def test_git_root_and_tracked_files_anchor_the_map(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    (root / "scratch_local.py").write_text("def junk() -> None:\n    pass\n")
    report = codemap.generate(root / "fx")  # subdir invocation
    assert report["shards"] == ["map.json"]
    doc = read_map(root)
    assert "fx/core.py" in doc["modules"]
    assert "scratch_local.py" not in doc["modules"]
    assert not (root / "fx" / "map.json").exists()


# ---------------------------------------------------------------------------
# generate prunes what no longer exists


def test_generate_prunes_orphaned_shards_and_reports_orphan_notes(tmp_path):
    make_two_shard_repo(tmp_path)
    p1 = tmp_path / "packages" / "p1"
    codemap.generate(tmp_path)
    assert (p1 / "map.json").exists()
    (p1 / "src.py").unlink()
    (tmp_path / "map.notes.json").write_text(json.dumps(
        {"gone.py": {"hash": "x", "note": "orphan"}}))
    report = codemap.generate(tmp_path)
    assert report["removed_shards"] == ["packages/p1/map.json"]
    assert not (p1 / "map.json").exists()
    assert report["orphan_notes"] == ["gone.py"]


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
    make_two_shard_repo(tmp_path)
    p1 = tmp_path / "packages" / "p1"
    report = codemap.generate(tmp_path)
    assert sorted(report["shards"]) == ["map.json", "packages/p1/map.json"]
    assert "rootmod.py" in read_map(tmp_path)["modules"]
    p1_doc = json.loads((p1 / "map.json").read_text())
    assert "packages/p1/src.py" in p1_doc["modules"]
