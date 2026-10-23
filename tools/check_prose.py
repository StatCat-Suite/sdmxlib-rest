"""Fail on the prose constructions CLAUDE.md's "Writing style" rules forbid (GH-55).

Ported from sdmxlib (StatCat-Suite/sdmxlib#84 and #85), as statcatlib and the
docs hub did; the phrase list is sdmxlib's, unchanged, since the style section
is shared.

Most of those rules need a reader: a scan of the tree found that about a third
of the "X, not Y" sentences break them and two-thirds correct a real misreading,
so no pattern can gate them. Three checks are exact, and this runs those:

1. **A Markdown heading ending in `?`.** A heading names its section's content.
   GH-84's first step renamed the 17 question headings under `docs/`.
2. **A fixed phrase list** (`PHRASES`): colon reveals and hook clauses with no
   legitimate use in this tree's prose, such as "the whole point" and "here's
   the". Two were rewritten when this landed.
3. **A bare issue reference in the docs site**: `#N` or `GH-N` in `docs/`. A
   reader of https://statcat-suite.github.io/sdmxlib-rest/ cannot tell a
   GitLab number from a GitHub one, so a page states the behaviour, or links
   the issue in full. A Markdown link is removed before the line is read, so a
   full link and a page anchor such as `(#middleware-order)` pass.

## What is read

Headings: the Markdown in `MARKDOWN`, outside code fences. Phrases: that
Markdown, and every Python, toml and justfile line under `CODE`. Issue
references: the Markdown under `USER_DOCS`, outside code fences. CHANGELOG.md
is left out as the historical record.

CLAUDE.md's own "Writing style" section quotes the phrases it bans, so lines
inside it are exempt. This file is exempt for the same reason.

## Usage

    uv run python tools/check_prose.py           # report
    uv run python tools/check_prose.py --check   # exit 1 if any
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Markdown read for both checks: a directory is read recursively.
MARKDOWN = ("docs", "README.md", "CLAUDE.md")
#: Trees and files whose lines are read for the phrase check.
CODE = ("src", "tests", "tools", "justfile", "pyproject.toml")
#: The docs site, read for bare issue references.
USER_DOCS = "docs"
#: The section of CLAUDE.md that states the rules, and so quotes what they ban.
RULES_FILE = "CLAUDE.md"
RULES_HEADING = "## Writing style"

#: Banned phrases, matched case-insensitively. Each is a colon reveal or a hook
#: clause from the Writing style rules, phrased narrowly enough that a match is
#: always a violation.
PHRASES = (
    r"\bhere'?s (the|what|why|how)\b",
    r"\bhere is the thing\b",
    r"\bwhy this matters\b",
    r"\bthe whole point\b",
    r"\bthe (real|actual) (issue|problem|question|story)\s*:",
    r"\bthe short version\b",
    r"\btl;?dr\b",
    r"\bthe part (nobody|no one)\b",
    r"\bnobody (mentions|talks about)\b",
    r"\b(less|more) than you'?d (think|expect)\b",
)
PHRASE_RE = re.compile("|".join(PHRASES), re.IGNORECASE)

#: An issue reference: GitLab's ``#N`` or GitHub's ``GH-N``, not inside a word or a path.
ISSUE_RE = re.compile(r"(?<![\w/#&])(?:#\d+|GH-\d+)\b")
#: A Markdown link or image, whose text and target may name an issue in full.
LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*(\{[^}]*\})?\s*$")
SELF = Path(__file__).resolve()


def _files(root: Path, names: tuple[str, ...], suffixes: tuple[str, ...]) -> list[Path]:
    """The files `names` holds: a directory's by suffix, a named file as is."""
    found: list[Path] = []
    for name in names:
        path = root / name
        if path.is_dir():
            found.extend(p for p in sorted(path.rglob("*")) if p.suffix in suffixes and p.is_file())
        elif path.is_file():
            found.append(path)
    return [p for p in found if p.resolve() != SELF]


def _markdown_lines(text: str) -> list[tuple[int, str]]:
    """Lines outside code fences, numbered from 1."""
    lines: list[tuple[int, str]] = []
    fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            fence = not fence
        elif not fence:
            lines.append((number, line))
    return lines


def _exempt_rules(rel: str, lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop the lines of CLAUDE.md's Writing style section."""
    if rel != RULES_FILE:
        return lines
    kept: list[tuple[int, str]] = []
    inside = False
    for number, line in lines:
        if line.startswith("## "):
            inside = line.strip() == RULES_HEADING
        if not inside:
            kept.append((number, line))
    return kept


def _is_user_doc(rel: str) -> bool:
    """Whether `rel` is a page of the docs site."""
    return rel.startswith(f"{USER_DOCS}/")


def find(root: Path) -> list[str]:
    """Every finding under `root`, as `path:line: message`.

    Args:
        root: The repository root.

    Returns:
        One line per finding, in file order.
    """
    findings: list[str] = []
    for path in _files(root, MARKDOWN, (".md",)):
        rel = path.relative_to(root).as_posix()
        lines = _exempt_rules(rel, _markdown_lines(path.read_text()))
        for number, line in lines:
            heading = HEADING_RE.match(line)
            if heading and heading.group(1).endswith("?"):
                findings.append(f"{rel}:{number}: heading is a question: {heading.group(1)!r}")
            if match := PHRASE_RE.search(line):
                findings.append(f"{rel}:{number}: banned phrase {match.group(0)!r}")
            if _is_user_doc(rel) and (match := ISSUE_RE.search(LINK_RE.sub("", line))):
                findings.append(f"{rel}:{number}: bare issue reference {match.group(0)!r}; state the fact or link it")
    for path in _files(root, CODE, (".py", ".toml")):
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if match := PHRASE_RE.search(line):
                findings.append(f"{rel}:{number}: banned phrase {match.group(0)!r}")
    return findings


def main() -> int:
    """Report findings; with `--check`, exit 1 when there are any.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 on any finding")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    findings = find(root)
    for finding in findings:
        print(finding)
    print(f"prose check: {len(findings)} finding(s)" if findings else "prose check: clean")
    return 1 if args.check and findings else 0


if __name__ == "__main__":
    sys.exit(main())
