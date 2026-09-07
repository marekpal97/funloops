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
    """The issue as it crosses from ``gh`` into the pack — a NamedTuple because
    the record is two fields and no behaviour; a class would be a passive one."""

    title: str
    body: str


def compose(number: int, issue: Issue, role: Role, persona: str,
            codegraph: Codegraph, prime: str = "", trace: str = "") -> str:
    """The pack text. ``persona`` is the implementer's, already spliced with
    the constitution (empty for the judge — the one role check is the
    caller's), and the standing orders ride with it. The map is codegraph's:
    both tiers, or the degraded block on any failure — never an exception."""
    try:
        codegraph.sync()
        catalog = render_tree(codegraph.files(), codegraph.root)
        slices = [codegraph.context(issue.title)]
        slices += [codegraph.node(f) for f in named_files(issue.body, codegraph.root)]
        repo_map = (f"## Repo map — tier 1: catalog\n\n{catalog}\n\n"
                    f"## Repo map — tier 2: issue slice\n\n"
                    + "\n\n".join(s.strip("\n") for s in slices))
    except CodegraphUnavailable as e:
        repo_map = DEGRADED.format(reason=e)
    parts = [f"# Dispatch pack — issue #{number} ({role})",
             f"## Issue\n\n{issue.title}\n\n{issue.body.strip()}"]
    if persona:
        parts.append(f"## Persona\n\n{persona}")
    parts.append(repo_map)
    if prime.strip():
        parts.append(f"## Prior lessons\n\n{prime.strip()}")
    if trace.strip():
        parts.append(f"## Run trace\n\n{trace.strip()}")
    if persona:
        parts.append(STANDING_ORDERS)
    return "\n\n".join(parts) + "\n"


class CodegraphUnavailable(Exception):
    """A codegraph verb could not run — the degraded-block trigger."""


class Codegraph:
    """codegraph as a tool the pack invokes: a CLI whose output is spliced,
    never a database it reads (no sqlite, no version pin, no schema test).
    The index under ``root/.codegraph`` is machine-local and self-provisioned
    by ``sync()``, so a fresh worktree maps too."""

    def __init__(self, binary: str, root: Path):
        self.binary, self.root = binary, root

    def sync(self) -> None:
        """``init -y`` when the index is absent, else ``sync``; both say nothing."""
        if (self.root / ".codegraph").is_dir():
            self._run("sync", ".")
        else:
            self._run("init", "-y", ".")

    def files(self) -> list[dict]:
        """``files -j``, parsed: the tool's own records, each ``{path, language,
        nodeCount, size}``. An empty or unparseable list is no catalog."""
        try:
            entries = json.loads(self._run("files", "-j", "-p", "."))
        except json.JSONDecodeError as e:
            raise CodegraphUnavailable(f"files: not JSON: {e}") from e
        if not isinstance(entries, list) or not entries:
            raise CodegraphUnavailable("files: empty output")
        return entries

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


def render_tree(files: list[dict], root: Path) -> str:
    """The catalog: ``Project Structure (N files):`` over a box-drawing tree
    of the records' paths — sorted, directories before files at each level —
    each file as ``name (language, N symbols)``, each module annotated with
    its responsibility. Drawn here, deterministically, from the JSON."""
    tree: dict = {}
    for f in files:
        node = tree
        *dirs, name = f["path"].split("/")
        for d in dirs:
            node = node.setdefault(d, {})
        label = f"{name} ({f['language']}, {f['nodeCount']} symbols)"
        if note := responsibility(root / f["path"]):
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
