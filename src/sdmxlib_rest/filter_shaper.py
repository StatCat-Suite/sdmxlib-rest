"""The component-filter shaper seam.

Between "parse the request into a :class:`~sdmxlib.ComponentFilter`" and "turn
that filter into WHERE clauses" there is a gap where catalogs legitimately want
to intervene, for two different reasons:

*Semantics.* A vendor query parameter can widen the filter — a
``within[GEO]=<parent>`` descends a geographic hierarchy and rewrites the
dimension's value list to every code beneath that parent. The client asked one
question; the catalog answers a broader one because it knows the hierarchy.

*Physics.* A catalog that knows its own partition layout can add a redundant
predicate that changes nothing about the result but everything about the scan —
e.g. a lake partitioned by geographic level can inject a ``GEO_LEVEL`` equality
when ``REF_AREA`` is a short concrete list, which is what lets DuckDB prune
partitions.

Neither belongs in a generic library: the first is a vendor extension, the
second is knowledge of a specific lake layout. But both have to happen at the
same point in every route that builds a filter, so the library defines the hook
and the catalog fills it. The default :class:`NullComponentFilterShaper` returns
the filter untouched, so a catalog that models neither pays nothing.

The shaper receives the raw query items rather than pre-parsed values, because
the library does not know which parameters a given extension reads — parsing
``within[...]`` is the shaper's own business.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from attrs import frozen

if TYPE_CHECKING:
    import duckdb
    import sdmxlib as sl


@frozen
class FilterContext:
    """Everything a shaper needs to rewrite a filter, resolved once per request.

    ``query_items`` is the full multi-valued query string as ``(name, value)``
    pairs — repeated parameters are preserved, which bracketed vendor params
    like ``within[DIM]`` rely on.
    """

    registry: sl.LocalRegistry
    conn: duckdb.DuckDBPyConnection
    flow_urn: sl.SdmxUrn[sl.Dataflow]
    binding: sl.DataflowBinding
    query_items: tuple[tuple[str, str], ...]


@runtime_checkable
class ComponentFilterShaper(Protocol):
    """Consumer hook to rewrite a parsed component filter before SQL generation.

    Implementations must be pure with respect to the request: return a new
    filter rather than mutating the input, and raise ``ValueError`` /
    ``KeyError`` for a malformed vendor parameter so the route can turn it into
    a 400 alongside the standard key-parse errors.
    """

    def shape(self, component_filter: sl.ComponentFilter, ctx: FilterContext) -> sl.ComponentFilter:
        """Return the filter to generate SQL from — possibly the input unchanged."""
        ...


class NullComponentFilterShaper:
    """Default no-op shaper — returns the filter exactly as parsed."""

    def shape(self, component_filter: sl.ComponentFilter, ctx: FilterContext) -> sl.ComponentFilter:  # noqa: ARG002
        """Pass the filter through untouched."""
        return component_filter


__all__ = ["ComponentFilterShaper", "FilterContext", "NullComponentFilterShaper"]
