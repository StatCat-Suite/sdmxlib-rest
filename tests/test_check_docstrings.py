"""Tests for `tools/check_docstrings.py` — the docstring convention gate (sdmxlib#374).

A gate tool needs tests for the reason `test_count_any.py` gives: when it
breaks, it breaks **open**. A check that finds nothing reports the same green
whether the tree is clean or the check is broken, and nobody investigates a
passing gate.

That is not hypothetical: in sdmxlib the pydoclint arm shipped reading `proc.stdout`,
which is empty — pydoclint 0.9 writes findings to stderr — so it reported a
clean tree on a file it had just flagged. It was caught by sabotaging the arm,
and the cases below are the standing version of that sabotage.

Two things get equal weight: that each arm **catches** its own violation, and
that the NumPy arm does **not** fire on a prose heading. sdmxlib#374 predicted the
second by name, because sdmxlib's `_dto.py` and `metadata_annotation.py` head prose with
an RST rule and a naive `^-{3,}$` grep calls all three NumPy sections.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# `tools/` is not a package, so the tool is loaded by path.
_TOOL = ROOT / "tools" / "check_docstrings.py"
_spec = importlib.util.spec_from_file_location("check_docstrings", _TOOL)
assert _spec is not None
assert _spec.loader is not None
check_docstrings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docstrings)

pytestmark = pytest.mark.unit


def _tree(tmp_path: Path, body: str) -> Path:
    """A throwaway root holding `src/probe.py`, so the arms can scan it."""
    src = tmp_path / check_docstrings.TARGET / "pkg"
    src.mkdir(parents=True)
    (src / "probe.py").write_text(body, encoding="utf-8")
    return tmp_path


# ── arm 1: Sphinx / RST markup ────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [":class:`Codelist`", ":meth:`get`", ":func:`parse`", ":exc:`ValueError`", ":py:attr:`id`", ":ref:`guide`"],
)
def test_a_sphinx_role_is_found(tmp_path: Path, role: str) -> None:
    """The list is the dialect, not the subset seen so far.

    A guard that only bans what already appeared bans nothing — `:exc:`,
    `:ref:` and the `py:`-prefixed spellings had never occurred here, which is
    exactly why they are the ones that would slip in.
    """
    found = check_docstrings.rst_markup(_tree(tmp_path, f'"""Summary naming {role} the RST way."""\n'))
    assert len(found) == 1
    assert role in found[0][2]


def test_an_rst_literal_block_is_found(tmp_path: Path) -> None:
    body = '"""Summary.\n\n    Example::\n\n        x = 1\n    """\n'
    found = check_docstrings.rst_markup(_tree(tmp_path, body))
    assert [f[2] for f in found] == ["RST literal block: Example::"]


def test_a_google_example_section_is_not_markup(tmp_path: Path) -> None:
    """One colon is the Google spelling and must stay legal."""
    body = '"""Summary.\n\n    Example:\n        x = 1\n    """\n'
    assert check_docstrings.rst_markup(_tree(tmp_path, body)) == []


# ── arm 2: NumPy sections, and the false positive it must avoid ───────────


@pytest.mark.parametrize("section", ["Parameters", "Returns", "Raises", "Attributes", "See Also", "Notes"])
def test_a_numpy_section_is_found(tmp_path: Path, section: str) -> None:
    body = f'"""Summary.\n\n    {section}\n    {"-" * len(section)}\n    x : int\n        A thing.\n    """\n'
    found = check_docstrings.numpy_sections(_tree(tmp_path, body))
    assert len(found) == 1, f"{section} underlined is a NumPy section"
    assert section in found[0][2]


@pytest.mark.parametrize(
    "heading",
    ["Conventions for wire Structs — read before adding one", "Why a *projection*, not persisted DSD content"],
)
def test_a_prose_heading_is_not_a_numpy_section(tmp_path: Path, heading: str) -> None:
    """The false positive sdmxlib#374 named, taken verbatim from the two files it named.

    sdmxlib's `_dto.py` and `metadata_annotation.py` head prose with an RST rule. Matching
    a bare rule calls those NumPy sections and fails a clean tree, which is the
    one outcome worse than not checking at all.
    """
    body = f'"""Summary.\n\n{heading}\n{"-" * len(heading)}\nBecause.\n"""\n'
    assert check_docstrings.numpy_sections(_tree(tmp_path, body)) == []


def test_a_google_section_is_not_a_numpy_section(tmp_path: Path) -> None:
    """`Returns:` with a colon and no rule is the Google spelling."""
    body = '"""Summary.\n\n    Returns:\n        int: A thing.\n    """\n'
    assert check_docstrings.numpy_sections(_tree(tmp_path, body)) == []


def test_a_rule_on_the_last_line_does_not_run_off_the_end(tmp_path: Path) -> None:
    """The arm looks one line ahead; a section name as the final line must not raise."""
    assert check_docstrings.numpy_sections(_tree(tmp_path, "# Returns\nReturns")) == []


# ── the arms are wired in ─────────────────────────────────────────────────


def test_every_arm_is_registered() -> None:
    """`ARMS` is what `main` iterates, so an arm not in it does not run.

    The failure mode this catches is silent: adding a check function and
    forgetting the tuple leaves a green gate that never calls it.
    """
    registered = {arm for _, arm in check_docstrings.ARMS}
    assert registered == {
        check_docstrings.rst_markup,
        check_docstrings.numpy_sections,
        check_docstrings.style_mismatches,
    }


def test_the_pydoclint_arm_reads_the_stream_pydoclint_writes_to() -> None:
    """pydoclint 0.9 writes findings to **stderr** and leaves stdout empty.

    Asserted against the source rather than by running pydoclint, because the
    bug this guards is a parser reading the wrong stream — which produces no
    findings, which is indistinguishable from a clean tree in any end-to-end
    check. If a later pydoclint moves to stdout, this fails and says where to
    look.
    """
    source = _TOOL.read_text(encoding="utf-8")
    assert "proc.stderr" in source, "the arm must consult stderr, where pydoclint writes"
