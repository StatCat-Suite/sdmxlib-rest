"""The pre-serialise structure-message annotation seam.

Some consumers need to project extra annotations onto a ``StructureMessage``
*before* it is written — e.g. the ``.Stat`` Suite Data Explorer discovers a
DSD's reference-metadata (MSD) by reading a ``type="METADATA"`` annotation on
the DataStructure (its ``getMSDIdentifiers`` selector). Because this seam runs
on the **model**, ahead of ``to_json`` / ``to_xml``, the annotation reaches
*every* wire format (SDMX-JSON 2.0, SDMX-ML 2.1 / 3.0) — unlike a post-serialise
JSON splice, which only ever touches the JSON body.

The library does not know which annotations a given catalogue wants to emit, so
a consumer implements `MessageAnnotator`; the default
`NullMessageAnnotator` returns the message untouched. The annotator is
carried on the `CatalogRuntime` and applied by the
structure route right after it builds the message and before it serialises.

The runtime owns the DuckDB connection (structural + metadata linkage live in
the same meta store), so it is passed in; the annotator owns only the
projection.

An annotator whose output can change while the bundle stays the same — one
applying an annotation overlay that is edited on a running deployment — also
implements `SupportsFingerprint` (GH-2). The runtime folds the fingerprint into
`CatalogRuntime.effective_token`, which the structure cache and the ETag key
on, so an annotation change is never served from a stale cache entry or
answered with a 304.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from attrs import frozen

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
        registry: sl.BundleRegistry,
        conn: duckdb.DuckDBPyConnection,
        token: str,
    ) -> StructureMessage:
        """Return ``message`` (or a copy) with the consumer's annotations applied.

        ``registry`` + ``conn`` let the annotator derive whatever it needs from
        the bundle; ``token`` changes at every publication of the bundle, so
        the annotator can memoise derivations per publication.
        """
        ...


@runtime_checkable
class SupportsFingerprint(Protocol):
    """An annotator whose output can change while the bundle's token does not."""

    def fingerprint(self) -> str:
        """A value that changes whenever `annotate` would produce different output.

        Called on every request, by the ETag middleware and the structure
        route, so it must be cheap: a version counter or a digest computed when
        the annotator's inputs change, never I/O. ``""`` means the output
        depends on the bundle alone, which is the same as not implementing this.
        """
        ...


def annotator_fingerprint(annotator: MessageAnnotator) -> str:
    """``annotator``'s fingerprint, or ``""`` when it does not implement one."""
    return annotator.fingerprint() if isinstance(annotator, SupportsFingerprint) else ""


@frozen
class CompositeMessageAnnotator:
    """Several annotators applied in order, as one.

    Its fingerprint joins its members': ``""`` while none has one, so a
    composite of fingerprint-free annotators keys exactly as they would alone.
    """

    annotators: tuple[MessageAnnotator, ...]

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.BundleRegistry,
        conn: duckdb.DuckDBPyConnection,
        token: str,
    ) -> StructureMessage:
        """Run each annotator on the previous one's output."""
        for annotator in self.annotators:
            message = annotator.annotate(message, registry=registry, conn=conn, token=token)
        return message

    def fingerprint(self) -> str:
        """The members' fingerprints, by position; ``""`` if every one is empty."""
        prints = [annotator_fingerprint(a) for a in self.annotators]
        return "\n".join(f"{i}:{fp}" for i, fp in enumerate(prints)) if any(prints) else ""


class NullMessageAnnotator:
    """Default annotator — leaves every message untouched."""

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.BundleRegistry,  # noqa: ARG002
        conn: duckdb.DuckDBPyConnection,  # noqa: ARG002
        token: str,  # noqa: ARG002
    ) -> StructureMessage:
        """Return the message unchanged."""
        return message


__all__ = [
    "CompositeMessageAnnotator",
    "MessageAnnotator",
    "NullMessageAnnotator",
    "SupportsFingerprint",
    "annotator_fingerprint",
]
