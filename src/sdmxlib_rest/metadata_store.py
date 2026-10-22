"""The reference-metadata store seam.

The ``/metadata/*`` routes need to answer "which MetadataSets are attached to
this structural artefact / metadataflow?" — a lookup over the bundle's
referential-metadata linkage. The library does not know how that linkage is
stored (a consumer's bundle keeps it in its own DuckDB tables); it only needs
the set URNs back, which it then materialises through the catalog like any
other artefact.

A consumer implements :class:`MetadataStore`; the default
:class:`NullMetadataStore` returns nothing, so a catalogue without a metadata
linkage simply reports no attached sets. The store is carried on the
:class:`~sdmxlib_rest.runtime.CatalogRuntime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import duckdb


@runtime_checkable
class MetadataStore(Protocol):
    """Consumer-owned lookup from structural artefacts to attached MetadataSet URNs.

    The runtime owns the DuckDB connection (the metadata linkage lives in the
    same meta store as everything else), so it is passed in; the store owns only
    the queries.
    """

    def sets_for_structure(
        self, conn: duckdb.DuckDBPyConnection, *, structure_urn: str, provider_id: str | None, limit: int
    ) -> list[str]:
        """MetadataSet URNs whose declared structure is ``structure_urn``.

        ``provider_id`` (or ``None`` for all providers) narrows to sets from one
        data provider; ``limit`` caps the result. URNs come back sorted.
        """
        ...

    def sets_for_target(
        self, conn: duckdb.DuckDBPyConnection, *, target_urn: str, item_pattern: str | None, limit: int
    ) -> list[str]:
        """MetadataSet URNs attached to ``target_urn`` (or item URNs matching ``item_pattern``).

        ``item_pattern`` is a URN LIKE-pattern for item-level targets under the
        artefact (e.g. Concepts of a ConceptScheme); ``None`` matches the exact
        target only. URNs come back distinct + sorted.
        """
        ...


class NullMetadataStore:
    """Default store — no metadata linkage, so every artefact reports no attached sets."""

    def sets_for_structure(
        self,
        conn: duckdb.DuckDBPyConnection,  # noqa: ARG002
        *,
        structure_urn: str,  # noqa: ARG002
        provider_id: str | None,  # noqa: ARG002
        limit: int,  # noqa: ARG002
    ) -> list[str]:
        """Always empty."""
        return []

    def sets_for_target(
        self,
        conn: duckdb.DuckDBPyConnection,  # noqa: ARG002
        *,
        target_urn: str,  # noqa: ARG002
        item_pattern: str | None,  # noqa: ARG002
        limit: int,  # noqa: ARG002
    ) -> list[str]:
        """Always empty."""
        return []


__all__ = ["MetadataStore", "NullMetadataStore"]
