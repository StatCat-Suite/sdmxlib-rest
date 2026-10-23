"""Count `Any` in `src/` and fail if it moves off the recorded baseline.

Ported from sdmxlib (sdmxlib#284), as part of the StatCat-Suite baseline.

## Why this exists

`ANN401` refuses a *bare* ``Any`` annotation. It does not look inside generic
arguments::

    def f(x: Any) -> None: ...  # ANN401 finding
    def f(x: dict[str, Any]) -> None: ...  # silent

In sdmxlib an audit found 112 of 171 occurrences were the second form, so the
rule as enforced covered roughly 4 of 171 sites. Here, with ANN401 on, every
one of the baseline's occurrences is the second form.

No linter in the stack has a rule for this, and writing one is out of
proportion to the problem. A ratchet is the technique the repo already uses
elsewhere — pyrefly's deny-list carries the measured count that justifies each
entry — and it converts "we intend to reduce this" into something that cannot
quietly reverse.

## What it counts

Occurrences of the bare name ``Any`` used as a *type*, per file:

- Comments and docstrings are excluded. Eighteen of the 171 were prose, and a
  count that moves when someone rewords a docstring is noise.
- ``"Any"`` as an *element* of a tuple, list, set or dict literal is excluded:
  there it is data — SDMX's own class name, `base.Any` (sdmxlib#430) — never the
  type. A string annotation, quoted or not, and ``cast("Any", …)`` still count.
- ``AnyRef``, ``AnyAncestorMatches`` and friends are excluded — matching is on
  the whole word.
- ``from typing import Any`` is excluded: an import is a consequence of a use,
  not a use, and counting it double-counts the last site in a file.
- **Aliases are followed.** A module-level ``X = Something[Any]`` hides an
  ``Any`` behind a name, and uses of ``X`` are then invisible to a text scan.
  ``AnyRef = Ref[Any]`` had 74 such uses against 15 spelled ``Ref[Any]`` — so
  the gate was seeing a sixth of the truth, and "standardise on the alias"
  would have dropped the count by 15 while changing nothing (sdmxlib#287). Aliases
  are discovered across all of ``src/``, so one defined in `model/ref.py`
  still counts where it is used in a writer.

This is a text scan rather than an AST walk on purpose. It has to agree with
what a reader greps for, and an AST walk would disagree about string
annotations, which this codebase writes as quoted forward references.

## Usage

    python tools/count_any.py            # report the breakdown
    python tools/count_any.py --check    # fail if above baseline
    python tools/count_any.py --write    # record the current count as baseline
"""

import argparse
import ast
import io
import sys
import tokenize
from collections.abc import Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_BASELINE = _ROOT / "tools" / "any_baseline.txt"

#: Import lines are a consequence of a use, not a use.
_IMPORT_PREFIXES = ("import ", "from ")


def _any_values(tree: ast.AST) -> set[tuple[int, int]]:
    """Where ``"Any"`` is a *value*: an element of a tuple, list, set or dict.

    SDMX defines a class named Any (`base.Any`), and sdmxlib#430's class table names
    it. Only a collection element is excluded, because there a string is data:
    a bare ``x: "Any"`` annotation, or ``cast("Any", y)``, is still the type and
    still counts — the exclusion fails closed everywhere else.
    """
    found: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            continue
        elements = [*node.keys, *node.values] if isinstance(node, ast.Dict) else node.elts
        for element in elements:
            if isinstance(element, ast.Constant) and element.value == "Any":
                found.add((element.lineno, element.col_offset))
    return found


def _blank_tokens(blanked: list[str], tokens: list[tokenize.TokenInfo], values: set[tuple[int, int]]) -> None:
    """Blank comments, and the ``"Any"`` collection elements `_any_values` found."""
    for tok in tokens:
        row = tok.start[0] - 1
        if tok.type is tokenize.COMMENT:
            blanked[row] = blanked[row][: tok.start[1]]
        elif tok.type is tokenize.STRING and tok.start in values:
            line = blanked[row]
            blanked[row] = line[: tok.start[1]] + " " * (tok.end[1] - tok.start[1]) + line[tok.end[1] :]


def _code_lines(text: str) -> list[str]:
    """Return *text* with comments and docstrings blanked out.

    Tokenising rather than regexing because a ``#`` inside a string literal is
    not a comment, and this file's whole point is to be trusted.
    """
    lines = text.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenError, IndentationError, SyntaxError:
        return lines
    try:
        values = _any_values(ast.parse(text))
    except SyntaxError:
        values = set()
    _blank_tokens(blanked, tokens, values)
    # Docstrings are string *expressions*; blank them via the AST, which knows
    # which strings are statements and which are values.
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return blanked
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first: object = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            for row in range(first.lineno - 1, (first.end_lineno or first.lineno)):
                blanked[row] = ""
    return blanked


def alias_names(root: Path | None = None) -> set[str]:
    """Module-level names bound to a type that mentions ``Any``.

    ``AnyRef = Ref[Any]`` was one, until sdmxlib#287 deleted it in favour of spelling
    ``Ref[Any]`` out. Without this an alias is a hole in the gate: a name can
    carry an ``Any`` past a text scan, and the more the alias is preferred the
    less the count means. Kept — and tested — because the next alias would
    reopen the hole silently.

    Args:
        root: Tree to scan. Defaults to ``src/``; overridden in tests.
    """
    names: set[str] = set()
    for path in sorted((root or _SRC).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            targets: Sequence[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.TypeAlias):
                targets, value = [node.name], node.value
            # Only type *expressions*: a subscript (`Ref[Any]`), a bare name,
            # or a union. A dict or list literal that merely happens to
            # mention `AnyAncestorMatches`, or an `__all__` containing the
            # string "AnyRef", is not an alias.
            if value is None or not isinstance(value, (ast.Subscript, ast.Name, ast.BinOp, ast.Attribute)):
                continue
            if not _count_word(ast.unparse(value), "Any"):
                continue
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _count_word(line: str, word: str) -> int:
    """Whole-word occurrences of *word* in *line*."""
    total = 0
    n = len(word)
    for i in range(len(line) - n + 1):
        if line[i : i + n] != word:
            continue
        before = line[i - 1] if i else " "
        after = line[i + n] if i + n < len(line) else " "
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            total += 1
    return total


def count_file(path: Path, aliases: frozenset[str] = frozenset()) -> int:
    """Occurrences of the bare type name ``Any`` in *path*, excluding prose."""
    total = 0
    for line in _code_lines(path.read_text(encoding="utf-8")):
        stripped = line.strip()
        if stripped.startswith(_IMPORT_PREFIXES):
            continue
        # Whole-word match: `AnyRef` and `AnyAncestorMatches` are not `Any`.
        total += _count_word(line, "Any")
        # ...but a name *bound* to a type containing `Any` is one, wherever
        # it is used. The alias definition itself already counted its `Any`.
        for alias in aliases:
            if stripped.startswith((f"{alias} =", f"{alias}:")):
                continue
            total += _count_word(line, alias)
    return total


def survey() -> dict[str, int]:
    """Per-file counts, non-zero only, sorted by count descending."""
    aliases = frozenset(alias_names())
    counts = {str(p.relative_to(_ROOT)): n for p in sorted(_SRC.rglob("*.py")) if (n := count_file(p, aliases))}
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _read_baseline() -> int | None:
    if not _BASELINE.exists():
        return None
    for line in _BASELINE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return int(stripped)
    return None


def main() -> int:
    """Report, check or write the baseline, per the flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if above the baseline")
    parser.add_argument("--write", action="store_true", help="record the current count as the baseline")
    args = parser.parse_args()

    counts = survey()
    total = sum(counts.values())

    if args.write:
        _BASELINE.write_text(
            "# Occurrences of the type `Any` in src/, excluding comments,\n"
            "# docstrings and imports. Lower it as they are typed; raising it\n"
            "# needs a reason in the commit message. See tools/count_any.py.\n"
            f"{total}\n",
            encoding="utf-8",
        )
        print(f"baseline written: {total}")
        return 0

    baseline = _read_baseline()
    if not args.check:
        for name, n in counts.items():
            print(f"{n:5d}  {name}")
        print(f"{total:5d}  TOTAL" + (f"  (baseline {baseline})" if baseline is not None else ""))
        return 0

    if baseline is None:
        print("error: no baseline recorded; run `python tools/count_any.py --write`", file=sys.stderr)
        return 1
    if total > baseline:
        print(
            f"error: `Any` in src/ rose to {total}, above the baseline of {baseline}.\n"
            f"       ANN401 does not see inside generic arguments, so this ratchet is\n"
            f"       the only thing guarding `dict[str, Any]` and friends (sdmxlib#284).\n"
            f"       Fix the new one, or lower it deliberately with --write.\n",
            file=sys.stderr,
        )
        for name, n in counts.items():
            print(f"{n:5d}  {name}", file=sys.stderr)
        return 1
    if total < baseline:
        print(
            f"error: `Any` in src/ fell to {total}, below the baseline of {baseline}.\n"
            f"       Good — now record it: `python tools/count_any.py --write`.\n"
            f"       A ratchet that is not tightened stops ratcheting.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
