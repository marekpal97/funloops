"""The dispatch pack: one dispatch's context, printed by ``devloop pack``.

In order: the issue; the persona with the resolved constitution at its marker
(implementer only); the repo map — tier 1 the whole-repo catalog, tier 2 the
slice for the issue's title and every file it names, both from codegraph's
CLI, any failure degrading to a marked block; the prime block and the run's
trace where the host supplies them; the standing orders (implementer only).
Composition is hardcoded; argparse carries the only knobs.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Literal, NamedTuple

Role = Literal["implementer", "judge"]


class Issue(NamedTuple):
    """The issue as it crosses from ``gh`` into the pack: title and body."""

    title: str
    body: str


def compose(number: int, issue: Issue, role: Role, persona: str,
            codegraph: Codegraph, prime: str = "", trace: str = "") -> str:
    """The pack text. The persona (already spliced with the constitution) and
    the standing orders are the implementer's — the one role check in the
    pack; resolving the persona at all is the caller's. The map is
    codegraph's, or the degraded block on any failure — never an exception."""
    try:
        repo_map = codegraph.repo_map(issue)
    except CodegraphUnavailable as e:
        repo_map = DEGRADED.format(reason=e)
    implementer = role == "implementer"
    parts = [f"# Dispatch pack — issue #{number} ({role})",
             f"## Issue\n\n{issue.title}\n\n{issue.body.strip()}"]
    if implementer:
        parts.append(f"## Persona\n\n{persona}")
    parts.append(repo_map)
    if prime.strip():
        parts.append(f"## Prior lessons\n\n{prime.strip()}")
    if trace.strip():
        parts.append(f"## Run trace\n\n{trace.strip()}")
    if implementer:
        parts.append(STANDING_ORDERS)
    return "\n\n".join(parts) + "\n"


class CodegraphUnavailable(Exception):
    """A codegraph verb could not run — the degraded-block trigger."""


class FileRecord(NamedTuple):
    """One entry of ``codegraph files -j`` as the pack reads it: the three
    fields the catalog draws. Declared here so a shape mismatch degrades."""

    path: str
    language: str
    node_count: int


class Codegraph:
    """codegraph as a tool the pack invokes: a CLI whose output is spliced,
    never a database it reads (no sqlite, no version pin). The pack depends
    on the record fields of ``files -j``; that dependency is checked where
    the JSON is parsed into ``FileRecord``, and a shape mismatch degrades.
    The index under ``root/.codegraph`` is machine-local and self-provisioned
    by ``sync()``, so a fresh worktree maps too."""

    def __init__(self, binary: str, root: Path):
        self.binary, self.root = binary, root

    def repo_map(self, issue: Issue) -> str:
        """Both tiers for one issue: the catalog, then the slice for the
        issue's title and every file it names. Raises ``CodegraphUnavailable``
        on any failure — the caller decides what a missing map means."""
        self.sync()
        catalog = render_tree(self.files(), self.root)
        slices = [self.context(issue.title)]
        slices += [self.node(f) for f in named_files(issue.body, self.root)]
        return (f"## Repo map — tier 1: catalog\n\n{catalog}\n\n"
                f"## Repo map — tier 2: issue slice\n\n"
                + "\n\n".join(s.strip("\n") for s in slices))

    def sync(self) -> None:
        """``init -y`` when the index is absent, else ``sync``; both say nothing."""
        if (self.root / ".codegraph").is_dir():
            self._run("sync", ".")
        else:
            self._run("init", "-y", ".")

    def files(self) -> list[FileRecord]:
        """``files -j``, parsed into records. An empty list, a non-list, or an
        entry missing a field is no catalog."""
        try:
            entries = json.loads(self._run("files", "-j", "-p", "."))
        except json.JSONDecodeError as e:
            raise CodegraphUnavailable(f"files: not JSON: {e}") from e
        if not isinstance(entries, list) or not entries:
            raise CodegraphUnavailable("files: empty output")
        records = []
        for entry in entries:
            try:
                records.append(FileRecord(str(entry["path"]), str(entry["language"]),
                                          int(entry["nodeCount"])))
            except (TypeError, KeyError, ValueError) as e:
                raise CodegraphUnavailable(f"files: unexpected record: {entry!r}") from e
        return records

    def context(self, title: str) -> str:
        """``context --no-code <title>``: the slice around the issue's title."""
        return self._run("context", "-p", ".", "--no-code", title)

    def node(self, path: str) -> str:
        """``node -f <path> --symbols-only``: one file's symbols and dependents."""
        return self._run("node", "-p", ".", "-f", path, "--symbols-only")

    def _run(self, *args: str) -> str:
        """One verb's stdout, colorless; any failure names the verb. A read
        verb that prints nothing is not a map — it is a wrapper, a wrong binary,
        or a version writing elsewhere; spliced, it would read as a clean empty
        catalog (rule 6). init/sync legitimately say nothing."""
        try:
            proc = subprocess.run([self.binary, "--no-color", *args], cwd=self.root,
                                  capture_output=True, text=True, check=False,
                                  timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CodegraphUnavailable(f"{args[0]}: {e}") from e
        if proc.returncode != 0:
            raise CodegraphUnavailable(
                f"{args[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()}")
        if args[0] not in ("init", "sync") and not proc.stdout.strip():
            raise CodegraphUnavailable(f"{args[0]}: empty output")
        return proc.stdout


def responsibility(path: Path) -> str:
    """A module's responsibility: the first line of its docstring, else ''."""
    if path.suffix != ".py" or not path.is_file():
        return ""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return ""
    return doc.strip().splitlines()[0] if doc and doc.strip() else ""


def render_tree(files: list[FileRecord], root: Path) -> str:
    """The catalog: ``Project Structure (N files):`` over a box-drawing tree
    of the records' paths (either separator) — sorted, directories before
    files at each level — each file as ``name (language, N symbols)``, each
    module annotated with its responsibility. A path that is both a file and
    a directory, or listed twice, is no catalog."""
    tree: dict = {}
    for f in files:
        node = tree
        *dirs, name = re.split(r"[\\/]", f.path)
        for d in dirs:
            node = node.setdefault(d, {})
            if not isinstance(node, dict):
                raise CodegraphUnavailable(f"files: {f.path!r} is under a file")
        if name in node:
            raise CodegraphUnavailable(f"files: {f.path!r} listed twice")
        label = f"{name} ({f.language}, {f.node_count} symbols)"
        if note := responsibility(root / f.path):
            label += f" — {note}"
        node[name] = label
    lines = [f"Project Structure ({len(files)} files):", ""]

    def draw(node: dict, prefix: str) -> None:
        entries = sorted(node.items(), key=lambda kv: (not isinstance(kv[1], dict), kv[0]))
        for i, (name, sub) in enumerate(entries):
            last = i == len(entries) - 1
            if isinstance(sub, dict):
                lines.append(f"{prefix}{'└── ' if last else '├── '}{name}")
                draw(sub, prefix + ("    " if last else "│   "))
            else:
                lines.append(f"{prefix}{'└── ' if last else '├── '}{sub}")

    draw(tree, "")
    return "\n".join(lines)


def named_files(body: str, root: Path) -> list[str]:
    """The files an issue names: every backticked token that resolves to an
    existing file inside ``root`` (symlinks followed, so a link out of the
    repo does not count), first mention first, deduped."""
    found = [tok for tok in re.findall(r"`([^`\n]+)`", body)
             if (root / tok).resolve().is_relative_to(root) and (root / tok).is_file()]
    return list(dict.fromkeys(found))


def body(path: Path) -> str:
    """Everything below a doc's LEADING provenance header (``<!-- … -->``);
    a doc that opens with prose is served whole, whatever it contains."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("<!--"):
        text = text.partition("-->")[2]
    return text.strip("\n")


def splice(persona: str, rules: list[str]) -> str:
    """The persona with the constitution injected: the ``rules`` bodies
    (packaged first, then the host overlay) replace the persona's one
    ``MARKER`` line, joined by a blank line. A persona without exactly one
    marker is refused — appended silently, the rules would ride outside the
    text that reads them (rule 6)."""
    lines = persona.split("\n")
    if lines.count(MARKER) != 1:
        raise ValueError(f"persona must carry exactly one '{MARKER}' line")
    at = lines.index(MARKER)
    return "\n".join([*lines[:at], "\n\n".join(rules), *lines[at + 1:]])


DEGRADED = """\
## Repo map — DEGRADED (codegraph unavailable: {reason})

No catalog or slice was spliced. Before editing, gather it yourself: the
package layout (the tree of source files), the public surface of every module
the issue touches (its top-level definitions and signatures), and their import
neighbours (what they import, who imports them)."""

STANDING_ORDERS = """\
## Standing orders — drill down with codegraph's CLI

The catalog and slice above are already spliced; do not re-derive them. Before
writing, look at what exists: `codegraph explore "<area>"` (an area's symbols
and call paths), `codegraph node <symbol>` / `codegraph node -f <file>` (one
symbol or file with its dependents), `codegraph impact <symbol>` and
`codegraph callers` / `codegraph callees <symbol>` (who is affected by a
change)."""

MARKER = "<!-- constitution -->"

# Provenance: funloops#28, #41; dec-f12457eb, dec-fd12489d, dec-d2de831e, dec-d79e8e7b, dec-ba48dbe2.
