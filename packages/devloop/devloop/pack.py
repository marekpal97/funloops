"""The dispatch pack: one dispatch's context, printed by ``devloop pack``.

In order: the issue; the rules; the persona (implementer only); the repo
map from codegraph's CLI, a catalog tier and an issue-slice tier, any
failure degrading to a marked block; the prime block and the run's trace
when the host supplies them; the standing orders (implementer only).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

Role = Literal["implementer", "judge"]

# Each tier renders at most this many lines; over it, the largest directories
# fold first. Sized so the map fits under 2,000 chars of an 8,000-char pack.
LINE_BUDGET = 16


class Issue(NamedTuple):
    """The issue as it crosses from ``gh`` into the pack: title and body."""

    title: str
    body: str


def compose(number: int, issue: Issue, role: Role, rules: list[str], persona: str,
            codegraph: Codegraph, prime: str = "", trace: str = "") -> str:
    """The pack text for one dispatch. An empty ``rules`` is refused; a
    codegraph failure renders the degraded block, never an exception."""
    if not rules:
        raise ValueError("a pack carries the rules; none were resolved")
    try:
        repo_map = codegraph.repo_map(issue)
    except CodegraphUnavailable as e:
        repo_map = DEGRADED.format(reason=e)
    implementer = role == "implementer"
    parts = [f"# Dispatch pack — issue #{number} ({role})",
             f"## Issue\n\n{issue.title}\n\n{issue.body.strip()}",
             "## Rules\n\n" + "\n\n".join(rules)]
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
    """One entry of ``codegraph files -j``: the three fields the catalog draws."""

    path: str
    language: str
    node_count: int


class Codegraph:
    """codegraph as a CLI the pack invokes; its index is never read directly.
    The index under ``root/.codegraph`` is self-provisioned by ``sync()``."""

    def __init__(self, binary: str, root: Path):
        self.binary, self.root = binary, root

    def repo_map(self, issue: Issue) -> str:
        """Both map tiers for one issue: the catalog, then the slice around
        its named files and entry points. Raises ``CodegraphUnavailable`` on
        any failure."""
        self.sync()
        tree = Directory.tree(self.files())
        named = named_files(issue.body, self.root)
        pointed = [*named, *self.entry_points(issue.title)]
        slices = [tree.slice(pointed, self.root, LINE_BUDGET), self.context(issue.title)]
        slices += [self.node(f) for f in named]
        return (f"## Repo map — tier 1: catalog\n\n{tree.catalog(self.root, LINE_BUDGET)}\n\n"
                f"## Repo map — tier 2: issue slice\n\n"
                + "\n\n".join(s.strip("\n") for s in slices if s.strip()))

    def sync(self) -> None:
        """``init -y`` when the index is absent, else ``sync``; both say nothing."""
        if (self.root / ".codegraph").is_dir():
            self._run("sync", ".")
        else:
            self._run("init", "-y", ".")

    def files(self) -> list[FileRecord]:
        """``files -j`` parsed into records; an empty list, a non-list, or a
        short entry raises ``CodegraphUnavailable``."""
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

    def entry_points(self, title: str) -> list[str]:
        """The entry-point files of ``context -f json`` for the title, in
        codegraph's order; output off the declared shape raises."""
        try:
            doc = json.loads(self._run("context", "-p", ".", "-f", "json", "--no-code", title))
            return [str(e["filePath"]) for e in doc["entryPoints"]]
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            raise CodegraphUnavailable(f"context: unexpected shape: {e!r}") from e

    def node(self, path: str) -> str:
        """``node -f <path> --symbols-only``: one file's symbols and dependents."""
        return self._run("node", "-p", ".", "-f", path, "--symbols-only")

    def _run(self, *args: str) -> str:
        """One verb's stdout, colorless; any failure raises naming the verb.
        A read verb that prints nothing raises too: spliced, it would read as
        a clean empty catalog. init/sync say nothing."""
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


class Directory:
    """One directory of the map: the records directly under it and its
    subdirectories. Tier 1 is a cut of this tree where every indexed file
    counts on exactly one line; tier 2 opens chosen directories to their
    files."""

    def __init__(self, path: str):
        self.path, self.files, self.children, self.folded = path, {}, {}, False

    @classmethod
    def tree(cls, files: list[FileRecord]) -> Directory:
        """The root over ``files -j``'s records. A path that is both a file
        and a directory, or listed twice, raises."""
        root = cls("")
        for f in files:
            *dirs, name = parts(f.path)
            node = root
            for d in dirs:
                if d in node.files:
                    raise CodegraphUnavailable(f"files: {f.path!r} is under a file")
                node = node.children.setdefault(d, cls(f"{node.path}/{d}".lstrip("/")))
            if name in node.files or name in node.children:
                raise CodegraphUnavailable(f"files: {f.path!r} listed twice or also a directory")
            node.files[name] = f
        return root

    def catalog(self, root: Path, budget: int) -> str:
        """Tier 1: ``Project Structure (N files):`` over the cut, one line per
        directory, path order, folded down to ``budget`` lines."""
        self.fold(budget)
        lines = [d.line(root, whole=d.folded) for d in self.lines()]
        return "\n".join([f"Project Structure ({self.count()[0]} files):", "", *lines])

    def slice(self, paths: list[str], root: Path, budget: int) -> str:
        """Tier 2: each pointed file's directory as its line over its files,
        in path order. A group opens whole while ``budget`` holds it; past
        that it folds to the pointed files, and a fold line names the count
        not shown. A directory the index does not hold renders as an empty
        line."""
        pointed: dict[str, set[str]] = {}
        for p in paths:
            *dirs, name = parts(p)
            pointed.setdefault("/".join(dirs), set()).add(name)
        groups = [self.find(d) for d in sorted(pointed)]
        must = {g: sorted(pointed[g.path] & g.files.keys()) for g in groups}
        rest = {g: sorted(g.files.keys() - pointed[g.path]) for g in groups}
        left = budget - sum(1 + len(must[g]) for g in groups)
        blocks = []
        for g in groups:
            n = len(rest[g]) if len(rest[g]) <= left else 0 if must[g] else max(left - 1, 0)
            left -= n + (n < len(rest[g]))  # a fold line costs one
            blocks.append("\n".join([g.line(root), *g.file_lines(root, must[g] + rest[g][:n], len(rest[g]) - n)]))
        return "\n\n".join(blocks)

    def fold(self, budget: int) -> None:
        """Collapse the largest innermost foldable directory to one line until
        the cut fits ``budget``. The root never folds."""
        while len(self.lines()) > budget and (foldable := self.foldable()):
            max(foldable, key=lambda d: d.count()[0]).folded = True

    def lines(self) -> list[Directory]:
        """The cut, in path order: this directory if it holds files or is
        folded, then its subdirectories' cuts."""
        if self.folded:
            return [self]
        own = [self] if self.files else []
        return own + [d for c in sorted(self.children.values(), key=lambda c: c.path)
                      for d in c.lines()]

    def foldable(self) -> list[Directory]:
        """The innermost directories below the root whose subtree still
        renders more than one line: the ones a fold would shorten."""
        if self.folded:
            return []
        inner = [d for c in self.children.values() for d in c.foldable()]
        return inner or ([self] if self.path and len(self.lines()) > 1 else [])

    def find(self, path: str) -> Directory:
        """The directory at ``path``, or an empty one when the index holds no
        file under it."""
        node = self
        for d in parts(path):
            if d not in node.children:
                return Directory(path)
            node = node.children[d]
        return node

    def count(self, whole: bool = True) -> tuple[int, int]:
        """Files and symbols: the whole subtree's, or only the files directly
        under it."""
        n, m = len(self.files), sum(f.node_count for f in self.files.values())
        for c in self.children.values() if whole else ():
            cn, cm = c.count()
            n, m = n + cn, m + cm
        return n, m

    def line(self, root: Path, whole: bool = False) -> str:
        """``<dir>/ (<N> files, <M> symbols) — <responsibility>``, counting the
        whole subtree when ``whole`` (a folded line), else the files directly
        under it."""
        n, m = self.count(whole)
        text = f"{self.path or '.'}/ ({n} files, {m} symbols)"
        if note := self.responsibility(root):
            text += f" — {note}"
        return text

    def file_lines(self, root: Path, names: list[str], hidden: int) -> list[str]:
        """The named files as ``name (language, N symbols) — responsibility``
        lines in name order, then ``… N more files`` when ``hidden`` is set."""
        lines = []
        for name in sorted(names):
            f = self.files[name]
            text = f"{name} ({f.language}, {f.node_count} symbols)"
            if note := responsibility(root.joinpath(*parts(f.path))):
                text += f" — {note}"
            lines.append(text)
        if hidden:
            lines.append(f"… {hidden} more files")
        return [("└── " if i == len(lines) - 1 else "├── ") + t for i, t in enumerate(lines)]

    def responsibility(self, root: Path) -> str:
        """The first docstring line of ``__init__.py``, else the first
        non-empty line of a README (its heading marks dropped), else ''."""
        here = root.joinpath(*parts(self.path))
        if note := responsibility(here / "__init__.py"):
            return note
        for readme in sorted(here.glob("README*")):
            for raw in readme.read_text(encoding="utf-8", errors="replace").splitlines():
                if line := raw.strip().lstrip("#").strip():
                    return line
        return ""


def responsibility(path: Path) -> str:
    """A module's responsibility: the first line of its docstring, else ''."""
    if path.suffix != ".py" or not path.is_file():
        return ""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return ""
    return doc.strip().splitlines()[0] if doc and doc.strip() else ""


def parts(path: str) -> tuple[str, ...]:
    """A path's segments, either separator, a leading ``./`` dropped."""
    return PurePosixPath(path.replace("\\", "/")).parts


def named_files(body: str, root: Path) -> list[str]:
    """Every backticked token in ``body`` that resolves to a file inside
    ``root``, first mention first, deduped. Symlinks are followed, so a link
    out of the repo does not count."""
    found = [tok for tok in re.findall(r"`([^`\n]+)`", body)
             if (root / tok).resolve().is_relative_to(root) and (root / tok).is_file()]
    return list(dict.fromkeys(found))


def body(path: Path) -> str:
    """A doc's text below its leading ``<!-- … -->`` header; a doc that opens
    with prose is served whole."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("<!--"):
        text = text.partition("-->")[2]
    return text.strip("\n")


DEGRADED = """\
## Repo map — DEGRADED (codegraph unavailable: {reason})

No catalog or slice was spliced. Before editing, gather it yourself: the
package layout (the tree of source files), the public surface of every module
the issue touches (its top-level definitions and signatures), and their import
neighbours (what they import, who imports them)."""

STANDING_ORDERS = """\
## Standing orders

The dispatch is this pack plus two lines the orchestrator adds: the branch
name and the baseline verdict (`green` or `red`, the tests gate on the pristine
worktree). Nothing else instructs you; a rule stated here is stated once.

- Read the repo's architecture and design docs for the areas you touch before
  editing them; a documented standard overrides your instinct.
- Check prior decisions for every file you touch
  (`weave_graph(file_path=…, filter='decisions_for_file')`; `weave decisions
  --file <path>` when MCP is absent). Never re-litigate a settled decision;
  surface the conflict in your return.
- TDD per the baseline line. `green`: TDD is enforced — for each acceptance
  criterion with a code-testable seam write the failing test FIRST, watch it
  fail, then implement to green. `red`: the whole-suite guarantee is off; still
  add tests for your slice. Either way you may reshape internals behind the
  seams the criteria name; apply your own cut (the persona's ladder) before
  returning, so the diff you hand back is the shortest one you understand.
  `verify:` lines are the issue author's — never add, edit, or satisfy one by
  changing what it checks.
- Before returning, run the tests gate command and every `verify:` line from
  the worktree root. Paste each result in your return.
- Use the code you changed the way the issue describes. If it does not do
  what the issue says, fix it or report the criterion as not met.
- Test at seams governs the tests you commit, not what you may run. Run
  whatever you need to convince yourself.
- **Test at seams.** Test only at the seams the issue names (its acceptance
  criteria / named interfaces); if it names none, choose them and declare the
  choice in your return so it lands in the PR body — never scatter tests across
  internals. **No tautological tests.** Expected values come from an independent
  source of truth (the issue's criteria, a hand-computed value, a fixture) —
  never recomputed the same way the code under test computes them.
- Commit in slice-sized increments on the branch named in the dispatch.
  Do NOT push, do NOT open a PR, do NOT close or label anything — the
  orchestrator owns the control plane.
- Return: worktree path, branch, files touched, test commands run, any deviation
  from the issue's declared shapes with its reason, and any acceptance criterion
  you believe is NOT yet met (honesty over green-washing). A report longer than
  a screen is a file in the worktree; return its path.

**Drill down with codegraph's CLI.** The catalog and slice above are already
spliced; do not re-derive them. Before writing, look at what exists:
`codegraph explore "<area>"` (an area's symbols and call paths), `codegraph
node <symbol>` / `codegraph node -f <file>` (one symbol or file with its
dependents), `codegraph impact <symbol>` and `codegraph callers` / `codegraph
callees <symbol>` (who is affected by a change)."""
