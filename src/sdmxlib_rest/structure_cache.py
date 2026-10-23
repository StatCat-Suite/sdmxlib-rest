"""The structure-response cache seam.

The generic structure route can serve a previously-rendered response straight
from a consumer-owned cache, skipping materialise + serialise + splice. The
library does not know *which* responses are worth caching (that is a
catalog-shaped speedup decision — e.g. a consumer may cache its largest
codelists and hierarchies) nor *where* the bytes live; it
only knows to ask the injected `StructureCache` before doing work and to
offer the finished response back for storage.

A consumer implements `StructureCache`; the default
`NullStructureCache` caches nothing, so a bare catalog just always does
the work. The cache is carried on the `CatalogRuntime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from attrs import frozen

if TYPE_CHECKING:
    from fastapi.responses import Response


@frozen
class StructureQuery:
    """The resolved structure request a cache keys on.

    ``version`` is post-``latest``-resolution and ``token`` is the runtime's
    ``effective_token``: one publication of the bundle, with the annotator's
    fingerprint folded in, so a re-publish or an annotation change invalidates
    every slot.
    """

    structure_type: str
    agency_id: str
    resource_id: str
    version: str
    detail: str
    references: str
    accept: str | None
    accept_language: str | None
    token: str


@runtime_checkable
class StructureCache(Protocol):
    """Consumer-owned cache for rendered structure responses.

    ``lookup`` returns a ready response to short-circuit with, or ``None`` to
    proceed with the live render. ``store`` is offered the finished response
    (only 2xx, body-bearing responses are passed) for the consumer to cache if
    its own gating says the query is worth it.
    """

    def lookup(self, query: StructureQuery) -> Response | None:
        """Return a cached response for ``query``, or ``None`` to render live."""
        ...

    def store(self, query: StructureQuery, response: Response) -> None:
        """Offer a freshly rendered response for caching (consumer decides)."""
        ...


class NullStructureCache:
    """Default no-op cache — never hits, never stores."""

    def lookup(self, query: StructureQuery) -> Response | None:  # noqa: ARG002
        """Never a hit."""
        return None

    def store(self, query: StructureQuery, response: Response) -> None:
        """No-op."""


__all__ = ["NullStructureCache", "StructureCache", "StructureQuery"]
