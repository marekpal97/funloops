"""Seams for the map rail (issue #27, dec-462f4b28): committed map.json
projected from codegraph's SQLite index — the ONE producer (the ast
fallback was dec-462f4b28's falsifier branch; it never fired and was
retired in fix round 5).

The seams:
- byte-determinism: two runs on an unchanged repo → identical bytes
- signatures are codegraph's raw text VERBATIM — including its own
  102-char truncation ellipsis; nothing is re-parsed or "repaired"
- ``map --check`` is PURE and its failure names the drifted module — it
  never writes, so it cannot self-clear on a second run
- the gate self-provisions: a missing default-path index runs
  ``codegraph init -y``, a stale/corrupt/mispinned one runs ``codegraph
  index`` — binary resolved --codegraph-bin → $CODEGRAPH_BIN → PATH
- degradation splits on the artifact: committed map + no codegraph is a
  LOUD MapError; no committed map means --check no-ops honestly and the
  read views report "not adopted" instead of erroring
- one pruning rule (_mappable) for file listing, index projection and
  shard scanning — a tracked file under SKIP_DIRS/dot-dirs never maps
- sidecar (map.notes.json) staleness names its module; the interface
  hash is module-identifying (two empty modules never share a hash)
- the codegraph version pin fails loudly (after one reindex attempt);
  the fixture db doubles as the schema contract for 1.6.0
- the map is git-anchored: root resolves to the repo toplevel, untracked
  files stay out — and a git failure where a repo plainly exists is a
  MapError from generate/check, never a silent fallback keyspace
- catalog respects --budget-lines by expanding only the focus subtree;
  focus/slice paths are normalized and unmatched entries are reported
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
# Fixture repo + fixture codegraph db describing the SAME tiny source tree.
# The tree keeps the syntaxes that once justified a parity layer: a
# double-quoted string constant (stored raw, served raw), a __future__
# import (codegraph records no node for it), a value long enough that
# codegraph itself truncates the stored signature (served verbatim).

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

TRUNC_VALUE = (
    '("aaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbb", '
    '"cccccccccccccccccccc", "dddddddddddddddddddd", "eeeeeeeeeeeeeeeeeeee", '
    '"ffffffffffffffffffff")'
)
HELPER_SRC = (
    f"TRUNC = {TRUNC_VALUE}\n"
    "\n\n"
    "def fmt(x: int) -> str:\n"
    "    return str(x)\n"
)

# codegraph 1.6.0 truncates the stored raw text at exactly 102 chars + "..."
# (every truncated signature in a real index is 105 chars) — the map serves
# that text verbatim, never re-parsed (re-parsing once fabricated a signature
# when the cut landed after a binary operator).
TRUNC_STORED = ("= " + TRUNC_VALUE)[:102] + "..."
assert len(TRUNC_STORED) == 105

# Hand-written expected entries (the independent source of truth): raw
# double quotes survive, __future__ never lands in imports_external.
EXPECTED_CORE = {
    "responsibility": None,
    "symbols": [
        {"kind": "variable", "name": "GREETING", "signature": '= "hi"'},
        {"kind": "variable", "name": "LIMIT", "signature": "= 5"},
        {"kind": "function", "name": "run", "signature": "(x: int) -> str"},
    ],
    "imports_internal": ["fx/helper.py"],
    "imports_external": ["json"],
}
EXPECTED_HELPER = {
    "responsibility": None,
    "symbols": [
        {"kind": "variable", "name": "TRUNC", "signature": TRUNC_STORED},
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

REPO_FILES = ("fx/__init__.py", "fx/core.py", "fx/helper.py", "tests/test_core.py")

# (id, kind, name, qualified_name, file_path, signature, line) — codegraph
# stores raw source text: double quotes stay double, truncation included.
REPO_NODES = (
    ("n1", "variable", "GREETING", "GREETING", "fx/core.py", '= "hi"', 6),
    ("n2", "variable", "LIMIT", "LIMIT", "fx/core.py", "= 5", 7),
    # duplicate module-level binding: first (by line) wins, deterministically
    ("n2b", "variable", "LIMIT", "LIMIT", "fx/core.py", "= 6", 99),
    ("n3", "function", "run", "run", "fx/core.py", "(x: int) -> str", 9),
    ("n4", "function", "_private", "_private", "fx/core.py", "() -> None", 12),
    # nested symbol: '::'-qualified, must be excluded from the public surface
    ("n5", "function", "inner", "run::inner", "fx/core.py", "()", 10),
    ("n6", "import", "json", "json", "fx/core.py", None, 3),
    ("n7", "import", "fx.helper", "fx.helper", "fx/core.py", None, 4),
    # NO __future__ import node: real codegraph never records one.
    ("n8", "variable", "TRUNC", "TRUNC", "fx/helper.py", TRUNC_STORED, 1),
    ("n9", "function", "fmt", "fmt", "fx/helper.py", "(x: int) -> str", 5),
    ("n10", "function", "test_a", "test_a", "tests/test_core.py", "()", 1),
    ("n11", "function", "test_b", "test_b", "tests/test_core.py", "()", 5),
)
REPO_EDGES = (("file:fx/core.py", "file:fx/helper.py"),)


def make_db(root, *, files, nodes=(), edges=(), version=None, db_path=None):
    """A codegraph-1.6.0-shaped index of the given tree: content hashes real
    (sha256 of the on-disk bytes, codegraph's own convention) so freshness
    passes. Default location is the default the code resolves: the root's
    .codegraph/codegraph.db. Overwrites any previous fixture db."""
    if db_path is None:
        (root / ".codegraph").mkdir(exist_ok=True)
        db_path = root / ".codegraph" / "codegraph.db"
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.executescript(CODEGRAPH_SCHEMA)
    db.execute("INSERT INTO project_metadata VALUES ('indexed_with_version', ?, 0)",
               (version or codemap.CODEGRAPH_VERSION,))
    for f in files:
        h = hashlib.sha256((root / f).read_bytes()).hexdigest()
        db.execute("INSERT INTO files VALUES (?, ?, 'python', 1, 0, 0, 0, NULL, 0)", (f, h))
    for id, kind, name, qual, fpath, sig, line in nodes:
        db.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, "
            "start_line, end_line, start_column, end_column, signature, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'python', ?, ?, 0, 0, ?, 0)",
            (id, kind, name, qual, fpath, line, line, sig))
    for src, tgt in edges:
        db.execute("INSERT INTO edges (source, target, kind) VALUES (?, ?, 'imports')",
                   (src, tgt))
    db.commit()
    db.close()
    return db_path


def make_codegraph_db(root, *, version=None, db_path=None, extra_files=(),
                      extra_nodes=()):
    return make_db(root, files=REPO_FILES + tuple(extra_files),
                   nodes=REPO_NODES + tuple(extra_nodes), edges=REPO_EDGES,
                   version=version, db_path=db_path)


# helper.py with an appended `extra` function — the drift fixture: the db is
# rebuilt to match the edited tree (a real reindex would do the same), so
# freshness passes and check compares fresh projection vs committed shard.
EXTRA_FN = "\n\ndef extra() -> int:\n    return 1\n"
EXTRA_NODE = ("nx", "function", "extra", "extra", "fx/helper.py", "() -> int", 8)


def drift_helper(root):
    helper = root / "fx" / "helper.py"
    helper.write_text(helper.read_text() + EXTRA_FN)
    make_codegraph_db(root, extra_nodes=(EXTRA_NODE,))


def read_map(root):
    return json.loads((root / "map.json").read_text(encoding="utf-8"))


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def _commit(root, msg="c"):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", msg], cwd=root, check=True)


# ---------------------------------------------------------------------------
# AC: byte-identical across two runs on an unchanged repo


def test_map_bytes_identical_across_two_runs(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    first = (root / "map.json").read_bytes()
    codemap.generate(root)
    assert (root / "map.json").read_bytes() == first


# ---------------------------------------------------------------------------
# AC: the projection matches the hand-written expected shape — raw verbatim
# signatures, truncation included


def test_codegraph_projection_matches_expected_shape(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    report = codemap.generate(root)
    assert report["producer"] == "codegraph"
    doc = read_map(root)
    assert doc["generator"] == {"producer": "codegraph",
                                "codegraph": codemap.CODEGRAPH_VERSION}
    assert doc["modules"]["fx/core.py"] == EXPECTED_CORE
    assert doc["modules"]["fx/helper.py"] == EXPECTED_HELPER
    assert doc["modules"]["tests/test_core.py"] == EXPECTED_TESTS


# ---------------------------------------------------------------------------
# An EXPLICIT --db is never self-provisioned: version pin, staleness and
# corruption fail loudly (the fixture db is the schema contract)


def test_codegraph_version_bump_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(root, version="9.9.9", db_path=tmp_path / "cg.db")
    with pytest.raises(codemap.MapError, match=r"9\.9\.9.*1\.6\.0|1\.6\.0.*9\.9\.9"):
        codemap.generate(root, db=db)


def test_explicit_missing_db_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(codemap.MapError, match="codegraph"):
        codemap.generate(root, db=tmp_path / "absent.db")


def test_stale_codegraph_index_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(root, db_path=tmp_path / "cg.db")
    (root / "fx" / "helper.py").write_text(HELPER_SRC + EXTRA_FN)
    with pytest.raises(codemap.MapError, match=r"behind the worktree.*fx/helper\.py"):
        codemap.generate(root, db=db)


def test_file_missing_from_index_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    db = make_codegraph_db(root, db_path=tmp_path / "cg.db")
    (root / "fx" / "extra.py").write_text("def novel() -> None:\n    pass\n")
    with pytest.raises(codemap.MapError, match=r"behind the worktree.*fx/extra\.py"):
        codemap.generate(root, db=db)


def test_corrupt_codegraph_index_raises_maperror(tmp_path):
    root = make_repo(tmp_path)
    bad = tmp_path / "cg.db"
    bad.write_bytes(b"this is not a sqlite database.......")
    with pytest.raises(codemap.MapError):
        codemap.generate(root, db=bad)


def test_deleted_tracked_file_is_stale_not_a_traceback(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    db = make_codegraph_db(root, db_path=tmp_path / "cg.db")
    (root / "fx" / "helper.py").unlink()  # rm without git rm: still in the index
    with pytest.raises(codemap.MapError, match=r"behind the worktree.*fx/helper\.py"):
        codemap.generate(root, db=db)


# ---------------------------------------------------------------------------
# Self-provisioning: the DEFAULT-path index is minted or refreshed by running
# codegraph itself — a missing index runs init, a stale one runs a reindex


def make_stub_codegraph(tmp_path, prebuilt):
    """A codegraph stand-in: logs the verb, 'indexes' by copying a prebuilt
    fixture db into <root>/.codegraph/ (the last argv entry is the root)."""
    stub = tmp_path / "codegraph-stub"
    log = tmp_path / "stub-calls.log"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{log}"\n'
        'for a in "$@"; do last="$a"; done\n'
        'mkdir -p "$last/.codegraph"\n'
        f'cp "{prebuilt}" "$last/.codegraph/codegraph.db"\n')
    stub.chmod(0o755)
    return stub, log


def test_generate_self_provisions_a_missing_index(tmp_path):
    root = make_repo(tmp_path)
    prebuilt = make_codegraph_db(root, db_path=tmp_path / "prebuilt.db")
    stub, log = make_stub_codegraph(tmp_path, prebuilt)
    report = codemap.generate(root, codegraph_bin=str(stub))
    assert log.read_text().split() == ["init"]
    assert (root / ".codegraph" / "codegraph.db").is_file()
    assert read_map(root)["modules"]["fx/core.py"] == EXPECTED_CORE
    assert report["producer"] == "codegraph"


def test_stale_default_index_triggers_a_reindex(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    # the tree moves on: the default-path db is now stale; the stub's
    # prebuilt db is what a real reindex of the NEW tree would produce
    (root / "fx" / "helper.py").write_text(HELPER_SRC + EXTRA_FN)
    prebuilt = make_db(root, files=REPO_FILES,
                       nodes=REPO_NODES + (EXTRA_NODE,), edges=REPO_EDGES,
                       db_path=tmp_path / "prebuilt.db")
    stub, log = make_stub_codegraph(tmp_path, prebuilt)
    report = codemap.check(root, codegraph_bin=str(stub))
    assert log.read_text().split() == ["index"]
    assert report["ok"] is False
    assert report["drifted"] == ["fx/helper.py"]


def test_env_var_selects_the_binary(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    prebuilt = make_codegraph_db(root, db_path=tmp_path / "prebuilt.db")
    stub, log = make_stub_codegraph(tmp_path, prebuilt)
    monkeypatch.setenv("CODEGRAPH_BIN", str(stub))
    codemap.generate(root)
    assert log.read_text().split() == ["init"]


def test_binary_resolution_order(monkeypatch):
    monkeypatch.setenv("CODEGRAPH_BIN", "from-env")
    assert codemap._codegraph_bin("explicit") == "explicit"
    assert codemap._codegraph_bin(None) == "from-env"
    monkeypatch.delenv("CODEGRAPH_BIN")
    assert codemap._codegraph_bin(None) == "codegraph"


# ---------------------------------------------------------------------------
# Degradation split — the dependency follows the artifact.
# Committed map + no codegraph: LOUD. No committed map: honest no-op.


def test_committed_map_without_codegraph_fails_loudly(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    (root / ".codegraph" / "codegraph.db").unlink()
    with pytest.raises(codemap.MapError, match="codegraph unavailable"):
        codemap.check(root, codegraph_bin=str(tmp_path / "no-such-binary"))
    with pytest.raises(codemap.MapError, match="codegraph unavailable"):
        codemap.generate(root, codegraph_bin=str(tmp_path / "no-such-binary"))


def test_check_no_ops_when_map_not_adopted(tmp_path, capsys):
    root = make_repo(tmp_path)  # no committed map.json anywhere
    report = codemap.check(root, codegraph_bin=str(tmp_path / "no-such-binary"))
    assert report["ok"] is True
    assert report["adopted"] is False
    assert main(["map", "--check", "--root", str(root),
                 "--codegraph-bin", str(tmp_path / "no-such-binary")]) == 0
    assert "not adopted" in json.loads(capsys.readouterr().out)["note"]


def test_read_views_report_not_adopted(tmp_path):
    root = make_repo(tmp_path)
    out = codemap.catalog(root, budget_lines=40, focus=[])
    assert "not adopted" in out
    sl = codemap.slice_modules(root, ["fx"])
    assert sl["modules"] == {}
    assert "not adopted" in sl["note"]


def test_cli_exit_2_when_codegraph_unavailable(tmp_path, capsys):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    (root / ".codegraph" / "codegraph.db").unlink()
    assert main(["map", "--check", "--root", str(root),
                 "--codegraph-bin", str(tmp_path / "no-such-binary")]) == 2
    assert "codegraph unavailable" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# AC: --check fails naming the drifted module — and is PURE: it never writes,
# so a second run cannot self-clear the gate.


def test_check_green_on_fresh_map(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    report = codemap.check(root)
    assert report["ok"] is True
    assert report["drifted"] == []


def test_check_is_pure_and_names_the_drifted_module(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    committed = (root / "map.json").read_bytes()
    drift_helper(root)
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["drifted"] == ["fx/helper.py"]
    assert (root / "map.json").read_bytes() == committed
    again = codemap.check(root)
    assert again["ok"] is False
    assert again["drifted"] == ["fx/helper.py"]


def test_check_exit_codes_via_cli(tmp_path, capsys):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    assert main(["map", "--root", str(root)]) == 0
    assert main(["map", "--check", "--root", str(root)]) == 0
    drift_helper(root)
    capsys.readouterr()
    assert main(["map", "--check", "--root", str(root)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert "fx/helper.py" in out["drifted"]


def test_check_refuses_read_view_combination(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert main(["map", "--check", "--slice", "fx/", "--root", str(root)]) == 2
    assert main(["map", "--check", "--catalog", "--root", str(root)]) == 2


# ---------------------------------------------------------------------------
# F4: a git failure where a repo plainly exists must not silently degrade the
# anchor mid-check — generate/check raise; the read views stay lenient


def test_git_failure_in_required_verb_is_a_maperror(tmp_path, monkeypatch):
    root = _git_repo(make_repo(tmp_path))
    make_codegraph_db(root)
    codemap.generate(root)
    monkeypatch.setattr(codemap, "_git", lambda *a: None)  # timeout/absence
    with pytest.raises(codemap.MapError, match="git"):
        codemap.check(root)
    with pytest.raises(codemap.MapError, match="git"):
        codemap.generate(root)
    # read views survive on the committed artifact
    assert "fx/core.py" in codemap.slice_modules(root, ["fx"])["modules"]


# ---------------------------------------------------------------------------
# AC: sidecar staleness names the module; the hash is module-identifying


def test_fresh_note_lands_as_responsibility(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    report = codemap.generate(root)
    current = {h["module"]: h["hash"] for h in report["unannotated"]}
    (root / "map.notes.json").write_text(json.dumps({
        "fx/helper.py": {"hash": current["fx/helper.py"], "note": "formats things"},
    }))
    codemap.generate(root)
    assert read_map(root)["modules"]["fx/helper.py"]["responsibility"] == "formats things"


def test_stale_note_names_its_module(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
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
    make_db(tmp_path, files=("e1.py", "e2.py"))
    report = codemap.generate(tmp_path)
    hashes = {u["module"]: u["hash"] for u in report["unannotated"]}
    assert hashes["e1.py"] != hashes["e2.py"]


# ---------------------------------------------------------------------------
# F1: ONE pruning rule — a tracked file under SKIP_DIRS/dot-dirs is not
# mapped by the file listing, the projection, or the shard scan; generate
# must be able to re-run over its own output


def test_tracked_files_under_skip_dirs_never_map(tmp_path):
    root = make_repo(tmp_path)
    build = root / "build"
    build.mkdir()
    (build / "pyproject.toml").write_text('[project]\nname = "b"\n')
    (build / "mod.py").write_text("def built() -> None:\n    pass\n")
    _git_repo(root)  # build/mod.py is TRACKED — and still never maps
    # the index HAS the file (codegraph saw it): the projection must prune it
    make_codegraph_db(root, extra_files=("build/mod.py",))
    report = codemap.generate(root)
    assert report["shards"] == ["map.json"]
    assert not (build / "map.json").exists()
    assert "build/mod.py" not in read_map(root)["modules"]
    codemap.generate(root)  # re-running over its own output: no refusal
    assert codemap.check(root)["ok"] is True


# ---------------------------------------------------------------------------
# F3: a valid-JSON file with a wrong-shaped 'modules' is never a healthy
# shard — with HEAD proof it is damaged (heal-able), and the read views
# never die with a raw TypeError


NULL_MODULES = json.dumps({"version": 1, "generator": {"producer": "codegraph"},
                           "modules": None})


def test_null_modules_shard_is_damaged_not_healthy(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    make_codegraph_db(root)
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "healthy")
    (root / "map.json").write_text(NULL_MODULES)
    report = codemap.check(root)  # no TypeError
    assert report["ok"] is False
    assert report["damaged"] == ["map.json"]
    heal = codemap.generate(root)
    assert heal["healed"] == ["map.json"]
    assert codemap.check(root)["ok"] is True


def test_read_views_never_typeerror_on_wrong_shape(tmp_path):
    root = make_two_shard_repo(tmp_path)
    make_two_shard_db(root)
    codemap.generate(root)
    (root / "packages" / "p1" / "map.json").write_text(NULL_MODULES)
    out = codemap.catalog(root, budget_lines=40, focus=[])  # no TypeError
    assert any(line.startswith("rootmod.py") for line in out.splitlines())
    assert "rootmod.py" in codemap.slice_modules(root, ["rootmod.py"])["modules"]


# ---------------------------------------------------------------------------
# Shard identity: map.json is a common filename (tilemaps, style files) —
# a foreign one is never pruned, never overwritten, never read as ours

FOREIGN_MAP = json.dumps({"version": 8, "tiles": [[1, 2], [3, 4]]})


def test_foreign_map_json_is_never_pruned_or_read(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
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
    make_codegraph_db(root)
    (root / "map.json").write_text(FOREIGN_MAP)  # squats the root shard's path
    with pytest.raises(codemap.MapError, match="not a devloop shard"):
        codemap.generate(root)
    assert (root / "map.json").read_text() == FOREIGN_MAP


def test_json_array_tilemap_is_foreign_too(tmp_path):
    # valid JSON that isn't an object — still someone else's data, never ours
    root = make_repo(tmp_path)
    make_codegraph_db(root)
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
    '{\n "version": 1, "generator": {"producer": "codegraph"},\n'
    ">>>>>>> other\n"
)


def test_conflicted_shard_is_healed_not_refused(tmp_path):
    # a merge conflict implies git: HEAD's healthy copy is the ownership
    # proof that keeps a fully-conflicted repo from reading as not-adopted
    root = _git_repo(make_repo(tmp_path))
    make_codegraph_db(root)
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "healthy")
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
    make_codegraph_db(root)
    codemap.generate(root)
    (root / "map.json").write_text('{"version": 1, "generator": {"produ')
    report = codemap.generate(root)
    assert report["healed"] == ["map.json"]
    assert codemap.check(root)["ok"] is True


def test_future_version_shard_is_still_ours(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
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


TWO_SHARD_FILES = ("rootmod.py", "packages/p1/src.py")
TWO_SHARD_NODES = (
    ("t1", "function", "top", "top", "rootmod.py", "() -> None", 1),
    ("t2", "function", "inner", "inner", "packages/p1/src.py", "() -> None", 1),
)


def make_two_shard_db(root, *, files=TWO_SHARD_FILES, nodes=TWO_SHARD_NODES):
    return make_db(root, files=files, nodes=nodes)


def test_read_views_skip_damaged_shard_with_a_warning(tmp_path):
    # the read views are how the map reaches a dispatch — a damaged file
    # must never kill them; it degrades to a warning naming the remedy
    root = make_two_shard_repo(tmp_path)
    make_two_shard_db(root)
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
    make_codegraph_db(root)
    codemap.generate(root)
    (root / "map.json").write_text(CONFLICTED)
    with pytest.raises(codemap.MapError, match=r"map\.json.*damaged"):
        codemap.catalog(root, budget_lines=40, focus=[])


# ---------------------------------------------------------------------------
# The three entry points agree about a damaged file OUTSIDE the rendered set:
# with HEAD proof it is ours — check flags it, generate prunes it; without
# proof it is treated like foreign — untouched, non-fatal


def test_damaged_orphan_with_head_proof_is_flagged_and_pruned(tmp_path):
    root = _git_repo(make_two_shard_repo(tmp_path))
    make_two_shard_db(root)
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "healthy")
    p1_map = root / "packages" / "p1" / "map.json"
    subprocess.run(["git", "rm", "-q", "packages/p1/src.py"], cwd=root, check=True)
    # the module is gone: the reindexed truth no longer contains it
    make_two_shard_db(root, files=("rootmod.py",), nodes=TWO_SHARD_NODES[:1])
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
    make_two_shard_db(root)
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
# The size guard never fires on our own shard


def test_oversized_own_shard_is_still_ours(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    monkeypatch.setattr(codemap, "_SHARD_MAX_BYTES",
                        (root / "map.json").stat().st_size - 1)
    assert codemap.check(root)["ok"] is True
    codemap.generate(root)  # no refusal about its own output


# ---------------------------------------------------------------------------
# check surfaces orphan notes (advisory: an orphan can't mislead the map,
# unlike a stale note, so it reports without failing the gate)


def test_check_reports_orphan_notes_without_failing(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
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
    make_codegraph_db(root)
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
    root = make_two_shard_repo(tmp_path)
    make_two_shard_db(root)
    p1 = root / "packages" / "p1"
    codemap.generate(root)
    assert (p1 / "map.json").exists()
    (p1 / "src.py").unlink()
    make_two_shard_db(root, files=("rootmod.py",), nodes=TWO_SHARD_NODES[:1])
    (root / "map.notes.json").write_text(json.dumps(
        {"gone.py": {"hash": "x", "note": "orphan"}}))
    report = codemap.generate(root)
    assert report["removed_shards"] == ["packages/p1/map.json"]
    assert not (p1 / "map.json").exists()
    assert report["orphan_notes"] == ["gone.py"]


# ---------------------------------------------------------------------------
# F10: dotted-name resolution — a suffix index with the documented
# lexicographically-first tie-break (hand-expected values)


def test_resolve_uses_suffix_index_with_first_wins_tie_break():
    idx = codemap._suffix_index(["a/util.py", "b/util.py", "pkg/__init__.py"])
    assert codemap._resolve("util", idx) == "a/util.py"
    assert codemap._resolve("b.util", idx) == "b/util.py"
    assert codemap._resolve("pkg", idx) == "pkg/__init__.py"
    assert codemap._resolve("missing", idx) is None
    assert codemap._resolve("til", idx) is None  # suffix is /-boundary, not substring


# ---------------------------------------------------------------------------
# AC: catalog respects --budget-lines, expanding only the focus subtree


def make_two_tree_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "two"\n')
    files, nodes = [], []
    for d in ("alpha", "beta"):
        (tmp_path / d).mkdir()
        for m in ("one", "two", "three"):
            (tmp_path / d / f"{m}.py").write_text(f"def {m}_{d}() -> None:\n    pass\n")
            files.append(f"{d}/{m}.py")
            nodes.append((f"{d}{m}", "function", f"{m}_{d}", f"{m}_{d}",
                          f"{d}/{m}.py", "() -> None", 1))
    make_db(tmp_path, files=tuple(files), nodes=tuple(nodes))
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
# F9: focus/slice paths go through ONE normalization + ONE equal-or-under
# predicate; unmatched entries are visible, never silently empty


def test_slice_normalizes_prefixes(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    plain = codemap.slice_modules(root, ["fx"])["modules"]
    assert set(plain) == {"fx/__init__.py", "fx/core.py", "fx/helper.py"}
    assert codemap.slice_modules(root, ["./fx/"])["modules"] == plain


def test_slice_reports_unmatched_prefixes(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    out = codemap.slice_modules(root, ["fx", "no/such/dir"])
    assert set(out["modules"]) == {"fx/__init__.py", "fx/core.py", "fx/helper.py"}
    assert out["unmatched"] == ["no/such/dir"]


def test_catalog_focus_is_normalized_and_unmatched_focus_is_reported(tmp_path):
    root = make_two_tree_repo(tmp_path)
    codemap.generate(root)
    out = codemap.catalog(root, budget_lines=5, focus=["./alpha/one.py"])
    assert any(line.startswith("alpha/one.py") for line in out.splitlines())
    out = codemap.catalog(root, budget_lines=5, focus=["no/such/dir"])
    assert "no/such/dir" in out and "matched nothing" in out


# ---------------------------------------------------------------------------
# --slice: tier-2 detail for the named subtree only


def test_slice_emits_tier2_for_named_paths_only(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    out = codemap.slice_modules(root, ["fx/"])
    assert set(out["modules"]) == {"fx/__init__.py", "fx/core.py", "fx/helper.py"}
    assert out["modules"]["fx/core.py"]["symbols"] == EXPECTED_CORE["symbols"]


# ---------------------------------------------------------------------------
# Monorepo: one map.json shard per package dir (dir owning a pyproject.toml)


def test_monorepo_shards_per_package_dir(tmp_path):
    root = make_two_shard_repo(tmp_path)
    make_two_shard_db(root)
    p1 = root / "packages" / "p1"
    report = codemap.generate(root)
    assert sorted(report["shards"]) == ["map.json", "packages/p1/map.json"]
    assert "rootmod.py" in read_map(root)["modules"]
    p1_doc = json.loads((p1 / "map.json").read_text())
    assert "packages/p1/src.py" in p1_doc["modules"]


# ---------------------------------------------------------------------------
# Round-6 F1: adoption consults HEAD — deleting or foreign-overwriting the
# committed shard must never read as "not adopted" (silently green)


def test_git_rm_of_committed_shard_is_red_not_unadopted(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    make_codegraph_db(root)
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "adopted")
    subprocess.run(["git", "rm", "-q", "map.json"], cwd=root, check=True)
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["missing_shards"] == ["map.json"]
    assert report["drifted"] == []  # no fabricated per-module drift
    codemap.generate(root)  # the remedy restores the shard
    assert codemap.check(root)["ok"] is True


def test_foreign_overwrite_of_committed_shard_is_red(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    make_codegraph_db(root)
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "adopted")
    (root / "map.json").write_text(FOREIGN_MAP)
    report = codemap.check(root)
    assert report["ok"] is False
    assert "map.json" in report["damaged"]


# ---------------------------------------------------------------------------
# Round-6 F2: freshness is one-directional over TRACKED files — untracked
# scratch that real codegraph indexed neither wedges the gate nor maps


def test_untracked_indexed_scratch_neither_wedges_nor_maps(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    (root / "scratch.py").write_text("def wip() -> None:\n    pass\n")  # untracked
    make_codegraph_db(root, extra_files=("scratch.py",))  # codegraph walks the FS
    report = codemap.generate(root)  # no 'behind the worktree' wedge
    assert "scratch.py" not in read_map(root)["modules"]
    assert codemap.check(root)["ok"] is True


# ---------------------------------------------------------------------------
# Round-6 F3: a version mismatch fails immediately — no futile reindex tax


def test_version_mismatch_fails_without_a_reindex(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root, version="9.9.9")  # default path
    prebuilt = make_codegraph_db(root, db_path=tmp_path / "prebuilt.db")
    stub, log = make_stub_codegraph(tmp_path, prebuilt)
    with pytest.raises(codemap.MapError, match=r"9\.9\.9"):
        codemap.generate(root, codegraph_bin=str(stub))
    assert not log.exists()  # the stub was never invoked


# ---------------------------------------------------------------------------
# Round-6 F4: a tracked file codegraph declines (legacy encoding, broken
# syntax) degrades to an `unparsed: true` marker after one reindex attempt —
# the map still builds and the gate still runs


def test_declined_tracked_file_gets_unparsed_marker(tmp_path):
    root = make_repo(tmp_path)
    (root / "legacy.py").write_bytes(b"# caf\xe9\nX = 1\n")  # tracked, undecodable
    make_codegraph_db(root)  # index lacks legacy.py
    prebuilt = make_codegraph_db(root, db_path=tmp_path / "prebuilt.db")
    stub, log = make_stub_codegraph(tmp_path, prebuilt)
    report = codemap.generate(root, codegraph_bin=str(stub))
    assert log.read_text().split() == ["index"]  # one reindex attempt, then degrade
    doc = read_map(root)
    assert doc["modules"]["legacy.py"]["unparsed"] is True
    assert doc["modules"]["legacy.py"]["symbols"] == []
    assert codemap.check(root, codegraph_bin=str(stub))["ok"] is True


# ---------------------------------------------------------------------------
# Round-6 F5: the three verbs share one adoption/damage semantics.
# (a) never-adopted + proofless JSONC anywhere: read views warn, never raise;
# (b) a half-written file AT a would-render path is reportable damage even
# with zero healthy shards and no HEAD


def test_read_views_do_not_raise_on_never_adopted_jsonc(tmp_path):
    root = make_repo(tmp_path)
    assets = root / "assets"
    assets.mkdir()
    (assets / "map.json").write_text("// tile config\n{\"tiles\": [1]}\n")
    out = codemap.catalog(root, budget_lines=40, focus=[])  # no raise
    assert "not adopted" in out
    sl = codemap.slice_modules(root, ["fx"])
    assert sl["modules"] == {} and "not adopted" in sl["note"]


def test_half_written_file_at_render_path_is_not_green(tmp_path):
    root = make_repo(tmp_path)  # no git: no HEAD proof possible
    make_codegraph_db(root)
    (root / "map.json").write_text('{"version": 1, "generator": {"produ')
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["damaged"] == ["map.json"]


# ---------------------------------------------------------------------------
# Round-6 F6: check names a foreign squat itself — no fabricated drift with
# a dead-end remedy


def test_check_names_a_foreign_squat_instead_of_fabricated_drift(tmp_path):
    root = make_two_shard_repo(tmp_path)
    make_two_shard_db(root)
    codemap.generate(root)
    (root / "packages" / "p1" / "map.json").unlink()
    (root / "packages" / "p1" / "map.json").write_text(FOREIGN_MAP)
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["squatted"] == ["packages/p1/map.json"]
    assert report["drifted"] == []


# ---------------------------------------------------------------------------
# Round-6 F8: the sidecar gets the same provenance heal as shards


def test_conflicted_sidecar_heals_from_head(tmp_path):
    root = _git_repo(make_repo(tmp_path))
    make_codegraph_db(root)
    report = codemap.generate(root)
    current = {h["module"]: h["hash"] for h in report["unannotated"]}
    good = json.dumps({"fx/helper.py": {"hash": current["fx/helper.py"],
                                        "note": "formats things"}})
    (root / "map.notes.json").write_text(good)
    codemap.generate(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "annotated")
    (root / "map.notes.json").write_text(CONFLICTED)
    report = codemap.check(root)
    assert report["ok"] is False
    assert report["damaged_notes"] == ["map.notes.json"]
    heal = codemap.generate(root)
    assert heal["healed_notes"] == ["map.notes.json"]
    assert (root / "map.notes.json").read_text() == good  # HEAD's bytes restored
    assert read_map(root)["modules"]["fx/helper.py"]["responsibility"] == "formats things"
    assert codemap.check(root)["ok"] is True


def test_conflicted_sidecar_without_head_is_a_hand_fix_error(tmp_path):
    root = make_repo(tmp_path)
    make_codegraph_db(root)
    codemap.generate(root)
    (root / "map.notes.json").write_text(CONFLICTED)
    with pytest.raises(codemap.MapError, match="by hand"):
        codemap.check(root)
    with pytest.raises(codemap.MapError, match="by hand"):
        codemap.generate(root)


# ---------------------------------------------------------------------------
# Round-6 F10: a default-path db that cannot even be OPENED still self-heals
# (reset + reinit) instead of raising past the advertised provisioning


def test_default_path_open_failure_self_heals(tmp_path):
    root = make_repo(tmp_path)
    (root / ".codegraph" / "codegraph.db").mkdir(parents=True)  # unopenable
    prebuilt = make_db(root, files=REPO_FILES, nodes=REPO_NODES,
                       edges=REPO_EDGES, db_path=tmp_path / "prebuilt.db")
    stub, log = make_stub_codegraph(tmp_path, prebuilt)
    report = codemap.generate(root, codegraph_bin=str(stub))
    assert log.read_text().split() == ["init"]
    assert read_map(root)["modules"]["fx/core.py"] == EXPECTED_CORE
