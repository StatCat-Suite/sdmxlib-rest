"""The pre-serialise structure-message annotation seam.

Some consumers need to project extra annotations onto a ``StructureMessage``
*before* it is written — e.g. the ``.Stat`` Suite Data Explorer discovers a
DSD's reference-metadata (MSD) by reading a ``type="METADATA"`` annotation on
the DataStructure (its ``getMSDIdentifiers`` selector). Because this seam runs
on the **model**, ahead of ``to_json`` / ``to_xml``, the annotation reaches
*every* wire format (SDMX-JSON 2.0, SDMX-ML 2.1 / 3.0) — unlike a post-serialise
JSON splice, which only ever touches the JSON body.

The library does not know which annotations a given catalogue wants to emit, so
a consumer implements :class:`MessageAnnotator`; the default
:class:`NullMessageAnnotator` returns the message untouched. The annotator is
carried on the :class:`~sdmxlib_rest.runtime.CatalogRuntime` and applied by the
structure route right after it builds the message and before it serialises.

The runtime owns the DuckDB connection (structural + metadata linkage live in
the same meta store), so it is passed in; the annotator owns only the
projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import duckdb
    import sdmxlib as sl
    from sdmxlib.model.message import StructureMessage


@runtime_checkable
class MessageAnnotator(Protocol):
    """Consumer-owned projection applied to a structure message before serialisation."""

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.LocalRegistry,
        conn: duckdb.DuckDBPyConnection,
        build_id: str,
    ) -> StructureMessage:
        """Return ``message`` (or a copy) with the consumer's annotations applied.

        ``registry`` + ``conn`` let the annotator derive whatever it needs from
        the bundle; ``build_id`` identifies the (immutable) bundle so the
        annotator can memoise per-build derivations.
        """
        ...


class NullMessageAnnotator:
    """Default annotator — leaves every message untouched."""

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.LocalRegistry,  # noqa: ARG002
        conn: duckdb.DuckDBPyConnection,  # noqa: ARG002
        build_id: str,  # noqa: ARG002
    ) -> StructureMessage:
        """Return the message unchanged."""
        return message


__all__ = ["MessageAnnotator", "NullMessageAnnotator"]
