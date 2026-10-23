"""Fail when a docstring drifts from plain Google style.

Ported from sdmxlib (sdmxlib#374), as part of the StatCat-Suite baseline.

## Why this exists, when `ruff.toml` already says `convention = "google"`

Because that setting is not the gate it looks like. Ruff's pydocstyle rules
check a docstring's **structure** — summary line, blank lines, punctuation —
and never read its markup or reject a NumPy section header. In the sibling
statcatlib project (then pinax) the same line sat declared and unenforced for
the life of the file while 98 Sphinx roles and 13 NumPy-sectioned docstrings
accumulated underneath it.

sdmxlib-rest carried 61 Sphinx roles in `src/` when this was ported; they were
rewritten as plain backticks in the same change, so every arm starts at zero
and stays there as a regression guard.

## The three arms

1. **No Sphinx/RST roles.** A grep, because no tool in the ecosystem models
   this: pydoclint and griffe both parse ``:class:`X` `` as ordinary prose,
   correctly, since banning a markup dialect is house policy rather than a
   correctness property.
2. **No NumPy section underlines** — matched as a section *name* followed by a
   rule, never a bare rule, so a prose heading underlined RST-style is not a
   finding (sdmxlib has three; a naive ``^-{3,}$`` grep flags them).
3. **pydoclint `DOC003`** — a docstring whose shape is not Google even without
   an underline. Off by default upstream; needs `--check-style-mismatch`.

Only DOC003 is consulted. pydoclint's other codes (missing ``Args``,
``Returns`` and the like) are a separate question this gate takes no position
on.

## Usage

    uv run python tools/check_docstrings.py           # report
    uv run python tools/check_docstrings.py --check   # exit 1 if any
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Checked as a whole. Tests and benchmarks carry docstrings that are prose for
#: a reader rather than API documentation, and the convention this enforces is
#: about the published surface.
TARGET = "src"
#: Every tree checked. One that does not exist under the root is skipped.
TARGETS = (TARGET,)

#: Sphinx/RST cross-reference roles. The list is the dialect, not the subset
#: seen so far — a guard that only bans what already appeared bans nothing.
_ROLE_NAMES = (
    "class",
    "meth",
    "func",
    "mod",
    "attr",
    "obj",
    "exc",
    "data",
    "ref",
    "doc",
    "term",
    "const",
    "keyword",
    "envvar",
    "option",
)
ROLE_RE = re.compile(rf":(?:py:)?(?:{'|'.join(_ROLE_NAMES)}):`")

#: ``Example::`` and friends — the RST literal-block marker, which Google style
#: spells as an ``Example:`` section with an indented body.
LITERAL_BLOCK_RE = re.compile(r"^\s*(Example|Examples|Usage|Note|Warning)::\s*$")

#: NumPy section names. A line holding one of these *followed by a rule* is a
#: NumPy section; a rule under anything else is a prose heading and fine.
_SECTIONS = (
    "Parameters",
    "Other Parameters",
    "Returns",
    "Yields",
    "Receives",
    "Raises",
    "Warns",
    "Warnings",
    "See Also",
    "Notes",
    "References",
    "Examples",
    "Attributes",
    "Methods",
)
SECTION_RE = re.compile(rf"^\s*(?:{'|'.join(_SECTIONS)})\s*$")
RULE_RE = re.compile(r"^\s*[-=~^]{3,}\s*$")

Finding = tuple[str, int, str]


def _targets(root: Path) -> list[str]:
    return [target for target in TARGETS if (root / target).is_dir()]


def _python_files(root: Path) -> list[Path]:
    files = (p for target in _targets(root) for p in (root / target).rglob("*.py"))
    return sorted(p for p in files if "__pycache__" not in p.parts)


def rst_markup(root: Path) -> list[Finding]:
    """Every Sphinx role and RST literal-block marker under `TARGET`."""
    found: list[Finding] = []
    for path in _python_files(root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ROLE_RE.search(line):
                found.append((str(path.relative_to(root)), number, f"Sphinx role: {line.strip()}"))
            elif LITERAL_BLOCK_RE.match(line):
                found.append((str(path.relative_to(root)), number, f"RST literal block: {line.strip()}"))
    return found


def numpy_sections(root: Path) -> list[Finding]:
    """Every NumPy section header — a section *name* underlined by a rule.

    Deliberately not every underlined line: a prose heading under an RST rule
    is a perfectly good Google-style docstring, and a rule-only match calls it
    a NumPy section — the false positive sdmxlib#374 warned about by name.
    """
    found: list[Finding] = []
    for path in _python_files(root):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines[:-1], start=1):
            if SECTION_RE.match(line) and RULE_RE.match(lines[number]):
                found.append((str(path.relative_to(root)), number, f"NumPy section: {line.strip()!r} underlined"))
    return found


def style_mismatches(root: Path) -> list[Finding]:
    """Pydoclint's DOC003 — a docstring that is not Google style.

    Every other code is discarded. pydoclint exits non-zero whenever it reports
    anything, which here is the normal case, so only a crash counts as failure.
    """
    proc = subprocess.run(  # noqa: S603
        [
            "uv",
            "run",
            "pydoclint",
            "--style=google",
            "--check-style-mismatch=True",
            "--arg-type-hints-in-docstring=False",
            "--quiet",
            *_targets(root),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(f"pydoclint failed to run:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}\n")
        raise SystemExit(2)

    # **stderr, not stdout.** pydoclint 0.9 writes its findings to stderr and
    # leaves stdout empty, so a parser reading stdout reports a clean tree
    # forever. Caught by sabotaging the arm rather than by reading the docs —
    # which is the argument for sabotaging every arm of a gate that starts at
    # zero, since "no findings" is what it says when it works *and* when it
    # does not.
    found: list[Finding] = []
    current = ""
    for raw in (proc.stderr + proc.stdout).splitlines():
        line = raw.rstrip()
        if line.endswith(".py"):
            current = line
        elif "DOC003" in line:
            number, _, message = line.strip().partition(":")
            found.append((current, int(number) if number.isdigit() else 0, message.strip() or line.strip()))
    return found


ARMS = (
    ("Sphinx/RST markup", rst_markup),
    ("NumPy section headers", numpy_sections),
    ("pydoclint DOC003 (not Google style)", style_mismatches),
)


def main() -> int:
    """Run every arm and report; with --check, exit 1 on any finding."""
    parser = argparse.ArgumentParser(description="Docstring convention gate")
    parser.add_argument("--check", action="store_true", help="exit 1 when any finding is present")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    total = 0
    for label, arm in ARMS:
        found = arm(root)
        total += len(found)
        if not found:
            print(f"  ok    {label}")
            continue
        print(f"  FAIL  {label} — {len(found)}")
        for file, line, message in found:
            print(f"          {file}:{line}  {message}")

    if not total:
        print(f"{', '.join(f'{target}/' for target in _targets(root))}: docstrings are plain Google style")
        return 0
    if args.check:
        print(
            f"\n{total} finding(s). The convention is pure Google-style "
            f"docstrings — no RST roles, no NumPy sections (CLAUDE.md, Style)."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
