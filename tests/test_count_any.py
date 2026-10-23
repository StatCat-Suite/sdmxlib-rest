"""Tests for `tools/count_any.py` — the ratchet guarding generic-argument `Any` (sdmxlib#284).

A gate tool needs tests for a reason that does not apply to ordinary code: when
it breaks, it breaks *open*. A counter that silently undercounts reports a
green check forever, and nobody investigates a passing gate. So the cases below
are mostly about what must **not** be counted — every false exclusion is a hole
the ratchet stops guarding.

The counter is a text scan rather than an AST walk on purpose: it has to agree
with what a reader greps for, and this codebase uses string annotations freely
under `from __future__ import annotations`, which an AST walk would see
differently.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# `tools/` is not a package, so the tool is loaded by path.
ROOT = Path(__file__).resolve().parent.parent
_TOOL = ROOT / "tools" / "count_any.py"
_spec = importlib.util.spec_from_file_location("count_any", _TOOL)
assert _spec is not None
assert _spec.loader is not None
count_any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(count_any)
alias_names, count_file, survey = count_any.alias_names, count_any.count_file, count_any.survey

pytestmark = pytest.mark.unit

_ROOT = ROOT


def _count(tmp_path: Path, source: str, aliases: set[str] | None = None) -> int:
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    count: int = count_file(path, frozenset(aliases or ()))
    return count


# ── What must be counted ─────────────────────────────────────────────────────


def test_counts_a_bare_annotation(tmp_path: Path) -> None:
    assert _count(tmp_path, "def f(x: Any) -> None: ...\n") == 1


def test_counts_inside_a_generic_argument(tmp_path: Path) -> None:
    """The whole reason this tool exists — `ANN401` cannot see this one."""
    assert _count(tmp_path, "def f(x: dict[str, Any]) -> None: ...\n") == 1


def test_counts_each_occurrence_on_a_line(tmp_path: Path) -> None:
    """Per-occurrence, not per-line: the number must move when one site is fixed."""
    assert _count(tmp_path, "def f(x: dict[Any, Any]) -> None: ...\n") == 2


def test_counts_a_string_annotation(tmp_path: Path) -> None:
    """`from __future__ import annotations` makes these ordinary here."""
    assert _count(tmp_path, 'def f(x: "list[Any]") -> None: ...\n') == 1


def test_counts_a_return_type(tmp_path: Path) -> None:
    assert _count(tmp_path, "def f() -> Any: ...\n") == 1


# ── What must not be counted ─────────────────────────────────────────────────


def test_ignores_comments(tmp_path: Path) -> None:
    """Eighteen of the original 171 were prose; a count that moves when someone
    rewords a docstring is noise, and noise is what gets filtered out."""
    assert _count(tmp_path, "# this mentions Any in prose\nx = 1\n") == 0


def test_ignores_docstrings(tmp_path: Path) -> None:
    assert _count(tmp_path, 'def f() -> None:\n    """Talks about Any at length."""\n') == 0


def test_ignores_module_docstrings(tmp_path: Path) -> None:
    assert _count(tmp_path, '"""Module about Any."""\n\nx = 1\n') == 0


def test_ignores_imports(tmp_path: Path) -> None:
    """An import is a consequence of a use, not a use.

    Counting it double-counts the last remaining site in a file, so removing
    that site would drop the number by two and the ratchet's arithmetic would
    stop matching what a reader sees.
    """
    assert _count(tmp_path, "from typing import Any\nx = 1\n") == 0


def test_ignores_names_that_merely_start_with_any(tmp_path: Path) -> None:
    """`Ref[Any]` is a real alias in this codebase and is not `Any`."""
    source = "def f(x: AnyRef, y: AnyAncestorMatches, z: MyAny) -> None: ...\n"
    assert _count(tmp_path, source) == 0


def test_known_limitation_counts_any_inside_a_string_value(tmp_path: Path) -> None:
    """A message string saying "Any" is counted. Recorded, not fixed.

    The counter cannot distinguish a string *annotation* — which must count,
    and which this codebase has (`"Callable[[etree._Element, Any], None]"`) —
    from a string *value*, without the AST walk the module docstring explains
    it deliberately avoids.

    Left as-is because the error runs in the safe direction: over-counting can
    only cause a false *failure*, never a false pass. A gate that fails closed
    is annoying; one that fails open is worthless, and nobody investigates a
    passing check. `src/` has no such string today — verified when this was
    written — so the cost is currently zero.
    """
    assert _count(tmp_path, 'MSG = "expected Any here"\n') == 1


def test_survives_a_file_it_cannot_parse(tmp_path: Path) -> None:
    """A syntax error must not crash the gate — it should degrade, not explode."""
    path = tmp_path / "broken.py"
    path.write_text("def f(x: Any -> None\n", encoding="utf-8")
    assert count_file(path) >= 0


# ── The gate itself ──────────────────────────────────────────────────────────


def test_baseline_matches_the_tree() -> None:
    """`just check-any` is green, so the committed baseline must be current.

    This duplicates the gate on purpose: the gate runs in `just check`, but a
    stale baseline committed alongside a passing local run is exactly the drift
    the ratchet is meant to prevent.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_ROOT / "tools" / "count_any.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_survey_reports_real_files() -> None:
    """Sanity: the survey addresses `src/` and returns plausible paths."""
    counts = survey()
    assert counts, "survey found nothing — the tool is looking in the wrong place"
    assert all(name.startswith("src/sdmxlib_rest/") for name in counts)
    assert all(n > 0 for n in counts.values())


# ── Alias following (sdmxlib#287) ───────────────────────────────────────────────────


def test_follows_an_alias_bound_to_a_type_containing_any(tmp_path: Path) -> None:
    """`Ref[Any] = Ref[Any]` had 74 uses the gate could not see.

    A name can carry an `Any` past a text scan, and the more the alias is
    preferred over the spelled-out form the less the count means. Worse, it
    made "standardise on the alias" look like a 15-point improvement while
    changing nothing at all.
    """
    assert _count(tmp_path, "def f(x: MyAlias) -> None: ...\n", aliases={"MyAlias"}) == 1


def test_does_not_double_count_the_alias_definition(tmp_path: Path) -> None:
    """The definition's own `Any` is already counted as an `Any`."""
    assert _count(tmp_path, "MyAlias = Ref[Any]\n", aliases={"MyAlias"}) == 1


def test_alias_detection_finds_a_type_alias(tmp_path: Path) -> None:
    """The mechanism, on a synthetic tree.

    Tested against a fixture rather than the real tree because sdmxlib#287 deleted
    the only alias `src/` had. The mechanism still has to work: the next one
    would reopen the hole silently.
    """
    (tmp_path / "m.py").write_text("MyAlias = Ref[Any]\n", encoding="utf-8")
    assert alias_names(tmp_path) == {"MyAlias"}


def test_alias_detection_ignores_containers_that_merely_mention_any(tmp_path: Path) -> None:
    """A dict of emitters and an `__all__` list are not type aliases.

    Both shapes exist in this codebase and both matched a first, looser
    version of the detector — `_EMITTERS = {AnyAncestorMatches: ...}` and
    `__all__ = ["AnyRef"]` — however much their text says "Any".
    """
    (tmp_path / "m.py").write_text(
        '_EMITTERS = {AnyAncestorMatches: _emit}\n__all__ = ["AnyRef"]\n',
        encoding="utf-8",
    )
    assert alias_names(tmp_path) == set()


def test_src_has_no_any_bearing_aliases() -> None:
    """No name in `src/` hides an `Any` any more.

    Two did. `AnyRef = Ref[Any]` was a bare synonym whose name concealed 74
    uses from the gate (sdmxlib#287). `MapEndpoint = AnyRef | MapDataType` was an
    alias *of* that alias, hidden twice over — and it stopped carrying an
    `Any` at all once sdmxlib#289 made `Ref` covariant and it became
    `Ref[object] | MapDataType`.

    A new alias here is not forbidden, but it changes what the ratchet's
    number means, so it should be a deliberate act rather than a surprise.
    """
    assert alias_names() == set()


def test_ignores_any_as_an_element_of_a_collection_literal(tmp_path: Path) -> None:
    """SDMX defines a class named Any (`base.Any`), and sdmxlib#430's class table names
    it as data. A collection element is never a type."""
    source = 'PAIRS = frozenset({("base", "Any"), ("base", "Agency")})\nNAMES = ["Any"]\n'
    assert _count(tmp_path, source) == 0


@pytest.mark.parametrize(
    "source",
    ['def f(x: "Any") -> None: ...\n', 'y = cast("Any", x)\n', 'X: TypeAlias = "Any"\n'],
)
def test_still_counts_a_bare_quoted_any_that_is_a_type(tmp_path: Path, source: str) -> None:
    """The exclusion is for collection elements only, so it fails closed: a
    quoted ``"Any"`` anywhere a type goes is still a use."""
    assert _count(tmp_path, source) == 1
