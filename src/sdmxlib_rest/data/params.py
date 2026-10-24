"""Query-parameter parsing for the SDMX-REST data plane.

Everything here is driven by the request's query string plus a
`sdmxlib.DataflowBinding` — no catalog-specific knowledge. A consumer
wires these into its own ``/data`` handler.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

if TYPE_CHECKING:
    import sdmxlib as sl

# SDMX 2.1 / 3.0 identifier patterns (from SDMXCommonReferences.xsd). Applied
# to Path() so malformed refs fail 422 at the framework boundary instead of
# becoming a ValueError inside sdmxlib's URN parser.
AGENCY_PATTERN = r"^[A-Za-z0-9_@$-]+(\.[A-Za-z0-9_@$-]+)*$"
ID_PATTERN = r"^[A-Za-z0-9_@$-]+$"
VERSION_PATTERN = r"^[0-9]+(\.[0-9]+)*$"

# Why: SDMX-REST 2.2.2 uses ``c[<id>]=<expr>`` for component-level filters.
# FastAPI's Query parameters can't model the bracketed form, so callers
# extract them from the request's query_params multidict.
C_PARAM = re.compile(r"^c\[([^\]]+)\]$")

# SDMX-REST 2.2.2 `attributes=` / `measures=` keywords that map to "keep all".
# `dsd` is the default for `attributes`; `all` is the default for `measures`.
# `dataset` / `series` / `obs` filter by attachment level — attachment levels
# aren't modelled, so they're accepted and treated as `all`.
KEEP_ALL_ATTRIBUTE_KEYWORDS = frozenset({"all", "dsd", "dataset", "series", "obs", "observations"})
KEEP_ALL_MEASURE_KEYWORDS = frozenset({"all"})

# `attributes=msd` selects observations attached to referential metadata via a
# MetadataStructure. Consumers whose bundle carries no MSD attachments can
# short-circuit to a header-only CSV and skip the observation scan.
DROP_ALL_ATTRIBUTE_KEYWORDS = frozenset({"msd"})

# Operators defined by the SDMX-REST 2.2.2 `c[...]=` syntax. Each `+`-joined
# term may carry an optional `<op>:` prefix; the default is `eq`.
C_OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "co", "nc", "sw", "ew"})

# SQL templates per operator. ``{col}`` is substituted with the quoted physical
# column; ``?`` is the bind parameter. ``eq`` is the IN-list operator handled
# via ComponentFilter and isn't in this map.
#
# Comparison ops (lt/le/gt/ge) emit straight SQL operators — for VARCHAR
# columns DuckDB does lexicographic compare, which matches the SDMX-REST
# spec: "the sorting within a component is based on the code IDs or the
# non-coded component values."
NON_EQ_OPERATOR_SQL = {
    "ne": "{col} <> ?",
    "lt": "{col} < ?",
    "le": "{col} <= ?",
    "gt": "{col} > ?",
    "ge": "{col} >= ?",
    "co": "{col} LIKE ? ESCAPE '\\'",
    "nc": "{col} NOT LIKE ? ESCAPE '\\'",
    "sw": "{col} LIKE ? ESCAPE '\\'",
    "ew": "{col} LIKE ? ESCAPE '\\'",
}
LIKE_OPS = frozenset({"co", "nc", "sw", "ew"})


def sql_ident(name: str) -> str:
    """Quote a SQL identifier. Source is producer-controlled binding metadata."""
    if not name.replace("_", "").isalnum():
        msg = f"refusing to quote suspicious identifier: {name!r}"
        raise ValueError(msg)
    return f'"{name}"'


def parse_bracketed(
    items: Iterable[tuple[str, str]],
    pattern: re.Pattern[str],
) -> dict[str, list[str]]:
    """Extract bracketed query params (``c[<id>]=…`` / ``within[<id>]=…``).

    Repeated keys (``foo[X]=A&foo[X]=B``) and ``+``-joined values
    (``foo[X]=A+B``) are both accepted and merged into one value list.
    Non-matching items are ignored.
    """
    out: dict[str, list[str]] = {}
    for raw_key, raw_value in items:
        m = pattern.match(raw_key)
        if m is None:
            continue
        out.setdefault(m.group(1), []).extend(raw_value.split("+"))
    return out


def parse_c_params(
    items: Iterable[tuple[str, str]],
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]]]:
    """Parse ``c[<id>]=<expr>`` filters per SDMX-REST 2.2.2.

    Each ``<expr>`` is a ``+``-joined list of terms; each term may carry an
    optional ``<op>:`` prefix where ``<op>`` is one of ``eq``/``ne``/``lt``/
    ``le``/``gt``/``ge``/``co``/``nc``/``sw``/``ew``. Default (no prefix) is
    ``eq``. The ``+`` separator is interpreted as a per-component AND across
    operator terms — matches the spec example
    ``c[TIME_PERIOD]=ge:2020-01+le:2020-12``.

    Returns ``(eq_values, non_eq_terms)``:

    - ``eq_values[dim_id]`` is the list of values matched as equals (multi-value
      collapses to ``IN (…)`` via sdmxlib's ``ComponentFilter``).
    - ``non_eq_terms[dim_id]`` is a list of ``(op, value)`` tuples for the nine
      remaining operators; the caller renders them as SQL fragments.

    Args:
        items: Output of ``request.query_params.multi_items()``.
    """
    eq_values: dict[str, list[str]] = {}
    non_eq_terms: dict[str, list[tuple[str, str]]] = {}
    for raw_key, raw_value in items:
        m = C_PARAM.match(raw_key)
        if m is None:
            continue
        dim_id = m.group(1)
        for term in raw_value.split("+"):
            op, sep, val = term.partition(":")
            if sep and op in C_OPERATORS:
                if op == "eq":
                    eq_values.setdefault(dim_id, []).append(val)
                else:
                    non_eq_terms.setdefault(dim_id, []).append((op, val))
            else:
                # No recognised operator prefix: implicit eq on the whole term.
                # Covers SDMX values that legitimately contain `:` (e.g. timestamps).
                eq_values.setdefault(dim_id, []).append(term)
    return eq_values, non_eq_terms


def like_pattern(op: str, raw: str) -> str:
    """Wrap a user value in LIKE wildcards per operator semantics, escaping LIKE metachars."""
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if op in {"co", "nc"}:
        return f"%{escaped}%"
    if op == "sw":
        return f"{escaped}%"
    return f"%{escaped}"


def build_non_eq_where(
    non_eq_terms: dict[str, list[tuple[str, str]]],
    binding: sl.DataflowBinding,
) -> tuple[list[str], list[object]]:
    r"""Render ``{dim_id: [(op, value), …]}`` as ``(fragments, params)`` SQL clauses.

    Resolves each component id against the binding's dimension + attribute
    column maps; unknown ids raise ``HTTPException(400)`` so callers see
    exactly which one was wrong. Each ``(op, value)`` becomes one
    ``WHERE`` fragment using the template in `NON_EQ_OPERATOR_SQL`.

    For ``LIKE``-based operators (``co``/``nc``/``sw``/``ew``) the user-supplied
    value is escaped (``%``, ``_``, ``\\``) and wrapped in ``%`` wildcards per
    operator semantics — so ``co:Census`` matches ``…Census…``, ``sw:Census``
    matches ``Census…``, ``ew:Province`` matches ``…Province``, ``nc:foo``
    is the NOT counterpart of ``co:foo``.
    """
    fragments: list[str] = []
    params: list[object] = []
    for dim_id, terms in non_eq_terms.items():
        column = binding.dimension_column_map.get(dim_id) or binding.attribute_column_map.get(dim_id)
        if column is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"c[{dim_id}]: unknown component. "
                    f"Valid dimensions: {sorted(binding.dimension_column_map)}; "
                    f"valid attributes: {sorted(binding.attribute_column_map)}."
                ),
            )
        for op, raw in terms:
            fragments.append(NON_EQ_OPERATOR_SQL[op].format(col=sql_ident(column)))
            params.append(like_pattern(op, raw) if op in LIKE_OPS else raw)
    return fragments, params


def validate_dimension_at_observation(value: str | None, binding: sl.DataflowBinding) -> None:
    """Validate the SDMX-REST 2.2.2 ``dimensionAtObservation`` param.

    Accepts ``None`` (param omitted — defaults to ``AllDimensions``), the
    literal ``AllDimensions``, or any key-dimension ID declared in the
    binding. Anything else is 400.
    """
    if value is None or value == "AllDimensions":
        return
    if value in binding.dimension_column_map:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"dimensionAtObservation: {value!r} is not a key dimension on this DSD. "
            f"Valid: {sorted(binding.dimension_column_map)}; or use the keyword `AllDimensions`."
        ),
    )


def parse_sort(
    raw: str,
    binding: sl.DataflowBinding,
) -> list[tuple[str, Literal["ASC", "DESC"]]]:
    """Parse SDMX-REST 2.2.2 ``sort=`` into ``[(physical_column, direction), …]``.

    Syntax (per spec + upstream ``PaginationHelper.cs``):

    - ``comp_id[:asc|:desc]`` — single component
    - ``comp_id[:asc|:desc]+comp_id[:asc|:desc]…`` — multi-component, ``+``-joined
    - ``*[:asc|:desc]`` — every key dimension in DSD order
    - ``series_key[:asc|:desc]`` — every non-TIME key dimension in DSD order,
      for clients wanting a stable series listing independent of TIME_PERIOD

    ``series_key`` combined with ``dimensionAtObservation=AllDimensions`` is
    upstream-invalid (AllDimensions already flattens series); that check belongs
    in the caller, which sees both params.

    Each segment names a key dimension or attribute. Unknown ids raise
    ``HTTPException(400)`` so callers see exactly which one was wrong.
    """
    out: list[tuple[str, Literal["ASC", "DESC"]]] = []
    for term in raw.split("+"):
        head, sep, tail = term.partition(":")
        head = head.strip()
        direction: Literal["ASC", "DESC"] = "DESC" if sep and tail.strip().lower() == "desc" else "ASC"
        if head == "*":
            out.extend((col, direction) for col in binding.dimension_column_map.values())
            continue
        if head == "series_key":
            for dim_id, col in binding.dimension_column_map.items():
                if dim_id != "TIME_PERIOD":
                    out.append((col, direction))
            continue
        col = binding.dimension_column_map.get(head) or binding.attribute_column_map.get(head)
        if col is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"sort: unknown component {head!r}. "
                    f"Valid dimensions: {sorted(binding.dimension_column_map)}; "
                    f"valid attributes: {sorted(binding.attribute_column_map)}; "
                    "or use `*` for every key dimension in DSD order."
                ),
            )
        out.append((col, direction))
    return out


def reject_unsupported_standard_params(
    *,
    includeHistory: bool | None,  # noqa: N803
    updatedAfter: str | None,  # noqa: N803
    reportingYearStartDay: str | None,  # noqa: N803
) -> None:
    """Return 501 if the caller asked for a SDMX-REST 2.2.2 param this server won't implement.

    Consumers backed by a single static bundle have no version history and no
    fiscal-period semantics, so these three can never be implemented for
    them.
    """
    unsupported = [
        name
        for name, value in (
            ("includeHistory", includeHistory),
            ("updatedAfter", updatedAfter),
            ("reportingYearStartDay", reportingYearStartDay),
        )
        if value is not None
    ]
    if unsupported:
        raise HTTPException(
            status_code=501,
            detail=(
                f"query parameter(s) {unsupported!r} are valid SDMX-REST 2.2.2 surface "
                "but permanently not implemented by this server "
                "(single static bundle; no version history)."
            ),
        )
