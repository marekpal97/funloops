"""The dispatch pack: one dispatch's context, composed from codegraph's CLI text.

``devloop pack <N> --role implementer|judge`` (funloops#28; dec-f12457eb,
dec-fd12489d, dec-d2de831e) prints, deterministically and in order: the
issue body; the persona with the constitution injected (implementer only); the
repo map spliced from codegraph's CLI text — tier 1 the whole-repo catalog
(``codegraph files``, each module annotated with the first line of its own
docstring as its responsibility), tier 2 ``codegraph context --no-code
<title>`` plus ``codegraph node --file … --symbols-only`` for every file the
issue names; the prime block and the run's trace where the host extension
supplies them; the drill-down standing orders (implementer only). Composition
is hardcoded; argparse carries the only knobs.

codegraph is a tool the pack invokes, never a database it reads: no sqlite,
no version pin, no schema test. The index under ``<root>/.codegraph`` is
machine-local and self-provisioned by CLI call (``init -y`` when absent,
``sync`` otherwise) so a fresh worktree maps too. Any failure — no binary, a
failed verb — degrades to a marked block asking the model to gather the
layout itself; the pack never blocks (rule 7: a degraded path announces
itself, and a missing map is a weaker dispatch, not a wrong one).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path


class CodegraphUnavailable(Exception):
    """A codegraph verb could not run — the degraded-block trigger."""


def codegraph_bin(explicit: str | None) -> str:
    """``--codegraph-bin`` → ``$CODEGRAPH_BIN`` → ``codegraph`` on PATH."""
    return explicit or os.environ.get("CODEGRAPH_BIN") or "codegraph"


def _codegraph(binary: str, root: Path, *args: str) -> str:
    """One verb's stdout, colorless; any failure names the verb."""
    try:
        proc = subprocess.run([binary, "--no-color", *args], cwd=root,
                              capture_output=True, text=True, check=False,
                              timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CodegraphUnavailable(f"{args[0]}: {e}") from e
    if proc.returncode != 0:
        raise CodegraphUnavailable(
            f"{args[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()}")
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


_TREE_LINE = re.compile(r"^((?:(?:│|\s)\s{3})*)[├└]── (.+?)(?: \([^()]*\))?$")


def annotate_catalog(tree: str, root: Path) -> str:
    """codegraph's ``files`` tree with each module's responsibility appended.
    Depth is the drawing prefix (four chars a level), so the path is rebuilt
    from the tree itself; a file without a docstring stays as printed."""
    out, stack = [], []
    for line in tree.splitlines():
        if m := _TREE_LINE.match(line):
            del stack[len(m.group(1)) // 4:]
            stack.append(m.group(2))
            if note := responsibility(root.joinpath(*stack)):
                line = f"{line} — {note}"
        out.append(line)
    return "\n".join(out).strip("\n")


def named_files(body: str, root: Path) -> list[str]:
    """The files an issue names: every backticked token that is an existing
    file under ``root`` (relative, no ``..``), first mention first, deduped."""
    found = [tok for tok in re.findall(r"`([^`\n]+)`", body)
             if not tok.startswith("/") and ".." not in tok.split("/")
             and (root / tok).is_file()]
    return list(dict.fromkeys(found))


DEGRADED = """\
## Repo map — DEGRADED (codegraph unavailable: {reason})

No catalog or slice was spliced. Before editing, gather it yourself: the
package layout (the tree of source files), the public surface of every module
the issue touches (its top-level definitions and signatures), and their import
neighbours (what they import, who imports them)."""


def render_map(binary: str, root: Path, title: str, files: list[str]) -> str:
    """Both tiers from the CLI, or the degraded block — never an exception."""
    try:
        if (root / ".codegraph").is_dir():
            _codegraph(binary, root, "sync", ".")
        else:
            _codegraph(binary, root, "init", "-y", ".")
        catalog = annotate_catalog(_codegraph(binary, root, "files", "-p", "."), root)
        slices = [_codegraph(binary, root, "context", "-p", ".", "--no-code", title)]
        slices += [_codegraph(binary, root, "node", "-p", ".", "-f", f, "--symbols-only")
                   for f in files]
    except CodegraphUnavailable as e:
        return DEGRADED.format(reason=e)
    tier2 = "\n\n".join(s.strip("\n") for s in slices)
    return (f"## Repo map — tier 1: catalog\n\n{catalog}\n\n"
            f"## Repo map — tier 2: issue slice\n\n{tier2}")


STANDING_ORDERS = """\
## Standing orders — drill down with codegraph's CLI

The catalog and slice above are already spliced; do not re-derive them. Before
writing, look at what exists: `codegraph explore "<area>"` (an area's symbols
and call paths), `codegraph node <symbol>` / `codegraph node -f <file>` (one
symbol or file with its dependents), `codegraph impact <symbol>` and
`codegraph callers` / `codegraph callees <symbol>` (who is affected by a
change)."""


def body(text: str) -> str:
    """Everything below a doc's provenance header (``<!-- … -->``)."""
    _, close, rest = text.partition("-->")
    return (rest if close else text).strip("\n")


def compose(*, role: str, number: int, issue: dict, persona: str,
            constitution: list[str], repo_map: str, prime: str = "",
            trace: str = "") -> str:
    """The pack text. Persona, constitution and standing orders are the
    implementer's; the judge gets the contract and the map only."""
    parts = [f"# Dispatch pack — issue #{number} ({role})",
             f"## Issue\n\n{issue['title']}\n\n{issue['body'].strip()}"]
    if role == "implementer":
        parts.append("## Persona\n\n" + "\n\n".join([persona, *constitution]))
    parts.append(repo_map)
    if prime.strip():
        parts.append(f"## Prior lessons\n\n{prime.strip()}")
    if trace.strip():
        parts.append(f"## Run trace\n\n{trace.strip()}")
    if role == "implementer":
        parts.append(STANDING_ORDERS)
    return "\n\n".join(parts) + "\n"
