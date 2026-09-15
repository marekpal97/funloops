"""The three-form path matcher shared by triage and the diff gate."""

from __future__ import annotations

import fnmatch


def match(path: str, pattern: str) -> bool:
    """Match one repo-relative path against one pattern, by the pattern's shape.

      - dir prefix — trailing ``/`` (``hooks/``): the path starts with it.
      - glob — contains ``*``/``?``/``[`` (``*schema*``): fnmatched
        case-insensitively against the basename, or the whole path when the
        glob spans directories.
      - bare filename — anything else (``ontology.yaml``): the basename
        equals it, at any depth.
    """
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        target = path if "/" in pattern else path.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(target.lower(), pattern.lower())
    return path.rsplit("/", 1)[-1] == pattern


def hits(files: list[str], patterns: list[str]) -> list[str]:
    """``"<path> (matches <pattern>)"`` for each file that hits any pattern,
    deduped and sorted."""
    found = set()
    for path in files:
        for pat in patterns:
            if match(path, pat):
                found.add(f"{path} (matches {pat})")
    return sorted(found)
