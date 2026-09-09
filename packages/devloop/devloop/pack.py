"""The dispatch pack: one dispatch's context, printed by ``devloop pack``.

In order: the issue; the rules — the packaged constitution, then the host
overlay, one continuously numbered list (both roles: the judge cites them);
the persona (implementer only); the repo map — tier 1 the whole-repo catalog
at directory grain, tier 2 the slice for the issue: file lines for the
directories its named files and the title's entry points land in (those
files always, the rest to a line budget, a fold line naming what is cut),
then codegraph's context and the named files' symbols, all from codegraph's CLI,
any failure degrading to a marked block; the prime block and the run's trace
where the host supplies them; the standing orders (implementer only).
Composition is hardcoded; argparse carries the only knobs.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

Role = Literal["implementer", "judge"]

# Each tier renders at most this many lines of its own; over it, the largest
# directories fold first (dec-e6561edc: a fixed backstop, no config key).
# Tier 2 always shows the files the issue points at, even past the budget
# (funloops#53). Sized by the thinkweave judge pack: 6,000 chars of issue
# and rules leave under 2,000 for the map inside its 8,000-char ceiling.
LINE_BUDGET = 16


class Issue(NamedTuple):
    """The issue as it crosses from ``gh`` into the pack: title and body."""

    title: str
    body: str


def compose(number: int, issue: Issue, role: Role, rules: list[str], persona: str,
            codegraph: Codegraph, prime: str = "", trace: str = "") -> str:
    """The pack text. ``rules`` are the resolved constitution bodies in order
    and ride every role; the persona and the standing orders are the
    implementer's — the one role check in the pack. Resolving the files is
    the caller's; an empty ``rules`` is refused (a pack without the rules is
    the fail-open rule 6 names). The map is codegraph's, or the degraded
    block on any failure — never an exception."""
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
        """Both tiers for one issue: the catalog at directory grain, then the
        slice — file lines for the directories the issue's named files and
        the title's entry points land in, the context for the title, the
        symbols of each named file. Raises ``CodegraphUnavailable`` on any
        failure — the caller decides what a missing map means."""
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

    def entry_points(self, title: str) -> list[str]:
        """The files of the same query's entry points, from its ``-f json``
        form, in codegraph's order. Output off the declared shape (no
        ``entryPoints`` list of ``filePath`` records) is no slice."""
        try:
            doc = json.loads(self._run("context", "-p", ".", "-f", "json", "--no-code", title))
            return [str(e["filePath"]) for e in doc["entryPoints"]]
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            raise CodegraphUnavailable(f"context: unexpected shape: {e!r}") from e

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


class Directory:
    """One directory of the map: the records indexed directly under it and
    its subdirectories. Tier 1 is a cut of this tree — every indexed file
    counts on exactly one line, its own directory's or the nearest folded
    ancestor's — and tier 2 opens chosen directories to their files. Only
    the lines rendered read a docstring or a README."""

    def __init__(self, path: str):
        self.path, self.files, self.children, self.folded = path, {}, {}, False

    @classmethod
    def tree(cls, files: list[FileRecord]) -> Directory:
        """The root over ``files -j``'s records (either separator). A path
        that is both a file and a directory, or listed twice, is no map."""
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
        """Tier 2: the directory of each pointed file as its line over its
        files, groups a blank line apart in path order. A group opens whole
        while ``budget`` holds it; past that it folds to the pointed files
        alone, budget or not, or to the first files that still fit when the
        index holds none of them, and ends in a fold line naming the count
        not shown (rule 6: a fold reads as a fold). A directory the index
        does not hold renders as an empty line."""
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
        """While the cut exceeds ``budget`` lines and something can fold: the
        largest (most files) of the innermost foldable directories collapses
        to one line. The root never folds, so the floor is the top-level
        split."""
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
        """The named files directly under it, name order, in the catalog's
        old file-line form ``name (language, N symbols) — responsibility``,
        then the fold line ``… N more files`` when ``hidden`` files are not
        shown."""
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
- TDD per the baseline line. `green`: TDD is enforced — for each acceptance
  criterion with a code-testable seam write the failing test FIRST, watch it
  fail, then implement to green. `red`: the whole-suite guarantee is off; still
  add tests for your slice. Either way you may reshape internals behind the
  seams the criteria name; apply your own cut (the persona's ladder) before
  returning, so the diff you hand back is the shortest one you understand.
  `verify:` lines are the issue author's — never add, edit, or satisfy one by
  changing what it checks.
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

# Provenance: funloops#28, #41, #45, #46, #47, #53; dec-f12457eb, dec-fd12489d, dec-d2de831e,
# dec-ba48dbe2, dec-2f8c2322 (supersedes dec-d79e8e7b's persona-as-splice-container),
# dec-e6561edc (directory grain, the line budget), dec-72c80057 (the pack is the
# whole dispatch: the standing orders live here, not in the command doc).
