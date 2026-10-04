"""The dispatch pack: one role's complete dispatch, written by ``devloop pack``.

In order: the issue; the rules; the persona (writer posture only); the
repo map from codegraph's CLI, a catalog tier and an issue-slice tier, any
failure degrading to a marked block; the touched modules in full and the
slice diff (reader posture); the prime block and the run's trace when the
host supplies them; the posture's brief (the standing orders, the judge
brief, the shape brief); the dispatch lines. The shape posture packs a
stack: its issues, the rules, the touched modules, and codegraph's edges
between them in place of the repo map. The role is any ``[dispatch]`` entry;
its ``posture`` picks the sections.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

Posture = Literal["writer", "reader", "shape"]

# Each tier renders at most this many lines; over it, the largest directories
# fold first. Sized so the map fits under 2,000 chars of an 8,000-char pack.
LINE_BUDGET = 16


class Issue(NamedTuple):
    """The issue as it crosses from ``gh`` into the pack: title and body."""

    title: str
    body: str


def compose(issues: dict[int, Issue], role: str, posture: Posture, rules: list[str],
            persona: str, codegraph: Codegraph, touched: Touched | None = None,
            prime: str = "", trace: str = "", dispatch: Dispatch | None = None) -> str:
    """The whole dispatch text: ``role`` names it, ``posture`` shapes it and
    picks its brief, ``dispatch`` closes it. Only a shape pack spans several
    issues; a reader or shape pack carries ``touched``. Empty ``rules`` are
    refused; a codegraph failure renders a degraded block, never an
    exception."""
    if not rules:
        raise ValueError("a pack carries the rules; none were resolved")
    if posture != "shape" and len(issues) != 1:
        raise ValueError(f"a {posture} pack is one issue's, got {len(issues)}")
    if posture != "writer" and touched is None:
        raise ValueError(f"a {posture} pack carries the touched modules; none were resolved")
    writer, shape = posture == "writer", posture == "shape"
    numbers = ", ".join(f"#{n}" for n in issues)
    parts = [(f"# Dispatch pack — issue{'s' * (len(issues) > 1)} {numbers} "
              f"({role}{', shape' * shape})")]
    parts += [f"## Issue{f' #{n}' * shape}\n\n{issue.title}\n\n{issue.body.strip()}"
              for n, issue in issues.items()]
    parts.append("## Rules\n\n" + "\n\n".join(rules))
    if writer:
        parts.append(f"## Persona\n\n{persona}")
    if not shape:
        try:
            parts.append(codegraph.repo_map(next(iter(issues.values()))))
        except CodegraphUnavailable as e:
            parts.append(DEGRADED.format(reason=e))
    if touched is not None:
        parts.append(touched.render())
    if posture == "reader":
        parts.append(f"## Diff\n\n`git diff {touched.base}...HEAD`:\n\n"
                     + fenced(touched.diff, "diff"))
    if shape:
        try:
            parts.append(codegraph.edges(list(touched.modules)))
        except CodegraphUnavailable as e:
            parts.append(EDGES_DEGRADED.format(reason=e))
    if prime.strip():
        parts.append(f"## Prior lessons\n\n{prime.strip()}")
    if trace.strip():
        parts.append(f"## Run trace\n\n{trace.strip()}")
    parts.append(BRIEFS[posture])
    if dispatch and (lines := dispatch.render()):
        parts.append(lines)
    return "\n\n".join(parts) + "\n"


class Dispatch(NamedTuple):
    """What closes a dispatch: the reader's check output and evidence
    directory, then the branch, baseline, return-file and worktree lines.
    An empty field renders nothing."""

    branch: str = ""
    baseline: str = ""
    return_file: str = ""
    worktree: str = ""
    check: str = ""
    evidence: str = ""

    def render(self) -> str:
        blocks = [f"## Check output\n\n{fenced(self.check, 'json')}"] if self.check.strip() else []
        lines = [f"{label}: {value}" for label, value in (
            ("Evidence directory", self.evidence), ("Branch", self.branch),
            ("Baseline", self.baseline), ("Return file", self.return_file),
            ("Worktree", self.worktree)) if value]
        if lines:
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


class Touched(NamedTuple):
    """The files a range touches that HEAD still holds: each path's text, or
    ``None`` when it is not UTF-8 text; and the range's diff."""

    base: str
    modules: dict[str, str | None]
    diff: str = ""

    @classmethod
    def since(cls, base: str, root: Path) -> Touched:
        """The files ``base...HEAD`` touches in ``root``; a git failure raises
        ``ValueError`` naming the range."""
        proc = subprocess.run(["git", "diff", "--name-only", "--diff-filter=d", "-z",
                               f"{base}...HEAD"], cwd=root, capture_output=True,
                              text=True, check=False)
        if proc.returncode != 0:
            raise ValueError(f"git diff {base}...HEAD: {proc.stderr.strip()}")
        modules: dict[str, str | None] = {}
        for path in sorted(filter(None, proc.stdout.split("\0"))):
            try:
                modules[path] = root.joinpath(*parts(path)).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                modules[path] = None
        diff = subprocess.run(["git", "diff", f"{base}...HEAD"], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", check=True).stdout
        return cls(base, modules, diff)

    def render(self) -> str:
        """``## Touched modules``: each file in full, its lines numbered."""
        head = (f"## Touched modules\n\nEvery file `{self.base}...HEAD` touches, in "
                "full, its lines numbered for `file:line` citations.")
        if not self.modules:
            return f"{head}\n\nThe range touches no file."
        blocks = [head]
        for path, text in self.modules.items():
            if text is None:
                blocks.append(f"### {path}\n\nNot UTF-8 text; not shown.")
                continue
            lines = [f"{i:>4}  {line}".rstrip() for i, line in enumerate(text.splitlines(), 1)]
            blocks.append(f"### {path}\n\n" + fenced("\n".join(lines)))
        return "\n\n".join(blocks)


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

    def edges(self, paths: list[str]) -> str:
        """``## Module edges``: each path's ``node`` block, its symbols and the
        files that depend on it. Raises ``CodegraphUnavailable`` on any
        failure."""
        self.sync()
        blocks = [self.node(p).strip("\n") for p in paths] or ["No module to map."]
        return "## Module edges\n\n" + "\n\n".join(blocks)

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


def fenced(text: str, lang: str = "") -> str:
    """``text`` in a code fence longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text.rstrip(chr(10))}\n{fence}"


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

EDGES_DEGRADED = """\
## Module edges — DEGRADED (codegraph unavailable: {reason})

The module text above is whole; the import and call edges between the modules are missing."""

STANDING_ORDERS = """\
## Standing orders

This file is the whole dispatch. Its last lines name the branch, the baseline
verdict (`green` or `red`, the tests gate on the pristine worktree), the
return file path and the worktree you work inside. Nothing else instructs
you; a rule stated here is stated once.

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
- Before returning, run `devloop check --issue <N>` (N is this pack's issue)
  from the worktree root: one call runs the command gates and every `verify:`
  line. Paste its result in your return.
- Use the code you changed the way the issue describes. If it does not do
  what the issue says, fix it or report the criterion as not met.
- Run every `demo:` criterion on your final commit, by script or by driving
  a tool, and record it in `<return file>.demo/demo.md`: first line
  `sha: <full commit sha>`, then one section per demo with the steps taken
  (commands verbatim, or an action log) and the artifact file names. Save
  the artifacts beside it as files the judge can read: text output,
  screenshots, DOM dumps, transcripts. A fix round re-runs every `demo:`
  criterion on the new tip and rewrites `demo.md`. A demo the environment
  cannot run is not met: name the missing capability in your return.
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
- Return: worktree path, branch, files touched, test commands run, each
  deviation from the issue's declared shapes as one sentence with its reason,
  and any acceptance criterion you believe is NOT yet met (honesty over
  green-washing). Write the whole
  return to the return file the dispatch names; the orchestrator reads that
  file, never your screen.

**Drill down with codegraph's CLI.** The catalog and slice above are already
spliced; do not re-derive them. Before writing, look at what exists:
`codegraph explore "<area>"` (an area's symbols and call paths), `codegraph
node <symbol>` / `codegraph node -f <file>` (one symbol or file with its
dependents), `codegraph impact <symbol>` and `codegraph callers` / `codegraph
callees <symbol>` (who is affected by a change)."""


JUDGE_BRIEF = """\
## Judge brief

You are the judge: a fresh reader of one slice. The contract is the issue's
acceptance criteria, the Interfaces block's intent, and rules 3, 6, 7 and 8
of the Rules section. Judge the diff and the touched modules against that
contract and nothing else. Work in the worktree the last lines name; do not
edit code.

- Return one verdict per criterion, `"met"` or `"not-met"`, each with one
  line of evidence. A criterion is not met when the code does not do what it
  says.
- A `verify:` criterion takes its verdict from the Check output section
  below: cite that output, never re-run the command. Where a criterion is
  prose and no verify line ran it, run it yourself and cite the output.
  Evidence for a prose criterion is something you ran against the code,
  never only a test the diff adds.
- A `demo:` criterion is scored from the evidence directory only; never
  re-run the demo. Its `demo.md` opens with `sha: <commit>`. The steps it
  records must match the demo text, and that SHA must be the implementation
  tip (`git rev-parse HEAD` in the worktree). Rest the verdict on the
  artifacts you inspect yourself (output, screenshots, DOM dumps), never on
  the implementer's own assessment, and cite the artifact file. No evidence,
  or evidence on another SHA, is `not-met`. Evidence that cannot settle the
  demo's observable is `not-met` too: the evidence line names what the
  evidence lacks.
- Code the diff changes that no longer works as the issue intends is
  `not-met` too, even when no criterion names the case: return it as one
  more criterion under the reserved id `intent`, and only when you have the
  failure in hand.
- A violation of rule 3, 6, 7 or 8 in code the diff adds or changes is
  `not-met` under the reserved id `rule:<n>`, one entry per violation. Its
  evidence cites the `file:line` from the Touched modules section and says
  what breaks the rule. New code that copies an existing rule-breaking
  pattern is a `rule:<n>` violation: an older precedent excuses nothing.
  Code the diff does not touch never blocks. Rules 1 and 4 are not yours
  here, and no other rule blocks.
- Every `not-met` names a command, a test, or output; without one it is not
  a `not-met`, it is a finding.
- Everything else you notice (a risk, a smell, a better design, an edge
  case outside the contract) is a finding with one of two severities:
  `"problem"` (the code is wrong or fragile in a way you can describe but did
  not demonstrate; a human reads it before merge) or `"note"` (style,
  naming, a cleaner shape). A finding is one sentence naming the rule in the
  Rules section it violates (by number) or the exercised path it breaks (the
  documented verb and the normal state that reaches it); a finding that
  names neither is dropped, not listed. The observation first; no clause
  that withdraws it. Findings never block, whatever their severity. Do not
  search for problems the contract does not name.

Write exactly this object, as JSON, to the return file the last lines name.
`evidence` and `finding` are never blank; `findings` may be empty, not
absent.

```json
{"criteria": [{"id": "AC1", "verdict": "met", "evidence": "<one line>"},
              {"id": "intent", "verdict": "not-met",
               "evidence": "<the command or test you ran and its output>"}],
 "findings": [{"severity": "note", "finding": "<prose>"}]}
```"""

SHAPE_BRIEF = """\
## Shape brief

You are the judge in the shape posture. The contract is rules 1 and 4 of the
Rules section and every Interfaces block in the issues above. Judge the
module structure of the touched modules against that contract and nothing
else. Do not edit code.

- For rule 1, name each domain concept the stack touches (a lifecycle, a
  status rule) and the modules a reader must visit to follow it. Rule 1
  breaks when one concept's behaviour is split or re-derived across several
  modules, or when a new module has one consumer; adding no module does not
  by itself keep it. A record's own constructors and checks beside the
  record, or a helper moved to the one module its consumers share, deepen a
  module and do not break it.
- For rule 4, read each touched module from its top. It breaks when the top
  does not show the module's flow in a few short names, when one module
  holds several jobs as loose functions side by side, or when several
  functions each re-spell the same main-flow steps instead of one name
  owning them.
- The main flow is the path every route or verb of the stack runs. Fail the
  contract for a break on the main flow. A seam off it does not fail the
  contract: name it in `flow` under a `"met"` verdict.
- Return `"met"` when the main flow has one owning module that reads
  top-down and owns what its Interfaces block declares. Otherwise return
  `"not-met"` with a restructure case. `flow` states the current flow in at
  most five lines and names each broken rule by number. `owns` says, per
  module, what it should own. `options` gives one to three shape options,
  one sentence each.

Write exactly this object, as JSON, to the return file the last lines name.
A `"met"` return may leave `flow` empty and `owns` and `options` as empty
lists; its `flow` may name the seams off the main flow.

```json
{"verdict": "not-met",
 "flow": "<at most five lines naming rule 1 and/or 4>",
 "owns": [{"module": "<path>", "owns": "<what it should own>"}],
 "options": ["<shape option>"]}
```"""

BRIEFS: dict[str, str] = {"writer": STANDING_ORDERS, "reader": JUDGE_BRIEF, "shape": SHAPE_BRIEF}
